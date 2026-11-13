import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_state.dart';
import '../core/models.dart';
import '../platform.dart';
import '../theme.dart';
import 'common.dart';
import 'markdown.dart';
import 'voice_button.dart';
import 'voice_chat.dart';

/// 添付できる文字のファイル(PC の画面と同じ種類)。
final _textFile = RegExp(
    r'\.(txt|md|markdown|csv|tsv|json|jsonl|py|js|mjs|ts|tsx|jsx|html?|css|scss|log|xml|ya?ml|toml|ini|cfg|conf|c|cc|cpp|h|hpp|cs|java|kt|go|rs|rb|php|sql|sh|ps1|bat|cmd|dart|swift|r|tex|srt|vtt)$');
const _textLimit = 30000;

/// 依頼の本文に添えた文字のファイル(PC の画面と同じ書式)。
final _attachedFile = RegExp(r'\n\n\[添付ファイル: ([^\]\n]+)\]\n````\n([\s\S]*?)\n````');

const _examples = [
  ('コード', 'Pythonでリストの重複を取り除く関数を書いて'),
  ('計算・推論', 'りんごが3個で450円のとき、7個ではいくら?'),
  ('翻訳', '次を英語に翻訳して: 明日の会議は10時からに変更になりました'),
  ('文章作成', '取引先の田中さんへ、打ち合わせのお礼メールを短く書いて'),
];

class ChatPage extends StatefulWidget {
  const ChatPage({super.key, this.prefill});
  final ValueNotifier<String?>? prefill;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final _scaffold = GlobalKey<ScaffoldState>();
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _focus = FocusNode();
  String? _current;
  final List<(String, Uint8List)> _images = [];
  final List<(String, String)> _files = []; // 添付する文字のファイル (名前, 中身)
  bool? _usePc;
  bool _stick = true;

  @override
  void initState() {
    super.initState();
    widget.prefill?.addListener(_onPrefill);
    _scroll.addListener(() {
      if (!_scroll.hasClients) return;
      _stick = _scroll.position.maxScrollExtent - _scroll.position.pixels < 120;
    });
  }

