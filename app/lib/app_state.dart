import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:celestial_llm/celestial_llm.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:path_provider/path_provider.dart';

import 'ai/local_ai.dart';
import 'core/models.dart';
import 'core/personal.dart';
import 'core/settings.dart';
import 'core/store.dart';
import 'net/pc_client.dart';
import 'platform.dart';

enum LinkState { unpaired, connecting, online, offline, unauthorized }

class AgentStep {
  AgentStep(this.n, this.label, this.detail);
  final int n;
  final String label;
  final String detail;
  bool? ok;
}

class Approval {
  Approval(this.json);
  final Map<String, dynamic> json;
  String get id => json['id'] as String;
  String get label => json['label'] as String? ?? '';
  String get summary => json['summary'] as String? ?? '';
  String get preview => json['preview'] as String? ?? '';
  String? get conversationId => json['conversation_id'] as String?;
}

class PushItem {
  PushItem(this.kind, this.text, this.sender) : at = DateTime.now();
  final String kind, text, sender;
  final DateTime at;
}

/// 回答を作っている途中の表示(会話ごと)。
class RunView {
  RunView(this.mode);
  final String mode; // phone / pc
  String text = '';
  String stage = '';
  final List<AgentStep> steps = [];
  final List<Approval> approvals = [];
  String? runId;
  String? assistantId;
  String? error;
}

const _roleLabels = {
  'general': '一般',
  'code': 'コード',
  'reason': '計算・推論',
  'summarize': '要約',
  'translate': '翻訳',
  'write': '文章作成',
  'extract': '抽出・整形',
  'complex': '複合',
};

const _localSystemPrompt = 'あなたは Celestial のスマホ内AIです。ユーザーと同じ言語で、簡潔かつ正確に答えてください。'
    '確かでないことは推測で断定せず、分からないと伝えてください。';
final _kana = RegExp(r'[぀-ヿ]');
final _foreignRequest = RegExp(r'英語|英文|英訳|English|中国語|中文|韓国語|フランス語|ドイツ語|スペイン語', caseSensitive: false);

/// アプリ全体の状態と、PC との連携。
class AppState extends ChangeNotifier with WidgetsBindingObserver {
  late final Settings settings;
  late final LocalStore store;
  late final LocalAi ai;
  PcClient? client;
  LinkState link = LinkState.unpaired;
  String? linkError;
  Map<String, dynamic> pcState = {};
  final Map<String, RunView> runs = {};
  final List<Approval> globalApprovals = [];
  final List<PushItem> inbox = [];
  final _messages = StreamController<String>.broadcast();
  final _launch = StreamController<Map<String, dynamic>>.broadcast();
  StreamSubscription? _eventsSub;
  StreamSubscription? _intentSub;
  Timer? _retryTimer;
  Timer? _syncTimer;
  Timer? _notifyTimer;
  int _retryDelay = 2;
  bool _syncing = false;
  bool _syncAgain = false;
  bool foreground = true;
  bool ready = false;
  Map<String, dynamic>? _pendingLaunch;

  /// 起動のきっかけ(リンクや共有)を1度だけ受け取る。画面ができる前に届いた分もここで受け取れる。
  Map<String, dynamic>? takeLaunch() {
    final data = _pendingLaunch;
    _pendingLaunch = null;
    return data;
  }

  Stream<String> get messages => _messages.stream;
  Stream<Map<String, dynamic>> get launches => _launch.stream;
  bool get isDesktop => Platform.isWindows || Platform.isLinux || Platform.isMacOS;
  bool get paired => isDesktop || settings.paired;
  bool get online => link == LinkState.online;
  String get deviceId => isDesktop ? 'pc' : settings.deviceId;
  String get pcName => settings.pcName.isNotEmpty ? settings.pcName : (pcState['name'] as String? ?? 'PC');

  void toast(String text) => _messages.add(text);

  /// 担当の名前(PC から受け取れていなければ、アプリに入っている名前)。
  String roleLabel(Object? role) {
    final labels = pcState['role_labels'] as Map? ?? const {};
    return labels[role]?.toString() ?? _roleLabels[role] ?? role?.toString() ?? '';
  }

  // ---- 起動 ----

