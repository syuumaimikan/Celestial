import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../net/pc_client.dart';
import '../platform.dart';
import '../theme.dart';
import 'common.dart';
import 'flows_page.dart';

/// 触れた位置(画面上)を、拡大前の表示の位置に直す(「拡大」で拡大した表示のまま操作するため)。
Offset sceneOf(Offset local, Matrix4 transform) => MatrixUtils.transformPoint(Matrix4.inverted(transform), local);

/// 表示枠の中の位置を、PC の画面の 0〜1 の位置に直す。PC の画面は縦横比を保って枠の中央に描くので、
/// 上下か左右に余白ができる。余白に触れたときは null。
Offset? normalizeOnImage(Offset scene, Size box, int imageWidth, int imageHeight) {
  final aspect = imageWidth / imageHeight;
  double w = box.width, h = box.height, dx = 0, dy = 0;
  if (box.width / box.height > aspect) {
    w = box.height * aspect;
    dx = (box.width - w) / 2;
  } else {
    h = box.width / aspect;
    dy = (box.height - h) / 2;
  }
  final x = (scene.dx - dx) / w, y = (scene.dy - dy) / h;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return Offset(x, y);
}

/// PC の画面を見て操作する。タップ=クリック、長押し=右クリック、ダブルタップ=ダブルクリック、上下になぞる=スクロール。
class RemotePage extends StatefulWidget {
  const RemotePage({super.key, required this.active});
  final bool active;

  @override
  State<RemotePage> createState() => _RemotePageState();
}

class _RemotePageState extends State<RemotePage> {
  Uint8List? _bytes;
  String? _etag;
  int _imgW = 16, _imgH = 9;
  String? _error;
  bool _running = false;
  bool _control = true;
  bool _expanded = false;
  bool _keyboard = false;
  double _fps = 0;
  Offset? _tapMark;
  double _scrollAccum = 0;
  Map<String, dynamic>? _info;
  final _text = TextEditingController();
  final _open = TextEditingController();
  final _viewer = TransformationController();