  @override
  void dispose() {
    widget.prefill?.removeListener(_onPrefill);
    _input.dispose();
    _scroll.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _onPrefill() {
    final text = widget.prefill?.value;
    if (text == null) return;
    setState(() {
      _current = null;
      _input.text = text;
    });
    widget.prefill!.value = null;
    _focus.requestFocus();
  }

  void _scrollToEnd({bool force = false}) {
    if (!force && !_stick) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  bool _effectiveUsePc(AppState state) {
    if (state.isDesktop) return true;
    if (!state.online) return false;
    if (!state.ai.supported || state.ai.installed.isEmpty) return true;
    return _usePc ?? state.settings.preferPc;
  }

  Future<void> _send(AppState state) async {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    final usePc = _effectiveUsePc(state);
    if (usePc && !state.online) {
      state.toast('PC につながっていません');
      return;
    }
    try {
      final full = text + [for (final f in _files) '\n\n[添付ファイル: ${f.$1}]\n````\n${f.$2.trimRight()}\n````'].join();
      final id = await state.send(_current, full, usePc: usePc, agent: usePc && state.settings.agent, images: [for (final i in _images) i.$2]);
      setState(() {
        _current = id;
        _input.clear();
        _images.clear();
        _files.clear();
      });
      _scrollToEnd(force: true);
    } catch (e) {
      state.toast(e.toString().replaceFirst('Bad state: ', ''));
    }
  }

  Future<void> _attach(AppState state) async {
    final picked = await PlatformBridge.pickFile();
    if (picked == null) return;
    final lower = picked.name.toLowerCase();
    if (['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'].any(lower.endsWith)) {
      if (picked.size > 8 * 1024 * 1024) {
        state.toast('8MBを超える画像は添付できません');
        return;
      }
      final bytes = await File(picked.path).readAsBytes();
      setState(() => _images.add((picked.name, bytes)));
      return;
    }
    if (!_textFile.hasMatch(lower)) {
      state.toast('画像か、文字のファイル(txt・md・csv・コードなど)を選んでください');
      return;
    }
    if (picked.size > 2 * 1024 * 1024) {
      state.toast('2MBを超えるファイルは添付できません');
      return;
    }
    var text = utf8.decode(await File(picked.path).readAsBytes(), allowMalformed: true);
    if (text.length > _textLimit) {
      text = '${text.substring(0, _textLimit)}\n…(長いため、ここで切りました)';
      state.toast('長いため、先頭の $_textLimit 文字だけを添えます');
    }
    setState(() => _files.add((picked.name, text)));
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final wide = MediaQuery.sizeOf(context).width >= 840;
    final conv = _current != null ? state.store.conversations[_current] : null;
    if (conv != null && conv.deleted) _current = null;
    final list = _ConversationList(
      current: _current,
      onOpen: (id) {
        setState(() => _current = id);
        if (!wide) Navigator.of(context).maybePop();
        _scrollToEnd(force: true);
      },
      onNew: () {
        setState(() => _current = null);
        if (!wide) Navigator.of(context).maybePop();
        _focus.requestFocus();
      },
    );
    final chat = _ChatBody(
      conversationId: _current,
      scroll: _scroll,
      onScrolled: () => _scrollToEnd(),
      composer: _composer(context, state),
      onExample: (text) {
        _input.text = text;
        _focus.requestFocus();
      },
    );
    final title = conv == null ? '新しい会話' : (conv.title.isEmpty ? '無題の会話' : conv.title);
    return Scaffold(
      key: _scaffold,
      drawer: wide ? null : Drawer(width: 320, child: SafeArea(child: list)),
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: wide ? null : IconButton(tooltip: '会話の一覧', icon: const Icon(Icons.menu), onPressed: () => _scaffold.currentState?.openDrawer()),
        title: Text(title, overflow: TextOverflow.ellipsis),
        actions: [
          const Padding(padding: EdgeInsets.only(right: 4), child: Center(child: LinkBadge())),
          if (PlatformBridge.voiceSupported)
            IconButton(
              tooltip: '音声で会話(手を使わずに話し続ける)',
              icon: const Icon(Icons.record_voice_over_outlined),
              onPressed: () => showVoiceChat(context, conversationId: _current, usePc: _effectiveUsePc(state),
                  onConversation: (id) => setState(() => _current = id)),
            ),
          IconButton(tooltip: '新しい会話', icon: const Icon(Icons.edit_note), onPressed: () => setState(() => _current = null)),
          if (conv != null)
            PopupMenuButton<String>(
              tooltip: 'その他',
              onSelected: (v) async {
                if (v == 'rename') {
                  final t = await askText(context, title: '会話の名前', initial: conv.title);
                  if (t != null && t.trim().isNotEmpty) state.renameConversation(conv.id, t.trim());
                } else if (v == 'pin') {
                  state.pinConversation(conv.id, !conv.pinned);
                } else if (v == 'copy') {
                  final text = state.store.messagesOf(conv.id).map((m) => '${m.isUser ? 'あなた' : 'Celestial'}: ${m.content}').join('\n\n');
                  await Clipboard.setData(ClipboardData(text: text));
                  state.toast('会話をコピーしました');
                } else if (v == 'delete') {
                  if (context.mounted && await confirm(context, title: 'この会話を削除しますか?', body: 'PC とほかの端末からも消えます。', ok: '削除', danger: true)) {
                    state.deleteConversation(conv.id);
                    setState(() => _current = null);
                  }
                }
              },
              itemBuilder: (_) => [
                const PopupMenuItem(value: 'rename', child: Text('名前を変える')),
                PopupMenuItem(value: 'pin', child: Text(conv.pinned ? 'ピン留めを外す' : 'ピン留め')),
                const PopupMenuItem(value: 'copy', child: Text('会話をコピー')),
                const PopupMenuItem(value: 'delete', child: Text('削除')),
              ],
            ),
        ],
      ),
      body: wide
          ? Row(children: [
              SizedBox(width: 300, child: DecoratedBox(decoration: BoxDecoration(border: Border(right: BorderSide(color: context.palette.line))), child: list)),
              Expanded(child: chat),
            ])
          : chat,
    );
  }

  Widget _composer(BuildContext context, AppState state) {
    final usePc = _effectiveUsePc(state);
    final busy = _current != null && state.runs.containsKey(_current);
    final p = context.palette;
    final canPhone = state.ai.supported;
    final vision = state.pcState['vision_model'] != null;
    return SafeArea(
      top: false,
      child: Container(
        margin: const EdgeInsets.fromLTRB(12, 4, 12, 10),
        padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
        decoration: BoxDecoration(
          color: context.colors.surfaceContainerLow,
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: p.line),
        ),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          if (_files.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Wrap(spacing: 6, runSpacing: 6, children: [
                for (var i = 0; i < _files.length; i++)
                  InputChip(
                    avatar: const Icon(Icons.description_outlined, size: 16),
                    label: Text('${_files[i].$1}(${_files[i].$2.length} 文字)', style: const TextStyle(fontSize: 12)),
                    visualDensity: VisualDensity.compact,
                    onDeleted: () => setState(() => _files.removeAt(i)),
                  ),
              ]),
            ),
          if (_images.isNotEmpty)
            SizedBox(
              height: 64,
              child: ListView(scrollDirection: Axis.horizontal, children: [
                for (var i = 0; i < _images.length; i++)
                  Padding(
                    padding: const EdgeInsets.only(right: 8, bottom: 6),
                    child: Stack(children: [
                      ClipRRect(borderRadius: BorderRadius.circular(10), child: Image.memory(_images[i].$2, width: 58, height: 58, fit: BoxFit.cover)),
                      Positioned(
                        right: 0,
                        top: 0,
                        child: InkWell(
                          onTap: () => setState(() => _images.removeAt(i)),
                          child: Container(
                            decoration: const BoxDecoration(color: Colors.black54, shape: BoxShape.circle),
                            padding: const EdgeInsets.all(2),
                            child: const Icon(Icons.close, size: 14, color: Colors.white),
                          ),
                        ),
                      ),
                    ]),
                  ),
              ]),
            ),
          TextField(
            controller: _input,
            focusNode: _focus,
            minLines: 1,
            maxLines: 6,
            textInputAction: TextInputAction.newline,
            decoration: InputDecoration(
              hintText: usePc ? (state.settings.agent ? 'PC のエージェントに頼む' : 'PC の AI に聞く') : 'スマホ内の AI に聞く',
              filled: false,
              border: InputBorder.none,
              enabledBorder: InputBorder.none,
              focusedBorder: InputBorder.none,
              contentPadding: const EdgeInsets.symmetric(vertical: 6),
              suffixIcon: PlatformBridge.voiceSupported ? VoiceButton(controller: _input) : null,
            ),
          ),
          Row(children: [
            if (!state.isDesktop)
              SegmentedButton<bool>(
                style: SegmentedButton.styleFrom(visualDensity: VisualDensity.compact, textStyle: const TextStyle(fontSize: 12.5)),
                showSelectedIcon: false,
                segments: [
                  ButtonSegment(value: false, enabled: canPhone, icon: const Icon(Icons.smartphone, size: 16), label: const Text('スマホ')),
                  ButtonSegment(value: true, enabled: state.online, icon: const Icon(Icons.computer, size: 16), label: const Text('PC')),
                ],
                selected: {usePc},
                onSelectionChanged: (s) => setState(() => _usePc = s.first),
              ),
            if (usePc) ...[
              const SizedBox(width: 6),
              FilterChip(
                visualDensity: VisualDensity.compact,
                avatar: Icon(Icons.handyman_outlined, size: 16, color: state.settings.agent ? context.colors.primary : p.muted),
                label: const Text('エージェント'),
                selected: state.settings.agent,
                onSelected: (v) => state.settings.update((s) => s.agent = v),
              ),
              if (vision && !state.isDesktop)
                IconButton(tooltip: '画像・ファイルを添付', visualDensity: VisualDensity.compact, icon: const Icon(Icons.attach_file), onPressed: () => _attach(state)),
            ],
            const Spacer(),
            busy
                ? IconButton.filledTonal(tooltip: '停止', onPressed: () => state.stop(_current!), icon: const Icon(Icons.stop_rounded))
                : IconButton.filled(tooltip: '送信', onPressed: () => _send(state), icon: const Icon(Icons.arrow_upward_rounded)),
          ]),
        ]),
      ),
    );
  }
}

