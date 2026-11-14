import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

import '../app_state.dart';
import '../net/pc_client.dart';
import '../platform.dart';
import '../theme.dart';
import 'common.dart';
import 'models_page.dart';

/// この端末のアプリの版。PC に新しい版が置かれていれば、その場で受け取って入れ替えられる
/// (PC で `celestial publish-app` を実行すると置かれる)。
class _AppUpdate extends StatefulWidget {
  const _AppUpdate();

  @override
  State<_AppUpdate> createState() => _AppUpdateState();
}

class _AppUpdateState extends State<_AppUpdate> {
  Map<String, dynamic> _mine = const {};
  Map<String, dynamic>? _pc;
  double? _progress;
  String? _error;
  bool _checked = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_checked) return;
    _checked = true;
    _check();
  }

  Future<void> _check() async {
    final state = AppScope.read(context);
    final mine = await PlatformBridge.appVersion();
    Map<String, dynamic>? pc;
    final client = state.client;
    if (!state.isDesktop && state.online && client != null) {
      try {
        pc = await client.getJson('/api/app', timeout: const Duration(seconds: 8));
      } catch (_) {
        pc = null; // 版を返さない古い PC 側
      }
    }
    if (mounted) {
      setState(() {
        _mine = mine;
        _pc = pc;
      });
    }
  }

  /// 端末の種類ごとに分けた APK は、版の番号に 1000×種類 が足されている。
  int get _myBuild => ((_mine['code'] as num?)?.toInt() ?? 0) % 1000;
  int get _pcBuild => (_pc?['build'] as num?)?.toInt() ?? 0;
  bool get _newer => _pc?['available'] == true && _myBuild > 0 && _pcBuild > _myBuild;

  Future<void> _update() async {
    final state = AppScope.read(context);
    final client = state.client;
    if (client == null) return;
    setState(() {
      _progress = 0;
      _error = null;
    });
    try {
      final dir = Directory('${(await getTemporaryDirectory()).path}/updates');
      await dir.create(recursive: true);
      final file = File('${dir.path}/celestial.apk');
      await client.download('/app/celestial.apk', const {}, file, onProgress: (got, total) {
        if (mounted && total > 0) setState(() => _progress = got / total);
      });
      final how = await PlatformBridge.installApk(file.path);
      if (how == 'permission') state.toast('「この提供元のアプリを許可」をオンにして戻り、もう一度「更新する」を押してください');
    } on PcException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '更新できませんでした: $e');
    } finally {
      if (mounted) setState(() => _progress = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final name = (_mine['name'] as String?) ?? kAppVersion;
    final size = ((_pc?['size'] as num?) ?? 0) / 1e6;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('Celestial $name', style: const TextStyle(fontWeight: FontWeight.w600)),
      if (_newer) ...[
        const SizedBox(height: 10),
        Container(
          padding: const EdgeInsets.fromLTRB(14, 12, 12, 12),
          decoration: BoxDecoration(color: p.accentSoft, borderRadius: BorderRadius.circular(14)),
          child: Row(children: [
            Icon(Icons.system_update_alt, color: context.colors.primary),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('新しい版 ${_pc!['version']} が PC にあります', style: const TextStyle(fontWeight: FontWeight.w600)),
                const SizedBox(height: 2),
                Text(
                    _progress == null
                        ? '${size.toStringAsFixed(0)}MB。受け取ると、入れ替えの確認が出ます'
                        : '受け取り中 ${(_progress! * 100).toStringAsFixed(0)}%',
                    style: TextStyle(color: p.muted, fontSize: 12.5)),
                if (_progress != null)
                  Padding(padding: const EdgeInsets.only(top: 6), child: LinearProgressIndicator(value: _progress)),
              ]),
            ),
            const SizedBox(width: 8),
            FilledButton(onPressed: _progress == null ? _update : null, child: const Text('更新する')),
          ]),
        ),
      ],
      if (_error != null)
        Padding(padding: const EdgeInsets.only(top: 6), child: Text(_error!, style: TextStyle(color: p.bad, fontSize: 12.5))),
    ]);
  }
}

/// PC との接続(QR コードのリンクや、アドレスとコードの入力)。
Future<void> showPairSheet(BuildContext context, {String host = '', int port = 8765, String code = ''}) {
  return showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (context) => _PairSheet(host: host, port: port, code: code),
  );
}

class _PairSheet extends StatefulWidget {
  const _PairSheet({required this.host, required this.port, required this.code});
  final String host;
  final int port;
  final String code;

  @override
  State<_PairSheet> createState() => _PairSheetState();
}

class _PairSheetState extends State<_PairSheet> {
  late final _host = TextEditingController(text: widget.host);
  late final _port = TextEditingController(text: '${widget.port}');
  late final _code = TextEditingController(text: widget.code);
  bool _busy = false;
  String? _error;