  @override
  void didUpdateWidget(RemotePage old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) {
      _start();
    }
    if (!widget.active) PlatformBridge.keepScreenOn(false);
  }

  @override
  void initState() {
    super.initState();
    if (widget.active) WidgetsBinding.instance.addPostFrameCallback((_) => _start());
  }

  @override
  void dispose() {
    _running = false;
    PlatformBridge.keepScreenOn(false);
    _text.dispose();
    _open.dispose();
    _viewer.dispose();
    super.dispose();
  }

  Future<void> _start() async {
    if (_running) return;
    _running = true;
    PlatformBridge.keepScreenOn(true);
    unawaited(_loadInfo());
    var frames = 0;
    var window = DateTime.now();
    var quietFor = 0;
    final state = AppScope.read(context);
    while (mounted && _running && widget.active) {
      if (!state.online || !state.foreground) {
        await Future.delayed(const Duration(milliseconds: 800));
        continue;
      }
      final began = DateTime.now();
      try {
        final frame = await state.client!.screen(width: state.settings.screenWidth, etag: _etag);
        quietFor = frame == null ? quietFor + 1 : 0;
        if (frame != null && mounted) {
          setState(() {
            _bytes = frame.bytes;
            _etag = frame.etag;
            _imgW = frame.imageWidth > 0 ? frame.imageWidth : _imgW;
            _imgH = frame.imageHeight > 0 ? frame.imageHeight : _imgH;
            _error = null;
          });
        }
        if (frame != null) frames++;
        final span = DateTime.now().difference(window).inMilliseconds;
        if (span > 2000) {
          setState(() => _fps = frames * 1000 / span);
          frames = 0;
          window = DateTime.now();
        }
      } catch (e) {
        if (mounted) setState(() => _error = describeError(e));
        await Future.delayed(const Duration(seconds: 2));
      }
      // 画面が動いている間は間を詰め、止まっている間は少しずつ間を空ける(通信と電池の節約)
      final target = quietFor == 0 ? 90 : (150 + quietFor * 60).clamp(150, 700);
      final spent = DateTime.now().difference(began).inMilliseconds;
      await Future.delayed(Duration(milliseconds: (target - spent).clamp(15, target)));
    }
    _running = false;
  }

  Future<void> _loadInfo() async {
    final state = AppScope.read(context);
    if (!state.online) return;
    try {
      final info = await state.client!.getJson('/api/remote/info');
      if (mounted) setState(() => _info = info);
    } catch (e) {
      if (mounted) setState(() => _error = describeError(e));
    }
  }

  Future<void> _input(Map<String, dynamic> body) async {
    final state = AppScope.read(context);
    try {
      await state.client!.postJson('/api/remote/input', body);
      _etag = null; // すぐに新しい画面を取りに行く
    } catch (e) {
      state.toast(describeError(e));
    }
  }

  Future<void> _action(String action, [Map<String, dynamic> extra = const {}]) async {
    final state = AppScope.read(context);
    try {
      final r = await state.client!.postJson('/api/remote/action', {'action': action, ...extra});
      state.toast(r['message'] as String? ?? '実行しました');
      _etag = null;
    } catch (e) {
      state.toast(describeError(e));
    }
  }

  /// 拡大前の表示の位置を、PC の画面の 0〜1 の位置に直す(余白は無視)。
  Offset? _normalize(Offset scene, Size box) => normalizeOnImage(scene, box, _imgW, _imgH);

  /// 触れた位置(画面上)を、拡大前の表示の位置に直す。
  Offset _scene(Offset local) => sceneOf(local, _viewer.value);

  void _mark(Offset local) {
    setState(() => _tapMark = local);
    Future.delayed(const Duration(milliseconds: 450), () {
      if (mounted) setState(() => _tapMark = null);
    });
  }

  Widget _screen(BuildContext context) {
    final p = context.palette;
    if (_bytes == null) {
      return Container(
        color: Colors.black,
        alignment: Alignment.center,
        child: _error != null
            ? Padding(padding: const EdgeInsets.all(20), child: Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70)))
            : const CircularProgressIndicator(),
      );
    }
    final image = Image.memory(_bytes!, gaplessPlayback: true, fit: BoxFit.contain, filterQuality: FilterQuality.medium);
    return Container(
      color: Colors.black,
      child: LayoutBuilder(builder: (context, constraints) {
        final box = Size(constraints.maxWidth, constraints.maxHeight);
        final layer = Stack(fit: StackFit.expand, children: [
          image,
          if (_tapMark != null)
            Positioned(
              left: _tapMark!.dx - 14,
              top: _tapMark!.dy - 14,
              child: IgnorePointer(
                child: Container(width: 28, height: 28, decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: p.gold, width: 2))),
              ),
            ),
        ]);
        if (!_control) {
          return InteractiveViewer(transformationController: _viewer, minScale: 1, maxScale: 5, child: layer);
        }
        // 「拡大」で拡大した表示のまま操作できるように、拡大の状態を保って描き、触れた位置を元の位置に直す
        final shown = ClipRect(child: Transform(transform: _viewer.value, child: layer));
        return GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTapUp: (d) {
            final pos = _normalize(_scene(d.localPosition), box);
            if (pos == null) return;
            _mark(_scene(d.localPosition));
            _input({'action': 'click', 'x': pos.dx, 'y': pos.dy});
          },
          onDoubleTapDown: (d) {
            final pos = _normalize(_scene(d.localPosition), box);
            if (pos == null) return;
            _mark(_scene(d.localPosition));
            _input({'action': 'double', 'x': pos.dx, 'y': pos.dy});
          },
          onDoubleTap: () {},
          onLongPressStart: (d) {
            final pos = _normalize(_scene(d.localPosition), box);
            if (pos == null) return;
            HapticFeedback.mediumImpact();
            _mark(_scene(d.localPosition));
            _input({'action': 'right', 'x': pos.dx, 'y': pos.dy});
          },
          onVerticalDragUpdate: (d) {
            _scrollAccum += d.delta.dy;
            if (_scrollAccum.abs() >= 36) {
              final ticks = (_scrollAccum / 36).truncate();
              _scrollAccum -= ticks * 36;
              final pos = _normalize(_scene(d.localPosition), box);
              _input({'action': 'scroll', 'amount': ticks, if (pos != null) 'x': pos.dx, if (pos != null) 'y': pos.dy});
            }
          },
          onVerticalDragEnd: (_) => _scrollAccum = 0,
          child: shown,
        );
      }),
    );
  }

  Widget _key(String label, String keys, {IconData? icon}) => Padding(
        padding: const EdgeInsets.only(right: 6, bottom: 6),
        child: OutlinedButton(
          style: OutlinedButton.styleFrom(visualDensity: VisualDensity.compact, padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8)),
          onPressed: () => _input({'action': 'keys', 'keys': keys}),
          child: icon != null ? Icon(icon, size: 18) : Text(label, style: const TextStyle(fontSize: 13)),
        ),
      );

  Widget _toolbar(BuildContext context) {
    final state = AppScope.of(context);
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.fromLTRB(10, 8, 10, 4),
      child: Row(children: [
        SegmentedButton<bool>(
          style: SegmentedButton.styleFrom(visualDensity: VisualDensity.compact),
          showSelectedIcon: false,
          segments: const [
            ButtonSegment(value: true, icon: Icon(Icons.touch_app_outlined, size: 16), label: Text('操作')),
            ButtonSegment(value: false, icon: Icon(Icons.zoom_in, size: 16), label: Text('拡大')),
          ],
          selected: {_control},
          onSelectionChanged: (s) => setState(() => _control = s.first),
        ),
        const SizedBox(width: 6),
        FilterChip(
          visualDensity: VisualDensity.compact,
          avatar: const Icon(Icons.keyboard_outlined, size: 16),
          label: const Text('キーボード'),
          selected: _keyboard,
          onSelected: (v) => setState(() => _keyboard = v),
        ),
        const SizedBox(width: 6),
        PopupMenuButton<int>(
          tooltip: '画質',
          initialValue: state.settings.screenWidth,
          onSelected: (w) => state.settings.update((s) => s.screenWidth = w),
          itemBuilder: (_) => const [
            PopupMenuItem(value: 960, child: Text('速さ優先(960px)')),
            PopupMenuItem(value: 1280, child: Text('標準(1280px)')),
            PopupMenuItem(value: 1920, child: Text('画質優先(1920px)')),
          ],
          child: Pill('画質 ${state.settings.screenWidth}px  ${_fps < 0.05 ? '静止中' : '${_fps.toStringAsFixed(1)} fps'}', icon: Icons.hd_outlined),
        ),
        const SizedBox(width: 4),
        IconButton(tooltip: _expanded ? '操作パネルを表示' : '画面を大きく', onPressed: () => setState(() => _expanded = !_expanded), icon: Icon(_expanded ? Icons.fullscreen_exit : Icons.fullscreen)),
      ]),
    );
  }

  Widget _keyboardPanel(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 4),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Expanded(
            child: TextField(
              controller: _text,
              decoration: const InputDecoration(hintText: 'PC に入力する文字', isDense: true),
              onSubmitted: (_) => _sendText(),
            ),
          ),
          const SizedBox(width: 8),
          FilledButton(onPressed: _sendText, child: const Text('入力')),
        ]),
        const SizedBox(height: 8),
        Wrap(children: [
          _key('Enter', 'enter'),
          _key('', 'backspace', icon: Icons.backspace_outlined),
          _key('Tab', 'tab'),
          _key('Esc', 'esc'),
          _key('', 'up', icon: Icons.keyboard_arrow_up),
          _key('', 'down', icon: Icons.keyboard_arrow_down),
          _key('', 'left', icon: Icons.keyboard_arrow_left),
          _key('', 'right', icon: Icons.keyboard_arrow_right),
          _key('コピー', 'ctrl+c'),
          _key('貼り付け', 'ctrl+v'),
          _key('元に戻す', 'ctrl+z'),
          _key('すべて選択', 'ctrl+a'),
          _key('保存', 'ctrl+s'),
          _key('切り替え', 'alt+tab'),
          _key('Windows', 'win'),
          _key('F5', 'f5'),
        ]),
      ]),
    );
  }

  Future<void> _sendText() async {
    final text = _text.text;
    if (text.isEmpty) return;
    await _input({'action': 'text', 'text': text});
    _text.clear();
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    if (!state.paired || state.isDesktop) {
      return const EmptyState(icon: Icons.desktop_windows_outlined, title: 'PC と接続していません', body: '「設定」で PC と接続すると、スマホから PC の画面を見たり操作したりできます。');
    }
    if (!state.online) {
      return EmptyState(
        icon: Icons.link_off,
        title: 'PC につながっていません',
        body: state.linkError ?? 'PC の Celestial と Tailscale が動いているか確かめてください。',
        action: FilledButton(onPressed: state.connect, child: const Text('再接続')),
      );
    }
    final screen = AspectRatio(aspectRatio: _imgW / _imgH, child: _screen(context));
    final windows = (_info?['windows'] as List?)?.cast<String>() ?? const [];
    return SafeArea(
      child: Column(children: [
        _toolbar(context),
        if (_expanded) Expanded(child: _screen(context)) else screen,
        if (_keyboard) _keyboardPanel(context),
        if (!_expanded)
          Expanded(
            child: ListView(padding: const EdgeInsets.fromLTRB(12, 8, 12, 16), children: [
              if (_error != null) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(_error!, style: TextStyle(color: p.bad))),
              const FlowsCard(),
              const SizedBox(height: 12),
              SectionCard(
                title: 'すぐ使う操作',
                child: Wrap(spacing: 6, runSpacing: 6, children: [
                  IconButton.filledTonal(tooltip: '前の曲', onPressed: () => _action('previous'), icon: const Icon(Icons.skip_previous)),
                  IconButton.filledTonal(tooltip: '再生/一時停止', onPressed: () => _action('play_pause'), icon: const Icon(Icons.play_arrow)),
                  IconButton.filledTonal(tooltip: '次の曲', onPressed: () => _action('next'), icon: const Icon(Icons.skip_next)),
                  const SizedBox(width: 8),
                  IconButton.filledTonal(tooltip: '音量を下げる', onPressed: () => _action('volume_down'), icon: const Icon(Icons.volume_down)),
                  IconButton.filledTonal(tooltip: '消音', onPressed: () => _action('mute'), icon: const Icon(Icons.volume_off)),
                  IconButton.filledTonal(tooltip: '音量を上げる', onPressed: () => _action('volume_up'), icon: const Icon(Icons.volume_up)),
                  const SizedBox(width: 8),
                  IconButton.filledTonal(
                    tooltip: '画面をロック',
                    onPressed: () async {
                      if (await confirm(context, title: 'PC の画面をロックしますか?', body: 'ロックを解除するには PC の前でサインインが必要です。', ok: 'ロック')) {
                        _action('lock');
                      }
                    },
                    icon: const Icon(Icons.lock_outline),
                  ),
                ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                title: '開く',
                subtitle: 'アプリの名前(notepad など)、URL、ファイルの場所',
                child: Row(children: [
                  Expanded(child: TextField(controller: _open, decoration: const InputDecoration(hintText: '例: https://example.com', isDense: true))),
                  const SizedBox(width: 8),
                  FilledButton(
                    onPressed: () {
                      if (_open.text.trim().isNotEmpty) _action('open', {'target': _open.text.trim()});
                    },
                    child: const Text('開く'),
                  ),
                ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                title: 'ウィンドウ',
                subtitle: _info?['foreground'] != null ? '前面: ${_info!['foreground']}' : null,
                trailing: IconButton(tooltip: '更新', onPressed: _loadInfo, icon: const Icon(Icons.refresh)),
                child: windows.isEmpty
                    ? Text('ウィンドウの一覧を読み込み中', style: TextStyle(color: p.muted))
                    : Column(children: [
                        for (final title in windows)
                          ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text(title, maxLines: 1, overflow: TextOverflow.ellipsis),
                            onTap: () => _action('focus', {'title': title}),
                            trailing: IconButton(
                              tooltip: '閉じる',
                              icon: const Icon(Icons.close, size: 18),
                              onPressed: () async {
                                if (await confirm(context, title: 'ウィンドウを閉じますか?', body: title, ok: '閉じる')) {
                                  await _action('close', {'title': title});
                                  _loadInfo();
                                }
                              },
                            ),
                          ),
                      ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                title: 'クリップボード',
                child: Wrap(spacing: 8, runSpacing: 8, children: [
                  OutlinedButton.icon(
                    icon: const Icon(Icons.download, size: 18),
                    label: const Text('PC から受け取る'),
                    onPressed: () async {
                      try {
                        final r = await state.client!.getJson('/api/remote/clipboard');
                        await Clipboard.setData(ClipboardData(text: r['text'] as String? ?? ''));
                        state.toast('PC のクリップボードをコピーしました');
                      } catch (e) {
                        state.toast(describeError(e));
                      }
                    },
                  ),
                  OutlinedButton.icon(
                    icon: const Icon(Icons.upload, size: 18),
                    label: const Text('スマホから送る'),
                    onPressed: () async {
                      final data = await Clipboard.getData(Clipboard.kTextPlain);
                      if (data?.text?.isNotEmpty ?? false) await state.sendToPc('clipboard', data!.text!);
                    },
                  ),
                ]),
              ),
              const SizedBox(height: 12),
              if (_info?['system'] != null)
                SectionCard(
                  title: 'PC の状態',
                  trailing: IconButton(tooltip: '更新', onPressed: _loadInfo, icon: const Icon(Icons.refresh)),
                  child: Text(_info!['system'] as String, style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.6)),
                ),
            ]),
          ),
      ]),
    );
  }
}