class _ChatBody extends StatelessWidget {
  const _ChatBody({required this.conversationId, required this.scroll, required this.onScrolled, required this.composer, required this.onExample});
  final String? conversationId;
  final ScrollController scroll;
  final VoidCallback onScrolled;
  final Widget composer;
  final ValueChanged<String> onExample;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final messages = conversationId == null ? <Message>[] : state.store.messagesOf(conversationId!);
    final view = conversationId == null ? null : state.runs[conversationId];
    final trailing = view != null && (view.assistantId == null || !messages.any((m) => m.id == view.assistantId));
    final approvals = state.globalApprovals.where((a) => a.conversationId == null || a.conversationId == conversationId || conversationId == null).toList();
    onScrolled();
    return Column(children: [
      if (approvals.isNotEmpty)
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 4, 12, 4),
          child: Column(children: [for (final a in approvals) _ApprovalCard(approval: a, note: '別の端末からの依頼')]),
        ),
      Expanded(
        child: messages.isEmpty && view == null
            ? _Welcome(onExample: onExample)
            : ListView.builder(
                controller: scroll,
                padding: const EdgeInsets.fromLTRB(14, 8, 14, 16),
                itemCount: messages.length + (trailing ? 1 : 0),
                itemBuilder: (context, i) {
                  if (i >= messages.length) return _AssistantTile(message: null, view: view);
                  final m = messages[i];
                  if (m.isUser) return _UserTile(message: m, onEdit: onExample);
                  return _AssistantTile(message: m, view: view != null && view.assistantId == m.id ? view : null);
                },
              ),
      ),
      composer,
    ]);
  }
}