  Future<void> _pair() async {
    final state = AppScope.read(context);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await state.pair(_host.text.trim(), int.tryParse(_port.text.trim()) ?? 8765, _code.text.replaceAll(RegExp(r'\D'), ''));
      if (mounted) Navigator.pop(context);
      state.toast('${state.pcName} とつながりました');
    } catch (e) {
      setState(() => _error = describeError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.viewInsetsOf(context).bottom + 20),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('PC と接続', style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 6),
        Text('PC の Celestial で「スマホ連携」を開き、「接続用のコードを出す」を押してください。QR コードを読むと自動で入力されます。',
            style: TextStyle(color: p.muted, height: 1.5)),
        const SizedBox(height: 16),
        Row(children: [
          Expanded(flex: 3, child: TextField(controller: _host, decoration: const InputDecoration(labelText: 'PC のアドレス', hintText: '100.x.x.x'))),
          const SizedBox(width: 8),
          Expanded(child: TextField(controller: _port, keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: 'ポート'))),
        ]),
        const SizedBox(height: 10),
        TextField(
          controller: _code,
          keyboardType: TextInputType.number,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly, LengthLimitingTextInputFormatter(6)],
          style: const TextStyle(fontSize: 22, letterSpacing: 6, fontWeight: FontWeight.w700),
          decoration: const InputDecoration(labelText: '6桁のコード'),
          onSubmitted: (_) => _pair(),
        ),
        if (_error != null) Padding(padding: const EdgeInsets.only(top: 10), child: Text(_error!, style: TextStyle(color: p.bad))),
        const SizedBox(height: 16),
        FilledButton(
          onPressed: _busy ? null : _pair,
          child: _busy ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Text('接続する'),
        ),
      ]),
    );
  }
}

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  final _send = TextEditingController();

  @override
  void dispose() {
    _send.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    final s = state.settings;
    return Scaffold(
      appBar: AppBar(title: const Text('設定')),
      body: ListView(padding: const EdgeInsets.fromLTRB(14, 4, 14, 28), children: [
        if (!state.isDesktop)
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.fromLTRB(16, 6, 12, 6),
              leading: Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(color: p.accentSoft, borderRadius: BorderRadius.circular(12)),
                child: Icon(Icons.memory, color: context.colors.primary),
              ),
              title: const Text('AIモデル'),
              subtitle: Text(
                  state.settings.localModel.isEmpty ? '端末内のAIを PC から受け取る' : '使用中: ${state.settings.localModel}',
                  style: TextStyle(color: p.muted, fontSize: 12.5)),
              trailing: const Icon(Icons.chevron_right),
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ModelsPage(active: true))),
            ),
          ),
        SectionCard(
          title: 'PC との接続',
          trailing: const LinkBadge(),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            if (state.isDesktop)
              Text('この PC の Celestial(127.0.0.1:8765)を使っています。', style: TextStyle(color: p.muted))
            else if (!state.paired)
              Text('PC と接続すると、PC の AI 群やエージェントを使えるほか、会話の同期、PC の操作、ファイルのやり取りができます。', style: TextStyle(color: p.muted, height: 1.5))
            else
              Text('${state.pcName}(${s.host}:${s.port})\nこの端末: ${s.deviceName}', style: const TextStyle(height: 1.6)),
            if (state.linkError != null && !state.online) Padding(padding: const EdgeInsets.only(top: 6), child: Text(state.linkError!, style: TextStyle(color: p.bad))),
            const SizedBox(height: 12),
            Wrap(spacing: 8, runSpacing: 8, children: [
              if (!state.isDesktop) FilledButton.icon(onPressed: () => showPairSheet(context, host: s.host, port: s.port), icon: const Icon(Icons.qr_code_2), label: Text(state.paired ? '別の PC と接続' : 'PC と接続')),
              if (state.paired) OutlinedButton.icon(onPressed: state.connect, icon: const Icon(Icons.refresh, size: 18), label: const Text('再接続')),
              if (state.online) OutlinedButton.icon(onPressed: state.sync, icon: const Icon(Icons.sync, size: 18), label: const Text('今すぐ同期')),
              if (state.isDesktop)
                OutlinedButton.icon(
                  onPressed: () => PlatformBridge.openUrl('http://127.0.0.1:8765/'),
                  icon: const Icon(Icons.open_in_new, size: 18),
                  label: const Text('管理画面を開く'),
                ),
              if (state.paired && !state.isDesktop)
                TextButton(
                  onPressed: () async {
                    if (await confirm(context, title: '接続を解除しますか?', body: 'この端末の会話は残ります。もう一度つなぐには、PC で新しいコードを出してください。', ok: '解除', danger: true)) {
                      await state.unpair();
                    }
                  },
                  child: const Text('接続を解除'),
                ),
            ]),
          ]),
        ),
        const SizedBox(height: 12),
        const _PersonalCard(),
        if (state.online && !state.isDesktop) ...[
          const SizedBox(height: 12),
          SectionCard(
            title: 'PC に送る',
            subtitle: '文字や URL、クリップボードを PC に送ります',
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              TextField(controller: _send, minLines: 1, maxLines: 4, decoration: const InputDecoration(hintText: '送る文字や URL')),
              const SizedBox(height: 8),
              Wrap(spacing: 8, runSpacing: 8, children: [
                FilledButton.tonal(
                  onPressed: () {
                    if (_send.text.trim().isEmpty) return;
                    final text = _send.text.trim();
                    state.sendToPc(RegExp(r'^https?://').hasMatch(text) ? 'url' : 'text', text);
                    _send.clear();
                  },
                  child: const Text('送る'),
                ),
                OutlinedButton.icon(
                  onPressed: () async {
                    final data = await Clipboard.getData(Clipboard.kTextPlain);
                    if (data?.text?.isNotEmpty ?? false) {
                      await state.sendToPc('clipboard', data!.text!);
                    } else {
                      state.toast('クリップボードが空です');
                    }
                  },
                  icon: const Icon(Icons.content_paste, size: 18),
                  label: const Text('クリップボードを PC へ'),
                ),
              ]),
            ]),
          ),
        ],
        if (state.inbox.isNotEmpty) ...[
          const SizedBox(height: 12),
          SectionCard(
            title: 'PC から届いたもの',
            child: Column(children: [
              for (final item in state.inbox.take(20))
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  leading: Icon(item.kind == 'url' ? Icons.link : item.kind == 'clipboard' ? Icons.content_paste : Icons.notes, color: p.muted),
                  title: Text(item.text, maxLines: 3, overflow: TextOverflow.ellipsis),
                  subtitle: Text('${item.sender}・${item.at.hour.toString().padLeft(2, '0')}:${item.at.minute.toString().padLeft(2, '0')}', style: TextStyle(color: p.muted, fontSize: 12)),
                  onTap: item.kind == 'url' ? () => PlatformBridge.openUrl(item.text) : null,
                  trailing: IconButton(
                    tooltip: 'コピー',
                    icon: const Icon(Icons.content_copy, size: 18),
                    onPressed: () {
                      Clipboard.setData(ClipboardData(text: item.text));
                      state.toast('コピーしました');
                    },
                  ),
                ),
            ]),
          ),
        ],
        const SizedBox(height: 12),
        SectionCard(
          title: '表示',
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SegmentedButton<ThemeMode>(
              showSelectedIcon: false,
              segments: const [
                ButtonSegment(value: ThemeMode.system, label: Text('自動')),
                ButtonSegment(value: ThemeMode.light, label: Text('ライト')),
                ButtonSegment(value: ThemeMode.dark, label: Text('ダーク')),
              ],
              selected: {s.themeMode},
              onSelectionChanged: (v) => s.update((x) => x.themeMode = v.first),
            ),
            if (!state.isDesktop)
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('通知'),
                subtitle: const Text('アプリを閉じている間に回答や確認、PC からの送信が届いたら知らせます'),
                value: s.notifications,
                onChanged: (v) => s.update((x) => x.notifications = v),
              ),
          ]),
        ),
        const SizedBox(height: 12),
        SectionCard(
          title: 'このアプリについて',
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const _AppUpdate(),
            const SizedBox(height: 4),
            Text('小型AI群と管理AIによる、使うほど進化するローカルAI。会話・ファイル・操作の通信は Tailscale の中だけを通ります。', style: TextStyle(color: p.muted, height: 1.5)),
            const SizedBox(height: 8),
            if (!state.isDesktop) Text('端末内AI: llama.cpp b12319(${state.ai.variantLabel})', style: TextStyle(color: p.muted, fontSize: 12.5)),
            if (state.online && !state.isDesktop)
              TextButton(
                onPressed: () async {
                  if (await confirm(context, title: '会話を PC から取り直しますか?', body: 'PC にある会話をすべて受け取り直します。この端末だけにある未送信の変更は先に送ります。', ok: '取り直す')) {
                    await state.sync();
                    state.store.lastRev = 0;
                    await state.sync();
                    state.toast('取り直しました');
                  }
                },
                child: const Text('会話を PC から取り直す'),
              ),
          ]),
        ),
      ]),
    );
  }
}