  Future<void> init() async {
    WidgetsBinding.instance.addObserver(this);
    final support = await getApplicationSupportDirectory();
    settings = Settings(File('${support.path}/settings.json'));
    await settings.load();
    store = LocalStore(File('${support.path}/conversations.json'));
    await store.load();
    // 裏での同期(PC で作り直した回答・名前の変更など)を、開いている画面にすぐ映す
    store.addListener(notifyListeners);
    ai = LocalAi(Directory('${support.path}/models'));
    await ai.scan();
    if (settings.deviceId.isEmpty) settings.deviceId = newId().substring(0, 16);
    if (settings.deviceName.isEmpty) settings.deviceName = await PlatformBridge.deviceName();
    if (isDesktop) {
      settings.host = '127.0.0.1';
      settings.port = 8765;
    }
    if (settings.localModel.isEmpty && ai.installed.isNotEmpty) settings.localModel = ai.installed.first.name;
    await settings.save();
    ready = true;
    notifyListeners();
    final first = await PlatformBridge.launchData();
    if (first.isNotEmpty) _pendingLaunch = first;
    _intentSub = PlatformBridge.intents().listen((data) {
      _pendingLaunch = data;
      _launch.add(data);
    });
    notifyListeners();
    if (paired) unawaited(connect());
    _syncTimer = Timer.periodic(const Duration(seconds: 45), (_) {
      if (online) unawaited(sync());
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    foreground = state == AppLifecycleState.resumed;
    if (state == AppLifecycleState.paused) unawaited(store.save());
    if (foreground && paired && !online && link != LinkState.connecting) unawaited(connect());
    if (foreground && online) unawaited(sync());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _eventsSub?.cancel();
    _intentSub?.cancel();
    _retryTimer?.cancel();
    _syncTimer?.cancel();
    client?.close();
    super.dispose();
  }

  // ---- 接続 ----

  Future<void> connect() async {
    _retryTimer?.cancel();
    _eventsSub?.cancel();
    client?.close();
    if (!paired) {
      link = LinkState.unpaired;
      notifyListeners();
      return;
    }
    client = PcClient(host: settings.host, port: settings.port, token: isDesktop ? null : settings.token);
    link = LinkState.connecting;
    notifyListeners();
    try {
      final hello = await client!.getJson('/api/hello', timeout: const Duration(seconds: 8));
      if (hello['authenticated'] != true) {
        link = LinkState.unauthorized;
        linkError = 'この端末の登録が取り消されています。PC でもう一度接続してください';
        notifyListeners();
        return;
      }
      pcState = await client!.getJson('/api/state');
      _takePersonal();
      if ((hello['name'] as String?)?.isNotEmpty ?? false) settings.pcName = hello['name'] as String;
      link = LinkState.online;
      linkError = null;
      _retryDelay = 2;
      notifyListeners();
      _listen();
      await sync();
    } catch (e) {
      _offline(e);
    }
  }

  void _offline(Object e) {
    if (e is PcException && e.unauthorized) {
      link = LinkState.unauthorized;
      linkError = 'この端末の登録が取り消されています。PC でもう一度接続してください';
      notifyListeners();
      return;
    }
    link = LinkState.offline;
    linkError = describeError(e);
    notifyListeners();
    _retryTimer?.cancel();
    _retryTimer = Timer(Duration(seconds: _retryDelay), () {
      if (foreground) unawaited(connect());
    });
    _retryDelay = (_retryDelay * 2).clamp(2, 60);
  }

  void _listen() {
    _eventsSub?.cancel();
    final c = client;
    if (c == null) return;
    _eventsSub = c.events().listen(_onEvent, onError: (Object e) {
      if (identical(c, client)) _offline(e);
    }, onDone: () {
      if (identical(c, client) && link == LinkState.online) _offline(const SocketException('切断'));
    });
  }

  void _onEvent(Map<String, dynamic> e) {
    switch (e['type']) {
      case 'sync':
      case 'run_finished':
      case 'run_started':
        _scheduleSync();
      case 'approval':
        final approval = Approval(e);
        final local = runs.values.any((r) => r.runId == e['run_id']);
        if (!local && !globalApprovals.any((a) => a.id == approval.id)) {
          globalApprovals.add(approval);
          notifyListeners();
          if (!foreground && settings.notifications) PlatformBridge.notify('Celestial: 確認が必要です', '${approval.label} ${approval.summary}');
        }
      case 'approval_done':
        globalApprovals.removeWhere((a) => a.id == e['id']);
        for (final r in runs.values) {
          r.approvals.removeWhere((a) => a.id == e['id']);
        }
        notifyListeners();
      case 'push':
        final item = PushItem(e['kind'] as String? ?? 'text', e['text'] as String? ?? '', e['sender'] as String? ?? 'PC');
        inbox.insert(0, item);
        if (item.kind == 'clipboard') {
          Clipboard.setData(ClipboardData(text: item.text));
          toast('PC からクリップボードを受け取りました');
        } else {
          toast('${item.sender} から: ${item.text.length > 60 ? '${item.text.substring(0, 60)}…' : item.text}');
        }
        if (!foreground && settings.notifications) PlatformBridge.notify('${item.sender} から届きました', item.text);
        notifyListeners();
      case 'settings':
        unawaited(refreshPcState());
      case 'flows':
        flowsVersion++;
        notifyListeners();
      case 'flow_run':
        flowsVersion++;
        if (e['status'] == 'error') toast('自動化が途中で失敗しました。「自動化」で記録を確かめてください');
        notifyListeners();
    }
  }

  // ---- 自動化(PC で動くワークフロー) ----

  /// 自動化が変わる・動くたびに増える(画面が読み直すきっかけ)。
  int flowsVersion = 0;

  Future<Map<String, dynamic>> flows() async => client!.getJson('/api/flows');

  Future<List<Map<String, dynamic>>> flowRuns(String id) async =>
      [for (final r in (await client!.getJson('/api/flows/$id/runs'))['runs'] as List) Map<String, dynamic>.from(r as Map)];

  Future<void> runFlow(String id, String name) async {
    try {
      await client!.postJson('/api/flows/$id/run', {});
      toast('「$name」を動かしています');
    } catch (e) {
      toast(describeError(e));
    }
  }

  Future<void> setFlowEnabled(String id, bool enabled) async {
    try {
      await client!.postJson('/api/flows/$id/enabled', {'enabled': enabled});
    } catch (e) {
      toast(describeError(e));
    }
  }

  Future<void> refreshPcState() async {
    try {
      pcState = await client!.getJson('/api/state');
      _takePersonal();
      notifyListeners();
    } catch (_) {}
  }

  /// PC の「AIへの指示」と「覚えていること」を端末にも保存する(PC につながっていないときも、スマホの中のAIが使う)。
  void _takePersonal() {
    final s = pcState['settings'];
    if (s is! Map) return;
    final instructions = s['instructions'] as String? ?? '';
    final memories = [for (final m in (s['memories'] as List? ?? const [])) if (m is String) m];
    if (instructions != settings.instructions || memories.join('\n') != settings.memories.join('\n')) {
      settings.update((x) {
        x.instructions = instructions;
        x.memories = memories;
      });
    }
  }

  /// 「AIへの指示」「覚えていること」を PC に保存する(設定画面と、スマホの中のAIへの「覚えて」から使う)。
  Future<void> savePersonal({String? instructions, List<String>? memories}) async {
    final c = client;
    if (c == null || !online) throw const SocketException('PC につながっていません。PC とつながっているときに変えられます');
    await c.postJson('/api/settings', {'instructions': ?instructions, 'memories': ?memories});
    await refreshPcState();
  }

  void _scheduleSync() {
    _notifyTimer?.cancel();
    _notifyTimer = Timer(const Duration(milliseconds: 350), () => unawaited(sync()));
  }

  /// 未送信の変更を送り、PC の変更を受け取る。
  Future<void> sync() async {
    final c = client;
    if (c == null || !online) return;
    if (_syncing) {
      _syncAgain = true;
      return;
    }
    _syncing = true;
    try {
      do {
        _syncAgain = false;
        final pending = store.pending();
        if (pending.conversations.isNotEmpty || pending.messages.isNotEmpty) {
          await c.postJson('/api/sync', {'conversations': pending.conversations, 'messages': pending.messages});
          store.markSent(pending.conversations, pending.messages);
        }
        while (true) {
          final page = await c.getJson('/api/sync', query: {'since': '${store.lastRev}', 'limit': '500'});
          final db = page['db'] as String?;
          if (db != null && db != store.dbId) {
            // PC のデータベースが変わった(作り直された・別の PC になった): 続きからではなく最初から取り直す
            final first = store.dbId == null;
            store.dbId = db;
            if (!first && store.lastRev != 0) {
              store.lastRev = 0;
              continue;
            }
          }
          store.applyRemote(
            conversations: [for (final x in page['conversations'] as List) Map<String, dynamic>.from(x as Map)],
            messages: [for (final x in page['messages'] as List) Map<String, dynamic>.from(x as Map)],
            rev: (page['rev'] as num).toInt(),
          );
          if (page['more'] != true) break;
        }
      } while (_syncAgain);
    } catch (e) {
      _offline(e);
    } finally {
      _syncing = false;
    }
  }

  // ---- 登録 ----

  Future<void> pair(String host, int port, String code) async {
    final c = PcClient(host: host.trim(), port: port);
    try {
      final hello = await c.getJson('/api/hello', timeout: const Duration(seconds: 8));
      if (hello['app'] != 'Celestial') throw const PcException(0, 'Celestial の PC ではないようです');
      final result = await c.postJson('/api/pair', {'code': code, 'name': settings.deviceName, 'platform': Platform.operatingSystem});
      settings.host = host.trim();
      settings.port = port;
      settings.token = result['token'] as String;
      settings.pcName = result['pc_name'] as String? ?? '';
      await settings.save();
      store.lastRev = 0;  // 新しい PC なら、会話を最初から受け取る
      await connect();
    } finally {
      c.close();
    }
  }

  Future<void> unpair() async {
    _eventsSub?.cancel();
    client?.close();
    client = null;
    settings.token = '';
    await settings.save();
    link = LinkState.unpaired;
    notifyListeners();
  }

  // ---- 会話 ----

  Conversation newConversation() => store.createConversation(origin: deviceId);

  Future<String> send(String? conversationId, String text, {required bool usePc, bool agent = false, List<Uint8List> images = const []}) async {
    final conv = conversationId != null && store.conversations[conversationId] != null
        ? store.conversations[conversationId]!
        : store.createConversation(origin: deviceId);
    if (runs.containsKey(conv.id)) throw StateError('この会話はまだ回答の途中です');
    if (usePc) {
      unawaited(_sendPc(conv, text, agent: agent, images: images));
    } else {
      unawaited(_sendLocal(conv, text));
    }
    return conv.id;
  }

  Future<void> _sendPc(Conversation conv, String text, {required bool agent, required List<Uint8List> images}) async {
    final view = RunView('pc')..stage = 'PC に送信中…';
    runs[conv.id] = view;
    final userId = newId();
    store.addMessage(conv.id, 'user', text, id: userId, dirty: false, meta: {'device': deviceId, if (images.isNotEmpty) 'images': images.length});
    notifyListeners();
    var started = false;
    try {
      await sync();
      final stream = client!.postLines('/api/run', {
        'task': text,
        'conversation_id': conv.id,
        'message_id': userId,
        'agent': agent,
        'context': 'auto',
        if (images.isNotEmpty) 'images': [for (final i in images) base64Encode(i)],
      });
      await for (final e in stream) {
        _onRunEvent(conv.id, view, e);
        if (e['type'] == 'run') started = true;
      }
    } catch (e) {
      if (!started) {
        store.dirtyMessages.add(userId);  // あとで PC に届ける
        view.error = describeError(e);
      } else {
        view.stage = '接続が切れました。回答は PC で続いていて、つながると届きます';
      }
      toast(describeError(e));
    } finally {
      runs.remove(conv.id);
      notifyListeners();
      unawaited(sync());
      if (!foreground && settings.notifications) PlatformBridge.notify('Celestial', '回答が届きました: ${conv.title}');
    }
  }

  void _onRunEvent(String conversationId, RunView view, Map<String, dynamic> e) {
    switch (e['type']) {
      case 'run':
        view.runId = e['run_id'] as String?;
        view.assistantId = e['assistant_message_id'] as String?;
        view.stage = e['context'] == true ? '会話の流れを踏まえて考えています…' : 'キャッシュと担当を確認中…';
        if (view.assistantId != null && store.messages[view.assistantId] == null) {
          store.addMessage(conversationId, 'assistant', '', id: view.assistantId, dirty: false, meta: {'status': 'running', 'device': 'pc'});
        }
      case 'token':
        view.text += e['text'] as String? ?? '';
      case 'stage':
        if (e['stage'] == 'retry') view.text = '';
        if (e['stage'] == 'agent_step') {
          view.steps.add(AgentStep((e['n'] as num?)?.toInt() ?? view.steps.length + 1, e['label'] as String? ?? '', e['detail'] as String? ?? ''));
        }
        view.stage = _stageText(e) ?? view.stage;
      case 'approval':
        view.approvals.add(Approval(e));
        if (!foreground && settings.notifications) PlatformBridge.notify('Celestial: 確認が必要です', '${e['label']} ${e['summary'] ?? ''}');
      case 'approval_done':
        view.approvals.removeWhere((a) => a.id == e['id']);
      case 'done':
        final m = Map<String, dynamic>.from(e['message'] as Map);
        store.applyRemote(conversations: const [], messages: [m], rev: store.lastRev);
      case 'error':
        view.error = e['message'] as String?;
      case 'cancelled':
        view.stage = '停止しました';
    }
    _throttledNotify();
  }

  DateTime _lastNotify = DateTime.fromMillisecondsSinceEpoch(0);
  Timer? _pendingNotify;
  void _throttledNotify() {
    final now = DateTime.now();
    if (now.difference(_lastNotify).inMilliseconds > 60) {
      _lastNotify = now;
      notifyListeners();
    } else {
      _pendingNotify?.cancel();
      _pendingNotify = Timer(const Duration(milliseconds: 70), () {
        _lastNotify = DateTime.now();
        notifyListeners();
      });
    }
  }

  String? _stageText(Map<String, dynamic> e) {
    String role(Object? r) => roleLabel(r);
    return switch (e['stage']) {
      'cache' => 'キャッシュから即答',
      'route' => '${role(e['role'])}の担当へ',
      'worker' => e['agent'] == true ? '${e['model']} がエージェントとして作業中' : '${e['model']} が回答中',
      'agent_step' => '[${e['n']}手目] ${e['label']} ${e['detail'] ?? ''}',
      'verify' => '管理AIが回答を検証中…',
      'retry' => e['issue'] != null ? '${e['model']} が${e['issue']}中' : '検証で問題が見つかったため再回答中',
      'escalate' => e['reason'] == 'lang' ? '言語を取り違えたため ${e['model']} が答え直し中' : '確からしさが低いため ${e['model']} が答え直し中',
      'ground' => 'Wikipedia で事実を確かめています…',
      'news' => (e['topic'] as String?)?.isNotEmpty == true ? '「${e['topic']}」のニュースを探しています…' : '最新のニュースを取得しています…',
      'project' => switch (e['phase']) {
          'plan' => 'プロジェクトの計画を立てています…',
          'file' => 'ファイルを書いています(${e['n']}/${e['total']}): ${e['path']}',
          'check' => '構文とテストで確かめています…',
          'fix' => '問題のあったファイルを直しています: ${e['path']}',
          _ => null,
        },
      'plan' => '${(e['subtasks'] as List?)?.length ?? 0}件に分解して分担中',
      'synth' => '${e['model']} が結果を統合中',
      _ => null,
    };
  }

  Future<void> _sendLocal(Conversation conv, String text) async {
    final view = RunView('phone')..stage = '端末内のAIを準備中…';
    runs[conv.id] = view;
    final history = store.messagesOf(conv.id).where((m) => !m.running && m.status != 'error' && m.content.isNotEmpty).toList();
    store.addMessage(conv.id, 'user', text, meta: {'device': deviceId});
    notifyListeners();
    final watch = Stopwatch()..start();
    try {
      // 「覚えて」「忘れて」「何を覚えてる?」と、短い日付の質問は、AIを使わずに答える(PC と同じ決まり)
      final quick = await _quickLocalReply(text);
      if (quick != null) {
        store.addMessage(conv.id, 'assistant', quick.$2, meta: {
          'status': 'done',
          'model': quick.$1,
          'device': deviceId,
          'latency_s': watch.elapsedMilliseconds / 1000,
        });
        return;
      }
      final name = settings.localModel;
      if (name.isEmpty || ai.find(name) == null) throw LlmException('端末内のAIがまだありません。「AIモデル」で PC から受け取ってください');
      // 会話の流れは、新しいものから文字数の予算に収まる分だけ渡す
      final budget = (settings.contextSize * 1.2).round();
      final picked = <LlmMessage>[];
      var used = text.length;
      for (final m in history.reversed) {
        if (used + m.content.length > budget) break;
        used += m.content.length;
        picked.insert(0, LlmMessage(m.role, m.content));
      }
      // 小さいAIは日本語の質問に中国語や英語で答えがちなので、日本語の質問にははっきり指示する
      final japanese = _kana.hasMatch(text) && !_foreignRequest.hasMatch(text);
      // 今日の日付(日付が関わる依頼だけ)と、PC から同期した「AIへの指示」「覚えていること」を足す
      final system = (japanese ? '$_localSystemPrompt必ず自然な日本語で答えてください。' : _localSystemPrompt) +
          timeContext(text, DateTime.now(), memories: settings.memories) +
          personalPrompt(settings.instructions, settings.memories);
      final messages = [LlmMessage('system', system), ...picked, LlmMessage('user', text)];
      view.stage = ai.loadedName == name ? '考えています…' : '$name を読み込み中…';
      notifyListeners();
      final stream = ai.generate(name, messages,
          options: LlmOptions(temperature: settings.temperature, maxTokens: 1024), contextSize: settings.contextSize, threads: settings.threads);
      var checked = 0;
      await for (final piece in stream) {
        if (view.text.isEmpty) view.stage = '回答中';
        view.text += piece;
        if (view.text.length - checked >= 64) {
          checked = view.text.length;
          final cut = loopCut(view.text);
          if (cut != null) {
            // 同じ文の繰り返しに入った: 1回分を残して止める(時間と電池の節約)
            view.text = cut;
            ai.stop();
            break;
          }
        }
        _throttledNotify();
      }
      store.addMessage(conv.id, 'assistant', view.text.trim().isEmpty ? '(回答がありませんでした)' : view.text.trim(), meta: {
        'status': 'done',
        'model': 'local:$name',
        'device': deviceId,
        'latency_s': watch.elapsedMilliseconds / 1000,
        'tokens_per_s': double.parse(ai.lastSpeed.toStringAsFixed(1)),
      });
    } catch (e) {
      view.error = e.toString();
      store.addMessage(conv.id, 'assistant', 'エラー: $e', meta: {'status': 'error', 'model': 'local:${settings.localModel}', 'device': deviceId});
    } finally {
      runs.remove(conv.id);
      notifyListeners();
      if (online) unawaited(sync());
    }
  }

  /// AIを使わずに答えられる依頼への答え((モデルの表示名, 回答) か null)。
  Future<(String, String)?> _quickLocalReply(String text) async {
    final command = memoryCommand(text);
    if (command != null) {
      final (kind, fact) = command;
      final memories = List<String>.from(settings.memories);
      if (kind == 'recall') {
        return ('メモリ', memories.isEmpty ? 'まだ何も覚えていません。「覚えて: 〜」と頼むと覚えます。' : '覚えていること:\n${memories.map((m) => '- $m').join('\n')}');
      }
      // 覚えていることは PC に置く(PC・スマホのどちらのAIも同じものを使うため)
      if (!online) return ('メモリ', 'PC につながっていないため、変えられませんでした。PC とつながっているときに、もう一度頼んでください。');
      try {
        if (kind == 'remember') {
          if (memories.contains(fact)) return ('メモリ', 'もう覚えています: $fact');
          await savePersonal(memories: [...memories, fact]);
          return ('メモリ', '覚えました: $fact\n(設定の「あなたに合わせる」で確かめたり消したりできます)');
        }
        final removed = memories.where((m) => m.contains(fact) || fact.contains(m)).toList();
        if (removed.isEmpty) return ('メモリ', '覚えていることの中に「$fact」は見つかりませんでした。');
        await savePersonal(memories: memories.where((m) => !removed.contains(m)).toList());
        return ('メモリ', '忘れました:\n${removed.map((m) => '- $m').join('\n')}');
      } catch (e) {
        return ('メモリ', '変えられませんでした: ${describeError(e)}');
      }
    }
    if (timeSensitive(text)) {
      final answer = dateAnswer(text, settings.memories, DateTime.now());
      if (answer != null) return ('計算', answer);
    }
    return null;
  }

  Future<void> stop(String conversationId) async {
    final view = runs[conversationId];
    if (view == null) return;
    if (view.mode == 'phone') {
      ai.stop();
    } else if (view.runId != null) {
      try {
        await client!.postJson('/api/runs/${view.runId}/cancel', {});
      } catch (e) {
        toast(describeError(e));
      }
    }
  }

  Future<void> approve(Approval approval, bool allow, {bool all = false}) async {
    try {
      await client!.postJson('/api/approve', {'id': approval.id, 'allow': allow, 'all': all});
    } catch (e) {
      toast(describeError(e));
    }
    globalApprovals.removeWhere((a) => a.id == approval.id);
    for (final r in runs.values) {
      r.approvals.removeWhere((a) => a.id == approval.id);
    }
    notifyListeners();
  }

  /// 回答を Word・Excel・PowerPoint のファイルにして(PC で作る)、端末の「ダウンロード」に保存する。
  Future<void> exportMessage(Message message, String format) async {
    final c = client;
    if (c == null || !online) {
      toast('PC につながっていません');
      return;
    }
    try {
      // 端末内のAIの回答など、まだ PC に送っていないものは先に送る(PC が文書にするため)
      for (var i = 0; i < 20 && store.dirtyMessages.contains(message.id); i++) {
        await sync();
        if (store.dirtyMessages.contains(message.id)) await Future.delayed(const Duration(milliseconds: 150));
      }
      final dir = await getTemporaryDirectory();
      final name = '${fileNameFrom(store.conversations[message.conversationId]?.title ?? '')}.$format';
      final dest = File('${dir.path}/export_${message.id}.$format');
      for (final f in [dest, File('${dest.path}.part')]) {
        if (await f.exists()) await f.delete();
      }
      await c.download('/api/messages/${message.id}/export', {'format': format}, dest);
      final saved = await PlatformBridge.saveToDownloads(dest.path, name);
      toast('保存しました: ${saved ?? name}');
    } catch (e) {
      toast(describeError(e));
    }
  }

  /// PC の回答を、キャッシュを使わず検証つきで作り直してもらう(結果は同期で届く)。
  Future<void> regenerate(Message message) async {
    try {
      await client!.postJson('/api/messages/${message.id}/regenerate', {'stream': false});
      toast('検証つきで、もう一度答えています…');
      unawaited(sync());
    } catch (e) {
      toast(describeError(e));
    }
  }

  Future<void> feedback(Message message, bool good) async {
    try {
      await client!.postJson('/api/feedback', {'message_id': message.id, 'good': good});
      store.updateMessage(message.id, meta: {'feedback': good ? 1 : -1}, dirty: false);
      toast(good ? '手本として覚えました' : 'この回答は今後使いません');
    } catch (e) {
      toast(describeError(e));
    }
  }

  /// 回答に付いた「覚えますか」の候補に答える(覚えるのは PC。どちらでも候補は消える)。
  Future<void> answerMemorySuggestion(Message message, bool accept) async {
    final fact = message.meta['memory_suggestion'] as String?;
    try {
      await client!.postJson('/api/messages/${message.id}/memory', {'accept': accept});
      store.updateMessage(message.id, meta: {'memory_suggestion': null}, dirty: false);
      toast(accept ? '覚えました: $fact' : '覚えませんでした');
      if (accept) unawaited(refreshPcState());
    } catch (e) {
      toast(describeError(e));
    }
  }

  void renameConversation(String id, String title) {
    store.updateConversation(id, title: title);
    unawaited(sync());
  }

  void pinConversation(String id, bool pinned) {
    store.updateConversation(id, pinned: pinned);
    unawaited(sync());
  }

  void deleteConversation(String id) {
    store.updateConversation(id, deleted: true);
    unawaited(sync());
  }

  /// PC へ文字・URL・クリップボードを送る。
  Future<void> sendToPc(String kind, String text) async {
    try {
      await client!.postJson('/api/send', {'kind': kind, 'text': text});
      toast(kind == 'clipboard' ? 'PC のクリップボードに入れました' : 'PC に送りました');
    } catch (e) {
      toast(describeError(e));
    }
  }
}