class _Welcome extends StatelessWidget {
  const _Welcome({required this.onExample});
  final ValueChanged<String> onExample;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    return ListView(padding: const EdgeInsets.fromLTRB(20, 32, 20, 20), children: [
      const Center(child: CelestialMark(size: 44)),
      const SizedBox(height: 14),
      Text('何をしましょうか', textAlign: TextAlign.center, style: Theme.of(context).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w700)),
      const SizedBox(height: 8),
      Text(
        state.isDesktop
            ? '管理AIが依頼を見て、得意な小型AIに振り分けます。会話はスマホと同期されます。'
            : state.online
                ? 'PC の AI 群に頼むか、スマホの中だけで動く AI に聞けます。会話は PC と同期されます。'
                : 'いまは PC につながっていません。スマホの中だけで動く AI に聞けます。',
        textAlign: TextAlign.center,
        style: TextStyle(color: context.palette.muted, height: 1.6),
      ),
      const SizedBox(height: 20),
      Wrap(spacing: 8, runSpacing: 8, alignment: WrapAlignment.center, children: [
        for (final (kind, text) in _examples)
          ActionChip(
            avatar: Icon(Icons.north_east, size: 14, color: context.palette.muted),
            label: Text('$kind: ${text.length > 18 ? '${text.substring(0, 18)}…' : text}'),
            onPressed: () => onExample(text),
          ),
      ]),
    ]);
  }
}

class _UserTile extends StatelessWidget {
  const _UserTile({required this.message, required this.onEdit});
  final Message message;
  final ValueChanged<String> onEdit; // 入力欄に戻して、直して送り直す

  void _menu(BuildContext context, String text) {
    final state = AppScope.read(context);
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheet) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: const Icon(Icons.edit_outlined),
            title: const Text('入力欄に戻して直す'),
            subtitle: const Text('直してから送ると、新しい依頼として答えます'),
            onTap: () {
              Navigator.pop(sheet);
              onEdit(text.trim());
            },
          ),
          ListTile(
            leading: const Icon(Icons.content_copy),
            title: const Text('コピー'),
            onTap: () async {
              Navigator.pop(sheet);
              await Clipboard.setData(ClipboardData(text: message.content));
              state.toast('コピーしました');
            },
          ),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final images = (message.meta['images'] as num?)?.toInt() ?? 0;
    // 添付した文字のファイルは本文から外して、名前だけを並べる(押すと中身を見られる)
    final files = <(String, String)>[];
    final text = message.content.replaceAllMapped(_attachedFile, (m) {
      files.add((m[1]!, m[2]!));
      return '';
    });
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Align(
        alignment: Alignment.centerRight,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.82),
          child: Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
            if (images > 0) Padding(padding: const EdgeInsets.only(bottom: 4), child: Pill('画像 $images 枚', icon: Icons.image_outlined)),
            for (final f in files)
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: InkWell(
                  borderRadius: BorderRadius.circular(999),
                  onTap: () => _showFile(context, f.$1, f.$2),
                  child: Pill('添付: ${f.$1}', icon: Icons.description_outlined),
                ),
              ),
            GestureDetector(
              onLongPress: () => _menu(context, text),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                decoration: BoxDecoration(
                  color: context.palette.userBubble,
                  borderRadius: const BorderRadius.only(
                      topLeft: Radius.circular(18), topRight: Radius.circular(18), bottomLeft: Radius.circular(18), bottomRight: Radius.circular(6)),
                ),
                child: Text(text, style: const TextStyle(fontSize: 15, height: 1.5)),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: IconButton(
                tooltip: '入力欄に戻して直す',
                visualDensity: VisualDensity.compact,
                iconSize: 16,
                color: context.palette.muted,
                onPressed: () => onEdit(text.trim()),
                icon: const Icon(Icons.edit_outlined),
              ),
            ),
          ]),
        ),
      ),
    );
  }

  void _showFile(BuildContext context, String name, String body) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: SizedBox(
          height: MediaQuery.sizeOf(context).height * 0.7,
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Padding(padding: const EdgeInsets.fromLTRB(20, 0, 20, 8), child: Text(name, style: Theme.of(context).textTheme.titleMedium)),
            Expanded(
              child: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
                child: SelectableText(body, style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.45)),
              ),
            ),
          ]),
        ),
      ),
    );
  }
}