/// あなたに合わせる: AIへの指示と、覚えていること。PC に置き、PC とスマホのどちらのAIも使う。
class _PersonalCard extends StatefulWidget {
  const _PersonalCard();

  @override
  State<_PersonalCard> createState() => _PersonalCardState();
}

class _PersonalCardState extends State<_PersonalCard> {
  final _instructions = TextEditingController();
  final _fact = TextEditingController();
  String _shown = '';
  bool _edited = false;
  bool _busy = false;

  @override
  void dispose() {
    _instructions.dispose();
    _fact.dispose();
    super.dispose();
  }

  Future<void> _save(AppState state, {String? instructions, List<String>? memories, required String done}) async {
    setState(() => _busy = true);
    try {
      await state.savePersonal(instructions: instructions, memories: memories);
      if (instructions != null) _edited = false;
      state.toast(done);
    } catch (e) {
      state.toast(describeError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    final s = state.settings;
    // ほかの端末で変わったら、編集中でなければ表示を合わせる
    if (!_edited && _shown != s.instructions) {
      _shown = s.instructions;
      _instructions.text = s.instructions;
    }
    final canEdit = state.online && !_busy;
    void addFact() {
      final fact = _fact.text.trim();
      if (fact.isEmpty || !canEdit) return;
      _fact.clear();
      _save(state, memories: [...s.memories, fact], done: '覚えました');
    }

    return SectionCard(
      title: 'あなたに合わせる',
      subtitle: 'PC とスマホのすべての会話で使われます',
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('AIへの指示', style: const TextStyle(fontWeight: FontWeight.w600)),
        const SizedBox(height: 4),
        Text('答え方の好みや、あなたについて知っておいてほしいことを書きます。', style: TextStyle(color: p.muted, fontSize: 12.5)),
        const SizedBox(height: 8),
        TextField(
          controller: _instructions,
          enabled: state.online,
          minLines: 2,
          maxLines: 6,
          maxLength: 1000,
          decoration: const InputDecoration(hintText: '例: 敬語は使わず、結論から短く答えて。私はエンジニアです。'),
          onChanged: (_) => setState(() => _edited = true),
        ),
        Align(
          alignment: Alignment.centerRight,
          child: FilledButton.tonal(
            onPressed: canEdit && _edited ? () => _save(state, instructions: _instructions.text, done: 'AIへの指示を保存しました') : null,
            child: const Text('保存'),
          ),
        ),
        const SizedBox(height: 12),
        Text('覚えていること(${s.memories.length}件)', style: const TextStyle(fontWeight: FontWeight.w600)),
        const SizedBox(height: 4),
        Text('会話で「覚えて: 〜」と頼むと増えます。「忘れて: 〜」「何を覚えてる?」も使えます。', style: TextStyle(color: p.muted, fontSize: 12.5, height: 1.5)),
        const SizedBox(height: 8),
        if (s.memories.isEmpty)
          Text('まだありません', style: TextStyle(color: p.muted))
        else
          for (final m in s.memories)
            Container(
              margin: const EdgeInsets.only(bottom: 6),
              padding: const EdgeInsets.fromLTRB(12, 4, 4, 4),
              decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(10), border: Border.all(color: p.line)),
              child: Row(children: [
                Expanded(child: Text(m, style: const TextStyle(height: 1.4))),
                IconButton(
                  tooltip: '消す',
                  visualDensity: VisualDensity.compact,
                  icon: Icon(Icons.close, size: 18, color: p.muted),
                  onPressed: canEdit ? () => _save(state, memories: s.memories.where((x) => x != m).toList(), done: '消しました') : null,
                ),
              ]),
            ),
        const SizedBox(height: 4),
        Row(children: [
          Expanded(
            child: TextField(
              controller: _fact,
              enabled: state.online,
              maxLength: 200,
              decoration: const InputDecoration(hintText: '例: 私の名前は佐藤です', counterText: ''),
              onSubmitted: (_) => addFact(),
            ),
          ),
          const SizedBox(width: 8),
          FilledButton.tonal(onPressed: canEdit ? addFact : null, child: const Text('覚える')),
        ]),
        if (!state.online) ...[
          const SizedBox(height: 8),
          Text('PC とつながっているときに変えられます。今は、前に PC から受け取った内容をスマホの中のAIが使っています。',
              style: TextStyle(color: p.muted, fontSize: 12.5, height: 1.5)),
        ],
      ]),
    );
  }
}

/// 起動のきっかけ(QR コードのリンクや、ほかのアプリからの共有)を扱う。
void handleLaunch(BuildContext context, Map<String, dynamic> data, void Function(String text) onShare) {
  final link = data['link'] as String?;
  if (link != null && link.startsWith('celestial://pair')) {
    final uri = Uri.parse(link);
    showPairSheet(context, host: uri.queryParameters['h'] ?? '', port: int.tryParse(uri.queryParameters['p'] ?? '') ?? 8765, code: uri.queryParameters['c'] ?? '');
    return;
  }
  final text = data['text'] as String?;
  if (text != null && text.trim().isNotEmpty) onShare(text.trim());
}