class _AssistantTile extends StatelessWidget {
  const _AssistantTile({required this.message, required this.view});
  final Message? message;
  final RunView? view;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final m = message;
    final live = view;
    final text = live != null && live.text.isNotEmpty ? live.text : (m?.content ?? '');
    final running = live != null || (m?.running ?? false);
    final error = m?.status == 'error' || live?.error != null;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (running)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Row(children: [
              SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2, color: context.colors.primary)),
              const SizedBox(width: 8),
              Expanded(child: Text(live?.stage.isNotEmpty == true ? live!.stage : '応答中…', style: TextStyle(color: p.muted, fontSize: 13), overflow: TextOverflow.ellipsis)),
            ]),
          ),
        if (live != null && live.steps.isNotEmpty)
          Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(12)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              for (final s in live.steps)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 2),
                  child: Text.rich(TextSpan(children: [
                    TextSpan(text: '${s.n}. ${s.label}  ', style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                    TextSpan(text: s.detail, style: TextStyle(fontFamily: 'monospace', fontSize: 12, color: p.muted)),
                  ])),
                ),
            ]),
          ),
        if (live != null)
          for (final a in live.approvals) _ApprovalCard(approval: a),
        if (text.isNotEmpty)
          error
              ? Text(live?.error ?? text, style: TextStyle(color: p.bad))
              : MarkdownView(text, selectable: !running),
        if (live?.error != null && text.isEmpty) Text(live!.error!, style: TextStyle(color: p.bad)),
        if (m != null && !running && (m.meta['changes'] as List?)?.isNotEmpty == true) _ChangesView(changes: m.meta['changes'] as List),
        if (m != null && !running && m.meta['memory_suggestion'] is String) _MemorySuggestion(message: m),
        if (m != null && !running) _MetaRow(message: m),
      ]),
    );
  }
}

/// 「覚えますか」の提案(発言に利用者についての事実があると、PC の判断の機能が見分けて付ける)。
class _MemorySuggestion extends StatelessWidget {
  const _MemorySuggestion({required this.message});
  final Message message;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
      decoration: BoxDecoration(borderRadius: BorderRadius.circular(12), border: Border.all(color: p.line)),
      child: Row(children: [
        Icon(Icons.bookmark_add_outlined, size: 18, color: context.colors.primary),
        const SizedBox(width: 8),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('覚えますか', style: TextStyle(color: p.muted, fontSize: 12)),
            Text(message.meta['memory_suggestion'] as String, style: const TextStyle(fontWeight: FontWeight.w600, height: 1.4)),
          ]),
        ),
        TextButton(onPressed: state.online ? () => state.answerMemorySuggestion(message, true) : null, child: const Text('覚える')),
        TextButton(onPressed: state.online ? () => state.answerMemorySuggestion(message, false) : null, child: const Text('いいえ')),
      ]),
    );
  }
}

/// エージェントが変えたファイル(Claude Code のように、ファイルごとの増減と差分を見せる)。
class _ChangesView extends StatelessWidget {
  const _ChangesView({required this.changes});
  final List changes;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final items = [for (final c in changes) Map<String, dynamic>.from(c as Map)];
    final added = items.fold<int>(0, (s, c) => s + ((c['added'] as num?)?.toInt() ?? 0));
    final removed = items.fold<int>(0, (s, c) => s + ((c['removed'] as num?)?.toInt() ?? 0));
    const good = Color(0xFF2E9D6C), bad = Color(0xFFC4473A);
    const labels = {'added': '新規', 'modified': '変更', 'deleted': '削除'};
    return Container(
      margin: const EdgeInsets.only(top: 8),
      decoration: BoxDecoration(borderRadius: BorderRadius.circular(12), border: Border.all(color: p.line)),
      child: ExpansionTile(
        shape: const Border(),
        initiallyExpanded: items.length <= 3,
        tilePadding: const EdgeInsets.symmetric(horizontal: 12),
        title: Row(children: [
          Text('変更したファイル ${items.length}件', style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13.5)),
          const SizedBox(width: 8),
          Text('+$added', style: const TextStyle(color: good, fontSize: 12.5)),
          const SizedBox(width: 6),
          Text('−$removed', style: const TextStyle(color: bad, fontSize: 12.5)),
        ]),
        childrenPadding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
        children: [
          for (final c in items)
            ExpansionTile(
              shape: const Border(),
              dense: true,
              tilePadding: const EdgeInsets.symmetric(horizontal: 4),
              title: Row(children: [
                Pill(labels[c['status']] ?? '${c['status']}', tone: c['status'] == 'deleted' ? Tone.bad : c['status'] == 'added' ? Tone.good : Tone.neutral),
                const SizedBox(width: 6),
                Expanded(child: Text(c['path'] as String? ?? '', style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5), overflow: TextOverflow.ellipsis)),
                if (c['binary'] != true) ...[
                  Text('+${c['added']}', style: const TextStyle(color: good, fontSize: 12)),
                  const SizedBox(width: 4),
                  Text('−${c['removed']}', style: const TextStyle(color: bad, fontSize: 12)),
                ],
              ]),
              children: [
                if ((c['diff'] as String? ?? '').isNotEmpty)
                  Container(
                    width: double.infinity,
                    decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(8)),
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    child: SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        for (final line in (c['diff'] as String).split('\n').take(400))
                          Container(
                            color: line.startsWith('+') ? good.withValues(alpha: 0.13) : line.startsWith('-') ? bad.withValues(alpha: 0.13) : null,
                            padding: const EdgeInsets.symmetric(horizontal: 8),
                            child: Text(line, style: TextStyle(fontFamily: 'monospace', fontSize: 11.5, height: 1.45, color: line.startsWith('@@') ? p.muted : null)),
                          ),
                      ]),
                    ),
                  ),
              ],
            ),
        ],
      ),
    );
  }
}

class _MetaRow extends StatelessWidget {
  const _MetaRow({required this.message});
  final Message message;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final meta = message.meta;
    final tools = (meta['tool_calls'] as List?) ?? const [];
    final local = message.fromLocalAi;
    final escalated = meta['escalated'] is Map ? meta['escalated'] as Map : null;
    final sources = [for (final s in (meta['sources'] as List?) ?? const []) if (s is Map && s['url'] is String) s];
    final pills = <Widget>[
      if (local) const Pill('スマホ内のAI', tone: Tone.accent, icon: Icons.smartphone),
      if (!local && meta['model'] != null) Pill(meta['model'] as String, icon: Icons.computer),
      if (local) Pill((meta['model'] as String).substring(6)),
      if (meta['route_source'] == 'cache') const Pill('キャッシュ', tone: Tone.gold),
      if (meta['route_source'] == 'learned') const Pill('学習済みの振り分け', tone: Tone.gold),
      if (meta['role'] != null) Pill(state.roleLabel(meta['role']), tone: Tone.accent),
      if (tools.isNotEmpty) Pill('道具 ${tools.length} 回', icon: Icons.handyman_outlined),
      if (meta['examples_used'] is num && (meta['examples_used'] as num) > 0) Pill('手本 ${meta['examples_used']} 件', tone: Tone.gold),
      if (meta['tokens_per_s'] != null) Pill('${meta['tokens_per_s']} トークン/秒'),
      if (meta['latency_s'] != null) Pill('${(meta['latency_s'] as num).toStringAsFixed(1)} 秒'),
      if (escalated != null)
        Pill(
            escalated['reason'] == 'lang'
                ? '日本語で答え直し'
                : escalated['from'] == escalated['to']
                    ? '資料で確かめて答え直し'
                    : '答え直し ${escalated['from']} → ${escalated['to']}',
            icon: Icons.swap_horiz),
      for (final s in sources)
        InkWell(
          borderRadius: BorderRadius.circular(999),
          onTap: () => PlatformBridge.openUrl(s['url'] as String),
          child: Pill('出典: ${s['title']}', tone: Tone.accent, icon: Icons.menu_book_outlined),
        ),
    ];
    final feedback = (meta['feedback'] as num?)?.toInt();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Wrap(spacing: 6, runSpacing: 6, crossAxisAlignment: WrapCrossAlignment.center, children: [
        ...pills,
        _IconAction(icon: Icons.content_copy, tooltip: 'コピー', onTap: () async {
          await Clipboard.setData(ClipboardData(text: message.content));
          state.toast('コピーしました');
        }),
        if (state.online && meta['status'] == 'done' && message.content.trim().isNotEmpty)
          PopupMenuButton<String>(
            tooltip: '文書で保存',
            icon: Icon(Icons.save_alt_outlined, size: 17, color: context.palette.muted),
            style: IconButton.styleFrom(visualDensity: VisualDensity.compact),
            onSelected: (format) => state.exportMessage(message, format),
            itemBuilder: (context) => [
              const PopupMenuItem(value: 'docx', child: ListTile(dense: true, leading: Icon(Icons.description_outlined), title: Text('Word 文書で保存'))),
              if (hasTable(message.content))
                const PopupMenuItem(value: 'xlsx', child: ListTile(dense: true, leading: Icon(Icons.table_chart_outlined), title: Text('Excel で保存(表)'))),
              const PopupMenuItem(value: 'pptx', child: ListTile(dense: true, leading: Icon(Icons.slideshow_outlined), title: Text('PowerPoint で保存'))),
            ],
          ),
        if (!local && state.online && meta['status'] == 'done') ...[
          _IconAction(icon: feedback == 1 ? Icons.thumb_up : Icons.thumb_up_outlined, tooltip: '良い', active: feedback == 1, onTap: () => state.feedback(message, true)),
          _IconAction(icon: feedback == -1 ? Icons.thumb_down : Icons.thumb_down_outlined, tooltip: '悪い', active: feedback == -1, onTap: () => state.feedback(message, false)),
        ],
        if (!local && state.online && const ['done', 'error', 'cancelled'].contains(meta['status']))
          _IconAction(icon: Icons.refresh, tooltip: 'もう一度(検証つき)', onTap: () => state.regenerate(message)),
        if (Platform.isAndroid) ...[
          // 読み上げ中は「止める」に変わる(読み終わると自動で戻る)
          ValueListenableBuilder<String?>(
            valueListenable: PlatformBridge.speakingId,
            builder: (context, speaking, _) => speaking == message.id
                ? _IconAction(icon: Icons.stop_circle_outlined, tooltip: '読み上げを止める', active: true, onTap: PlatformBridge.stopSpeaking)
                : _IconAction(icon: Icons.volume_up_outlined, tooltip: '読み上げ', onTap: () => PlatformBridge.speak(message.content, id: message.id)),
          ),
          _IconAction(icon: Icons.ios_share, tooltip: '共有', onTap: () => PlatformBridge.shareText(message.content)),
        ],
        if (local && state.online)
          TextButton.icon(
            style: TextButton.styleFrom(visualDensity: VisualDensity.compact),
            onPressed: () {
              final msgs = state.store.messagesOf(message.conversationId);
              final i = msgs.indexWhere((x) => x.id == message.id);
              final question = msgs.take(i < 0 ? msgs.length : i).lastWhere((x) => x.isUser, orElse: () => message).content;
              state.send(message.conversationId, question, usePc: true);
            },
            icon: const Icon(Icons.computer, size: 16),
            label: const Text('PC で答え直す', style: TextStyle(fontSize: 12.5)),
          ),
      ]),
    );
  }
}

class _IconAction extends StatelessWidget {
  const _IconAction({required this.icon, required this.tooltip, required this.onTap, this.active = false});
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;
  final bool active;

  @override
  Widget build(BuildContext context) => IconButton(
        tooltip: tooltip,
        visualDensity: VisualDensity.compact,
        iconSize: 17,
        color: active ? context.colors.primary : context.palette.muted,
        onPressed: onTap,
        icon: Icon(icon),
      );
}

class _ApprovalCard extends StatelessWidget {
  const _ApprovalCard({required this.approval, this.note});
  final Approval approval;
  final String? note;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.read(context);
    final p = context.palette;
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: p.goldSoft, borderRadius: BorderRadius.circular(14), border: Border.all(color: p.gold.withValues(alpha: 0.6))),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Icon(Icons.verified_user_outlined, size: 18, color: p.gold),
          const SizedBox(width: 8),
          Expanded(child: Text('確認: ${approval.label}${note != null ? '($note)' : ''}', style: const TextStyle(fontWeight: FontWeight.w700))),
        ]),
        if (approval.summary.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(approval.summary, style: TextStyle(color: p.muted))),
        if (approval.preview.isNotEmpty)
          Container(
            margin: const EdgeInsets.only(top: 8),
            padding: const EdgeInsets.all(10),
            constraints: const BoxConstraints(maxHeight: 180),
            decoration: BoxDecoration(color: context.colors.surface, borderRadius: BorderRadius.circular(10)),
            child: SingleChildScrollView(child: Text(approval.preview, style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5))),
          ),
        const SizedBox(height: 10),
        Wrap(spacing: 8, runSpacing: 6, children: [
          FilledButton(onPressed: () => state.approve(approval, true), child: const Text('許可')),
          OutlinedButton(onPressed: () => state.approve(approval, true, all: true), child: const Text('この依頼の間は許可')),
          OutlinedButton(onPressed: () => state.approve(approval, false), child: const Text('拒否')),
        ]),
      ]),
    );
  }
}

class _ConversationList extends StatefulWidget {
  const _ConversationList({required this.current, required this.onOpen, required this.onNew});
  final String? current;
  final ValueChanged<String> onOpen;
  final VoidCallback onNew;

  @override
  State<_ConversationList> createState() => _ConversationListState();
}

class _ConversationListState extends State<_ConversationList> {
  String _query = '';

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    final items = state.store.listConversations(query: _query);
    return Column(children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 8),
        child: Column(children: [
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(onPressed: widget.onNew, icon: const Icon(Icons.add), label: const Text('新しい会話')),
          ),
          const SizedBox(height: 8),
          TextField(
            decoration: const InputDecoration(hintText: '会話を検索', prefixIcon: Icon(Icons.search, size: 20), isDense: true),
            onChanged: (v) => setState(() => _query = v),
          ),
        ]),
      ),
      Expanded(
        child: items.isEmpty
            ? Center(child: Text(_query.isEmpty ? 'まだ会話がありません' : '見つかりませんでした', style: TextStyle(color: p.muted)))
            : ListView.builder(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                itemCount: items.length,
                itemBuilder: (context, i) {
                  final c = items[i];
                  final selected = c.id == widget.current;
                  final running = state.runs.containsKey(c.id);
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 2),
                    child: ListTile(
                      dense: true,
                      selected: selected,
                      selectedTileColor: p.accentSoft,
                      onTap: () => widget.onOpen(c.id),
                      title: Row(children: [
                        if (c.pinned) Padding(padding: const EdgeInsets.only(right: 6), child: Icon(Icons.push_pin, size: 13, color: p.gold)),
                        Expanded(child: Text(c.title.isEmpty ? '無題の会話' : c.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w600))),
                        if (running) SizedBox(width: 12, height: 12, child: CircularProgressIndicator(strokeWidth: 1.6, color: context.colors.primary)),
                        const SizedBox(width: 6),
                        Text(shortTime(c.updated), style: TextStyle(fontSize: 11.5, color: p.muted)),
                      ]),
                      subtitle: Text(state.store.previewOf(c.id), maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(color: p.muted, fontSize: 12.5)),
                    ),
                  );
                },
              ),
      ),
      if (!state.isDesktop)
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 4, 8, 8),
          child: Row(children: [
            Icon(state.online ? Icons.cloud_done_outlined : Icons.cloud_off_outlined, size: 16, color: p.muted),
            const SizedBox(width: 6),
            Expanded(
              child: Text(
                state.store.pendingCount > 0 ? '未送信 ${state.store.pendingCount} 件' : (state.online ? 'PC と同期済み' : 'PC に未接続'),
                style: TextStyle(fontSize: 12, color: p.muted),
              ),
            ),
            TextButton(onPressed: state.online ? state.sync : state.connect, child: Text(state.online ? '同期' : '再接続')),
          ]),
        ),
    ]);
  }
}
