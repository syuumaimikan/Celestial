import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:camera/camera.dart';
import 'package:celestial_llm/celestial_llm.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:google_mlkit_barcode_scanning/google_mlkit_barcode_scanning.dart';
import 'package:google_mlkit_face_detection/google_mlkit_face_detection.dart';
import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';

import '../app_state.dart';
import '../net/pc_client.dart';
import '../platform.dart';
import '../theme.dart';
import '../vision/perception.dart';
import 'common.dart';
import 'markdown.dart';

const _askSystem = 'あなたは Celestial の画像アシスタントです。端末のカメラが読み取った情報(写っているものの推定・文字・コード・顔の数)だけを根拠に、'
    '利用者の質問に自然な日本語で簡潔に答えてください。読み取れていないことは推測で断定せず、「画像からは分かりません」と答えてください。';

/// 見る: カメラの映像を、端末の中でリアルタイムに認識する(文字・物と様子・QR とバーコード・顔・翻訳・質問)。
/// 映像は端末の外へ送らない。「PC で詳しく」を押したときだけ、止めた1枚を PC のAIに送る。
class VisionPage extends StatefulWidget {
  const VisionPage({super.key, required this.active, required this.onSendToChat});
  final bool active;
  final void Function(String text) onSendToChat;

  @override
  State<VisionPage> createState() => _VisionPageState();
}

class _VisionPageState extends State<VisionPage> with WidgetsBindingObserver {
  final _perceiver = Perceiver();
  final _question = TextEditingController();
  List<CameraDescription> _cameras = [];
  CameraController? _controller;
  int _cameraIndex = 0;
  bool _starting = false;
  String? _error;
  SeeMode _mode = SeeMode.text;
  FlashMode _flash = FlashMode.off;

  Seen? _seen;
  bool _busy = false;
  DateTime _last = DateTime.fromMillisecondsSinceEpoch(0);

  // 止めた1枚(撮った写真・選んだ画像)
  Uint8List? _stillBytes;
  String? _stillPath;
  Size _stillSize = Size.zero;
  Seen? _stillSeen;
  bool _readingStill = false;

  // 翻訳
  bool? _translationReady;
  bool _downloading = false;
  bool _translating = false;
  String _stableText = '';
  int _stableCount = 0;
  String _translatedFrom = '';
  String _translation = '';

  // 質問
  String _answer = '';
  String _answerBy = '';
  bool _answering = false;

  bool get _frozen => _stillBytes != null;
  Seen? get _current => _frozen ? _stillSeen : _seen;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (widget.active) _start();
  }

  @override
  void didUpdateWidget(VisionPage old) {
    super.didUpdateWidget(old);
    if (widget.active != old.active) widget.active ? _start() : _stop();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (!widget.active) return;
    if (state == AppLifecycleState.paused || state == AppLifecycleState.inactive) {
      _stop();
    } else if (state == AppLifecycleState.resumed) {
      _start();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _stop();
    _perceiver.close();
    _question.dispose();
    super.dispose();
  }

  // ---- カメラ ----

  Future<void> _start() async {
    if (!Platform.isAndroid || _controller != null || _starting) return;
    _starting = true;
    try {
      if (_cameras.isEmpty) _cameras = await availableCameras();
      if (_cameras.isEmpty) {
        setState(() => _error = 'この端末にカメラが見つかりません');
        return;
      }
      final controller = CameraController(_cameras[_cameraIndex % _cameras.length], ResolutionPreset.high,
          enableAudio: false, imageFormatGroup: ImageFormatGroup.nv21);
      await controller.initialize();
      if (!mounted || !widget.active) {
        await controller.dispose();
        return;
      }
      try {
        await controller.setFlashMode(_flash);
      } catch (_) {}
      _controller = controller;
      _error = null;
      if (!_frozen) await controller.startImageStream(_onFrame);
      unawaited(PlatformBridge.keepScreenOn(true));
      if (mounted) setState(() {});
    } on CameraException catch (e) {
      final denied = e.code.startsWith('CameraAccessDenied') || e.code.contains('ermission');
      if (mounted) {
        setState(() => _error = denied ? 'カメラの使用が許可されていません。端末の設定で Celestial のカメラを許可してから、もう一度開いてください。' : 'カメラを開けませんでした(${e.description ?? e.code})');
      }
    } catch (e) {
      if (mounted) setState(() => _error = 'カメラを開けませんでした($e)');
    } finally {
      _starting = false;
    }
  }

  Future<void> _stop() async {
    final controller = _controller;
    _controller = null;
    unawaited(PlatformBridge.keepScreenOn(false));
    if (controller != null) {
      try {
        if (controller.value.isStreamingImages) await controller.stopImageStream();
      } catch (_) {}
      await controller.dispose();
    }
    if (mounted) setState(() {});
  }

  Future<void> _switchCamera() async {
    if (_cameras.length < 2) return;
    _cameraIndex = (_cameraIndex + 1) % _cameras.length;
    _seen = null;
    await _stop();
    await _start();
  }

  Future<void> _toggleFlash() async {
    final controller = _controller;
    if (controller == null) return;
    final next = _flash == FlashMode.torch ? FlashMode.off : FlashMode.torch;
    try {
      await controller.setFlashMode(next);
      setState(() => _flash = next);
    } catch (_) {
      _toast('このカメラではライトを使えません');
    }
  }

  int get _intervalMs => switch (_mode) { SeeMode.code => 120, SeeMode.face => 100, SeeMode.scene => 250, _ => 380 };

  void _onFrame(CameraImage image) {
    if (_busy || _frozen || !mounted) return;
    final now = DateTime.now();
    if (now.difference(_last).inMilliseconds < _intervalMs) return;
    final controller = _controller;
    if (controller == null) return;
    final converted = frameToInput(image, controller.description);
    if (converted == null) return;
    _busy = true;
    _last = now;
    final mode = _mode == SeeMode.ask ? SeeMode.scene : _mode;
    _perceiver.look(converted.$1, converted.$2, mode).then((seen) {
      if (!mounted || _frozen) return;
      setState(() => _seen = seen);
      if (_mode == SeeMode.translate) _maybeTranslate(seen.plainText);
    }).catchError((Object e) {
      debugPrint('vision: $e');
    }).whenComplete(() => _busy = false);
  }

  // ---- 止めた1枚 ----

  Future<void> _capture() async {
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized || controller.value.isTakingPicture) return;
    try {
      if (controller.value.isStreamingImages) await controller.stopImageStream();
      final shot = await controller.takePicture();
      HapticFeedback.lightImpact();
      await _openStill(shot.path);
    } catch (e) {
      _toast('撮影できませんでした: $e');
      await _resume();
    }
  }

  Future<void> _pickImage() async {
    final picked = await PlatformBridge.pickFile();
    if (picked == null) return;
    if (!RegExp(r'\.(jpe?g|png|webp|bmp|gif|heic|heif)$', caseSensitive: false).hasMatch(picked.name)) {
      _toast('画像のファイルを選んでください');
      return;
    }
    final controller = _controller;
    if (controller != null && controller.value.isStreamingImages) await controller.stopImageStream();
    await _openStill(picked.path);
  }

  Future<void> _openStill(String path) async {
    final bytes = await File(path).readAsBytes();
    final codec = await ui.instantiateImageCodec(bytes);
    final frame = await codec.getNextFrame();
    final size = Size(frame.image.width.toDouble(), frame.image.height.toDouble());
    frame.image.dispose();
    setState(() {
      _stillBytes = bytes;
      _stillPath = path;
      _stillSize = size;
      _stillSeen = null;
      _readingStill = true;
      _answer = '';
      _answerBy = '';
    });
    try {
      final seen = await _perceiver.look(InputImage.fromFilePath(path), size, _mode, live: false);
      if (!mounted || _stillPath != path) return;
      setState(() => _stillSeen = seen);
      if (_mode == SeeMode.translate && seen.plainText.isNotEmpty && _translationReady == true) unawaited(_translate(seen.plainText));
    } catch (e) {
      _toast('読み取れませんでした: $e');
    } finally {
      if (mounted) setState(() => _readingStill = false);
    }
  }

  Future<void> _resume() async {
    setState(() {
      _stillBytes = null;
      _stillPath = null;
      _stillSeen = null;
      _answer = '';
      _answerBy = '';
    });
    final controller = _controller;
    if (controller != null && controller.value.isInitialized && !controller.value.isStreamingImages) {
      try {
        await controller.startImageStream(_onFrame);
      } catch (_) {}
    } else if (controller == null) {
      await _start();
    }
  }

  // ---- 翻訳 ----

  Future<void> _setMode(SeeMode mode) async {
    setState(() {
      _mode = mode;
      _seen = null;
    });
    HapticFeedback.selectionClick();
    if (mode == SeeMode.translate && _translationReady == null) {
      try {
        final ready = await translationReady();
        if (mounted) setState(() => _translationReady = ready);
      } catch (_) {
        if (mounted) setState(() => _translationReady = false);
      }
    }
    final path = _stillPath;
    if (_frozen && path != null) await _openStill(path); // 止めた画像は、新しいモードで読み直す
  }

  void _maybeTranslate(String text) {
    if (text.isEmpty || _translationReady != true) return;
    if (text == _stableText) {
      _stableCount++;
    } else {
      _stableText = text;
      _stableCount = 1;
    }
    if (_stableCount >= 2 && text != _translatedFrom && !_translating) unawaited(_translate(text));
  }

  Future<void> _translate(String text) async {
    _translating = true;
    try {
      final out = await _perceiver.translate(text);
      if (mounted) {
        setState(() {
          _translatedFrom = text;
          _translation = out;
        });
      }
    } catch (e) {
      _toast('翻訳できませんでした: $e');
    } finally {
      _translating = false;
    }
  }

  Future<void> _installTranslation(bool wifiOnly) async {
    setState(() => _downloading = true);
    try {
      final ok = await downloadTranslation(wifiOnly: wifiOnly);
      if (mounted) setState(() => _translationReady = ok);
      if (!ok) _toast('翻訳のモデルを入れられませんでした');
    } catch (e) {
      _toast('翻訳のモデルを入れられませんでした: $e');
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  // ---- 質問 ----

  Future<void> _ask(AppState state, {required bool pc}) async {
    if (_answering) return;
    if (!_frozen) await _capture(); // 質問は、止めた1枚について答える
    while (_readingStill) {
      await Future.delayed(const Duration(milliseconds: 60));
    }
    final seen = _stillSeen, bytes = _stillBytes;
    if (seen == null || bytes == null || !mounted) return;
    final question = _question.text.trim();
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() {
      _answering = true;
      _answer = '';
      _answerBy = pc ? 'PC で読み取り中…' : '';
    });
    try {
      if (pc) {
        final client = state.client;
        if (client == null || !state.online) throw StateError('PC に接続していません');
        final image = await _shrink(bytes);
        final r = await client.postJson('/api/vision', {'image': base64Encode(image), 'mode': question.isEmpty ? 'describe' : 'ask', 'question': question},
            timeout: const Duration(seconds: 150));
        if (!mounted) return;
        setState(() {
          _answer = (r['text'] as String?) ?? '';
          _answerBy = 'PC の ${r['model']}(${(r['latency_s'] as num?)?.toStringAsFixed(1) ?? '-'} 秒)';
        });
      } else {
        final name = state.settings.localModel;
        if (name.isEmpty || state.ai.find(name) == null) throw StateError('端末内のAIがまだありません。設定の「AIモデル」で PC から受け取ってください');
        if (state.ai.busy) throw StateError('端末内のAIが、ほかの回答の途中です');
        final messages = [
          const LlmMessage('system', _askSystem),
          LlmMessage('user', '画像から端末が読み取った情報:\n${seen.describe()}\n\n質問: ${question.isEmpty ? '何が写っているか説明して' : question}'),
        ];
        setState(() => _answerBy = '端末内の $name');
        await for (final piece in state.ai.generate(name, messages,
            options: const LlmOptions(temperature: 0.3, maxTokens: 600), contextSize: state.settings.contextSize, threads: state.settings.threads)) {
          if (!mounted) break;
          setState(() => _answer += piece);
        }
      }
    } catch (e) {
      if (mounted) setState(() => _answer = 'エラー: ${e is StateError ? e.message : describeError(e)}');
    } finally {
      if (mounted) setState(() => _answering = false);
    }
  }

  /// PC へ送る画像は、大きければ縮める(長い辺 1600px まで)。
  Future<Uint8List> _shrink(Uint8List bytes) async {
    if (bytes.length < 1500000) return bytes;
    final probe = await ui.instantiateImageCodec(bytes);
    final first = await probe.getNextFrame();
    final wide = first.image.width >= first.image.height;
    first.image.dispose();
    final codec = await ui.instantiateImageCodec(bytes, targetWidth: wide ? 1600 : null, targetHeight: wide ? null : 1600);
    final frame = await codec.getNextFrame();
    final data = await frame.image.toByteData(format: ui.ImageByteFormat.png);
    frame.image.dispose();
    final png = data?.buffer.asUint8List();
    return png != null && png.length < bytes.length ? png : bytes; // 写真は PNG にすると大きくなることがある
  }

  void _toast(String text) {
    if (!mounted) return;
    AppScope.read(context).toast(text);
  }

  void _send(String text) {
    if (text.trim().isEmpty) return;
    widget.onSendToChat(text.trim());
  }

  // ---- 画面 ----

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (!Platform.isAndroid) {
      return Scaffold(
        appBar: AppBar(title: const Text('見る')),
        body: const EmptyState(
            icon: Icons.center_focus_weak_outlined,
            title: 'スマホのカメラで使えます',
            body: 'PC では、ブラウザーの Celestial の「見る」で、カメラ・画面・画像を読み取れます。'),
      );
    }
    final padding = MediaQuery.paddingOf(context);
    return Scaffold(
      backgroundColor: Colors.black,
      body: AnnotatedRegion<SystemUiOverlayStyle>(
        value: SystemUiOverlayStyle.light,
        child: Stack(fit: StackFit.expand, children: [
          _viewport(context),
          Positioned(top: padding.top + 8, left: 12, right: 12, child: _topBar(context)),
          Positioned(left: 0, right: 0, bottom: 0, child: _bottom(context, state)),
        ]),
      ),
    );
  }

  Widget _viewport(BuildContext context) {
    final p = context.palette, accent = context.colors.primary;
    if (_frozen) {
      return Center(
        child: AspectRatio(
          aspectRatio: _stillSize.width / _stillSize.height,
          child: Stack(fit: StackFit.expand, children: [
            Image.memory(_stillBytes!, fit: BoxFit.fill, gaplessPlayback: true),
            if (_stillSeen != null) CustomPaint(painter: SeenPainter(_stillSeen!, _mode, mirror: false, accent: accent, gold: p.gold, good: p.good)),
            if (_readingStill) const _ScanLine(),
          ]),
        ),
      );
    }
    final controller = _controller;
    if (controller == null || !controller.value.isInitialized) {
      if (_error != null) {
        return Center(
          child: Padding(
            padding: const EdgeInsets.all(28),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              const Icon(Icons.no_photography_outlined, color: Colors.white70, size: 40),
              const SizedBox(height: 14),
              Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white, height: 1.6)),
              const SizedBox(height: 16),
              FilledButton.tonal(onPressed: _start, child: const Text('もう一度')),
            ]),
          ),
        );
      }
      return const Center(child: SizedBox(width: 28, height: 28, child: CircularProgressIndicator(strokeWidth: 2.4, color: Colors.white70)));
    }
    final preview = controller.value.previewSize ?? const Size(1280, 720);
    final front = controller.description.lensDirection == CameraLensDirection.front;
    return ClipRect(
      child: FittedBox(
        fit: BoxFit.cover,
        child: SizedBox(
          width: preview.height,
          height: preview.width,
          child: Stack(fit: StackFit.expand, children: [
            CameraPreview(controller),
            if (_seen != null) CustomPaint(painter: SeenPainter(_seen!, _mode, mirror: front, accent: accent, gold: p.gold, good: p.good)),
          ]),
        ),
      ),
    );
  }

  Widget _topBar(BuildContext context) {
    final seen = _current;
    final status = _frozen
        ? (_readingStill ? '読み取り中…' : '止めた画像')
        : (_controller == null ? 'カメラを準備中' : 'ライブ・端末内で認識${seen != null ? '(${seen.millis}ms)' : ''}');
    return Row(children: [
      _Glass(
        padding: const EdgeInsets.fromLTRB(10, 7, 14, 7),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          const CelestialMark(size: 18),
          const SizedBox(width: 8),
          Text(status, style: const TextStyle(color: Colors.white, fontSize: 12.5, fontWeight: FontWeight.w600)),
        ]),
      ),
      const Spacer(),
      _RoundIcon(icon: _flash == FlashMode.torch ? Icons.flashlight_on : Icons.flashlight_off_outlined, tooltip: 'ライト', onTap: _frozen ? null : _toggleFlash),
      const SizedBox(width: 8),
      _RoundIcon(icon: Icons.cameraswitch_outlined, tooltip: 'カメラを切り替える', onTap: _frozen || _cameras.length < 2 ? null : _switchCamera),
    ]);
  }

  Widget _bottom(BuildContext context, AppState state) {
    final padding = MediaQuery.paddingOf(context);
    return Column(mainAxisSize: MainAxisSize.min, children: [
      Padding(padding: const EdgeInsets.fromLTRB(12, 0, 12, 10), child: _resultCard(context, state)),
      SizedBox(
        height: 40,
        child: ListView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 12),
          children: [
            for (final mode in SeeMode.values)
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: _ModeChip(label: mode.label, selected: mode == _mode, onTap: () => _setMode(mode)),
              ),
          ],
        ),
      ),
      Padding(
        padding: EdgeInsets.fromLTRB(28, 12, 28, 14 + (padding.bottom > 0 ? 0 : 4)),
        child: Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
          _RoundIcon(icon: Icons.photo_library_outlined, tooltip: '画像を選ぶ', size: 48, onTap: _pickImage),
          _Shutter(frozen: _frozen, onTap: _frozen ? _resume : _capture),
          _RoundIcon(
              icon: Icons.ios_share_outlined,
              tooltip: '結果を共有',
              size: 48,
              onTap: _resultText().isEmpty ? null : () => PlatformBridge.shareText(_resultText())),
        ]),
      ),
    ]);
  }

  /// 今のモードの結果を文字にしたもの(コピー・共有・会話へ送る用)。
  String _resultText() {
    final seen = _current;
    switch (_mode) {
      case SeeMode.text:
        return seen?.plainText ?? '';
      case SeeMode.translate:
        return _translation.isEmpty ? '' : '$_translatedFrom\n\n$_translation';
      case SeeMode.ask:
        return _answer;
      case SeeMode.scene:
      case SeeMode.code:
      case SeeMode.face:
        return seen == null || seen.isEmpty ? '' : seen.describe();
    }
  }

  Widget _resultCard(BuildContext context, AppState state) {
    final maxHeight = MediaQuery.sizeOf(context).height * 0.36;
    return _Glass(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: maxHeight),
        child: SingleChildScrollView(
          child: AnimatedSize(
            duration: const Duration(milliseconds: 180),
            alignment: Alignment.bottomCenter,
            child: switch (_mode) {
              SeeMode.text => _textResult(),
              SeeMode.scene => _sceneResult(),
              SeeMode.code => _codeResult(),
              SeeMode.face => _faceResult(),
              SeeMode.translate => _translateResult(),
              SeeMode.ask => _askResult(state),
            },
          ),
        ),
      ),
    );
  }

  Widget _hint(String text) => Text(text, style: const TextStyle(color: Colors.white70, fontSize: 13, height: 1.5));

  Widget _actions(String text, {bool chat = true, bool speak = true}) => Padding(
        padding: const EdgeInsets.only(top: 10),
        child: Wrap(spacing: 8, runSpacing: 8, children: [
          _SmallButton(icon: Icons.copy_rounded, label: 'コピー', onTap: () {
            Clipboard.setData(ClipboardData(text: text));
            _toast('コピーしました');
          }),
          if (chat) _SmallButton(icon: Icons.forum_outlined, label: '会話で使う', onTap: () => _send(text)),
          if (speak) _SmallButton(icon: Icons.volume_up_outlined, label: '読み上げ', onTap: () => PlatformBridge.speak(text)),
        ]),
      );

  Widget _textResult() {
    final text = _current?.plainText ?? '';
    if (text.isEmpty) return _hint(_frozen ? (_readingStill ? '文字を読み取っています…' : '文字は見つかりませんでした') : '文字にカメラを向けてください。日本語と英数字を読み取ります。');
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SelectableText(text, style: const TextStyle(color: Colors.white, fontSize: 15, height: 1.55)),
      _actions(text),
    ]);
  }

  Widget _sceneResult() {
    final seen = _current;
    if (seen == null || (seen.labels.isEmpty && seen.objects.isEmpty)) return _hint('周りにカメラを向けると、写っているものを推定します。');
    final kinds = <String, int>{};
    for (final o in seen.objects) {
      final name = o.labels.isEmpty ? '物' : objectJa(o.labels.first.text);
      kinds[name] = (kinds[name] ?? 0) + 1;
    }
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      if (seen.objects.isNotEmpty)
        Padding(
          padding: const EdgeInsets.only(bottom: 8),
          child: Text('見つけた物 ${seen.objects.length} 個  ${kinds.entries.map((e) => '${e.key} ${e.value}').join('・')}',
              style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
        ),
      Wrap(spacing: 6, runSpacing: 6, children: [
        for (final l in seen.labels.take(12)) _Confidence(label: labelJa(l.label), value: l.confidence),
      ]),
      if (_frozen) _actions(seen.describe(), speak: false),
    ]);
  }

  Widget _codeResult() {
    final codes = _current?.barcodes ?? const <Barcode>[];
    if (codes.isEmpty) return _hint('QR コードやバーコードにカメラを向けてください。');
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      for (final b in codes.take(4))
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Pill(barcodeKind(b), tone: Tone.accent),
            const SizedBox(height: 6),
            SelectableText(_codeText(b), style: const TextStyle(color: Colors.white, fontSize: 15, height: 1.5)),
            const SizedBox(height: 6),
            Wrap(spacing: 8, runSpacing: 8, children: [
              if (b.value is BarcodeUrl || (b.rawValue ?? '').startsWith(RegExp(r'https?://')))
                _SmallButton(icon: Icons.open_in_new, label: '開く', onTap: () => PlatformBridge.openUrl((b.value as BarcodeUrl?)?.url ?? b.rawValue!)),
              if (b.value is BarcodeWifi && (b.value as BarcodeWifi).password != null)
                _SmallButton(icon: Icons.key_outlined, label: 'パスワードをコピー', onTap: () {
                  Clipboard.setData(ClipboardData(text: (b.value as BarcodeWifi).password!));
                  _toast('パスワードをコピーしました');
                }),
              _SmallButton(icon: Icons.copy_rounded, label: 'コピー', onTap: () {
                Clipboard.setData(ClipboardData(text: b.rawValue ?? b.displayValue ?? ''));
                _toast('コピーしました');
              }),
            ]),
          ]),
        ),
    ]);
  }

  String _codeText(Barcode b) {
    final value = b.value;
    if (value is BarcodeWifi) return 'ネットワーク名: ${value.ssid ?? '-'}\nパスワード: ${value.password == null ? '-' : '••••••••'}';
    if (value is BarcodeUrl) return value.url ?? b.rawValue ?? '';
    return b.displayValue ?? b.rawValue ?? '';
  }

  Widget _faceResult() {
    final faces = _current?.faces ?? const <Face>[];
    if (faces.isEmpty) return _hint('人の顔にカメラを向けると、人数と表情を読み取ります(顔の照合はしません)。');
    String turn(Face f) {
      final y = f.headEulerAngleY ?? 0;
      return y > 18 ? '左向き' : y < -18 ? '右向き' : '正面';
    }

    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('${faces.length} 人', style: const TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w700)),
      const SizedBox(height: 8),
      Wrap(spacing: 6, runSpacing: 6, children: [
        for (final (i, f) in faces.take(8).indexed)
          _Glass(
            dark: true,
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            child: Text(
              '${i + 1}: 笑顔 ${((f.smilingProbability ?? 0) * 100).round()}%・'
              '目 ${((f.leftEyeOpenProbability ?? 1) + (f.rightEyeOpenProbability ?? 1)) / 2 > 0.4 ? '開' : '閉'}・${turn(f)}',
              style: const TextStyle(color: Colors.white, fontSize: 12.5),
            ),
          ),
      ]),
    ]);
  }

  Widget _translateResult() {
    if (_translationReady == null) return _hint('翻訳の準備を確かめています…');
    if (_translationReady == false) {
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const Text('翻訳のモデルを端末に入れます', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w700)),
        const SizedBox(height: 4),
        _hint('日本語と英語の翻訳モデル(Google の ML Kit、合わせて約 60MB)を一度だけ取り込みます。取り込んだあとは、ネットなしで端末の中だけで翻訳します。'),
        const SizedBox(height: 10),
        Row(children: [
          Expanded(
            child: FilledButton(
              onPressed: _downloading ? null : () => _installTranslation(true),
              child: Text(_downloading ? '取り込み中…' : 'Wi-Fi のときに取り込む'),
            ),
          ),
          const SizedBox(width: 8),
          TextButton(
            onPressed: _downloading ? null : () => _installTranslation(false),
            style: TextButton.styleFrom(foregroundColor: Colors.white70),
            child: const Text('今すぐ'),
          ),
        ]),
      ]);
    }
    final source = _current?.plainText ?? '';
    if (source.isEmpty && _translation.isEmpty) return _hint('文字にカメラを向けると、日本語は英語に、ほかの言語は日本語に訳します。');
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      if (_translatedFrom.isNotEmpty)
        Text(_translatedFrom, maxLines: 3, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Colors.white60, fontSize: 12.5, height: 1.45)),
      const SizedBox(height: 6),
      if (_translation.isEmpty)
        _hint('読み取った文字が落ち着いたら訳します…')
      else
        SelectableText(_translation, style: const TextStyle(color: Colors.white, fontSize: 16, height: 1.55, fontWeight: FontWeight.w500)),
      if (_translation.isNotEmpty) _actions(_translation),
    ]);
  }

  Widget _askResult(AppState state) {
    final online = state.online;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      TextField(
        controller: _question,
        style: const TextStyle(color: Colors.white),
        cursorColor: Colors.white,
        textInputAction: TextInputAction.send,
        onSubmitted: (_) => _ask(state, pc: false),
        decoration: InputDecoration(
          isDense: true,
          hintText: '質問(例: これは何? 賞味期限は?)',
          hintStyle: const TextStyle(color: Colors.white54),
          filled: true,
          fillColor: Colors.white.withValues(alpha: 0.08),
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(12), borderSide: BorderSide.none),
        ),
      ),
      const SizedBox(height: 10),
      Row(children: [
        Expanded(
          child: FilledButton.icon(
            onPressed: _answering ? null : () => _ask(state, pc: false),
            icon: const Icon(Icons.smartphone, size: 18),
            label: const Text('端末のAIで答える'),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: OutlinedButton.icon(
            onPressed: _answering || !online ? null : () => _ask(state, pc: true),
            style: OutlinedButton.styleFrom(foregroundColor: Colors.white, side: const BorderSide(color: Colors.white38)),
            icon: const Icon(Icons.desktop_windows_outlined, size: 18),
            label: const Text('PC で詳しく'),
          ),
        ),
      ]),
      const SizedBox(height: 6),
      _hint(online
          ? '端末のAIは、端末が読み取った文字・物・コードをもとに答えます。「PC で詳しく」は、止めた1枚を PC の画像AIに送ります。'
          : '端末のAIは、端末が読み取った文字・物・コードをもとに答えます(PC につながると、画像をそのまま読む「PC で詳しく」も使えます)。'),
      if (_answerBy.isNotEmpty || _answer.isNotEmpty) ...[
        const SizedBox(height: 10),
        if (_answerBy.isNotEmpty) Text(_answerBy, style: const TextStyle(color: Colors.white54, fontSize: 12)),
        if (_answer.isNotEmpty)
          Theme(
            data: ThemeData.dark(useMaterial3: true).copyWith(extensions: [Theme.of(context).extension<CelestialColors>()!]),
            child: DefaultTextStyle.merge(style: const TextStyle(color: Colors.white), child: MarkdownView(_answer)),
          ),
        if (_answering && _answer.isEmpty) const Padding(padding: EdgeInsets.only(top: 8), child: LinearProgressIndicator(minHeight: 2)),
        if (!_answering && _answer.isNotEmpty) _actions(_answer),
      ],
    ]);
  }
}

/// 読み取ったものの位置(文字・物・コード・顔)を、映像の上に枠で示す。
class SeenPainter extends CustomPainter {
  SeenPainter(this.seen, this.mode, {required this.mirror, required this.accent, required this.gold, required this.good});
  final Seen seen;
  final SeeMode mode;
  final bool mirror;
  final Color accent, gold, good;

  @override
  void paint(Canvas canvas, Size size) {
    if (seen.size.width <= 0 || seen.size.height <= 0) return;
    final sx = size.width / seen.size.width, sy = size.height / seen.size.height;
    Rect place(Rect r) {
      var left = r.left * sx, right = r.right * sx;
      if (mirror) (left, right) = (size.width - right, size.width - left);
      return Rect.fromLTRB(left, r.top * sy, right, r.bottom * sy);
    }

    void box(Rect r, Color color, {String? label, double radius = 10}) {
      final rect = place(r);
      final rr = RRect.fromRectAndRadius(rect, Radius.circular(radius));
      canvas.drawRRect(rr, Paint()..color = color.withValues(alpha: 0.12));
      canvas.drawRRect(
          rr,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 2.2 / ((sx + sy) / 2).clamp(0.2, 5)
            ..color = color);
      if (label != null && label.isNotEmpty) _tag(canvas, rect.topLeft, label, color, 1 / ((sx + sy) / 2).clamp(0.2, 5));
    }

    switch (mode) {
      case SeeMode.text:
      case SeeMode.translate:
        for (final TextBlock b in seen.text?.blocks ?? const []) {
          box(b.boundingBox, accent, radius: 6);
        }
      case SeeMode.scene:
      case SeeMode.ask:
        for (final o in seen.objects) {
          final label = o.labels.isEmpty ? null : '${objectJa(o.labels.first.text)} ${(o.labels.first.confidence * 100).round()}%';
          box(o.boundingBox, gold, label: label);
        }
      case SeeMode.code:
        for (final b in seen.barcodes) {
          box(b.boundingBox, good, label: barcodeKind(b));
        }
      case SeeMode.face:
        for (final (i, f) in seen.faces.indexed) {
          box(f.boundingBox, accent, label: '${i + 1}', radius: 18);
        }
    }
  }

  void _tag(Canvas canvas, Offset at, String text, Color color, double scale) {
    final painter = TextPainter(
      text: TextSpan(text: text, style: TextStyle(color: Colors.white, fontSize: 13 * scale, fontWeight: FontWeight.w700)),
      textDirection: TextDirection.ltr,
    )..layout();
    final pad = 6 * scale;
    final rect = Rect.fromLTWH(at.dx, at.dy - painter.height - pad * 1.4, painter.width + pad * 2, painter.height + pad);
    canvas.drawRRect(RRect.fromRectAndRadius(rect, Radius.circular(8 * scale)), Paint()..color = color);
    painter.paint(canvas, Offset(rect.left + pad, rect.top + pad / 2));
  }

  @override
  bool shouldRepaint(SeenPainter old) => old.seen != seen || old.mode != mode || old.mirror != mirror;
}

/// すりガラスのような半透明の面(カメラの映像の上に置く)。
class _Glass extends StatelessWidget {
  const _Glass({required this.child, this.padding = const EdgeInsets.all(12), this.dark = false});
  final Widget child;
  final EdgeInsets padding;
  final bool dark;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(18),
      child: BackdropFilter(
        filter: ui.ImageFilter.blur(sigmaX: 18, sigmaY: 18),
        child: Container(
          padding: padding,
          decoration: BoxDecoration(
            color: Colors.black.withValues(alpha: dark ? 0.35 : 0.45),
            border: Border.all(color: Colors.white.withValues(alpha: 0.10)),
            borderRadius: BorderRadius.circular(18),
          ),
          child: child,
        ),
      ),
    );
  }
}

class _RoundIcon extends StatelessWidget {
  const _RoundIcon({required this.icon, required this.tooltip, required this.onTap, this.size = 42});
  final IconData icon;
  final String tooltip;
  final VoidCallback? onTap;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: Opacity(
        opacity: onTap == null ? 0.4 : 1,
        child: ClipOval(
          child: BackdropFilter(
            filter: ui.ImageFilter.blur(sigmaX: 14, sigmaY: 14),
            child: Material(
              color: Colors.black.withValues(alpha: 0.42),
              child: InkWell(
                onTap: onTap,
                child: SizedBox(width: size, height: size, child: Icon(icon, color: Colors.white, size: size * 0.46)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Shutter extends StatelessWidget {
  const _Shutter({required this.frozen, required this.onTap});
  final bool frozen;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: frozen ? 'ライブに戻る' : '止めて詳しく読み取る',
      child: GestureDetector(
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          width: 74,
          height: 74,
          padding: const EdgeInsets.all(5),
          decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: Colors.white, width: 3.5)),
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 180),
            decoration: BoxDecoration(
              color: frozen ? context.colors.primary : Colors.white,
              borderRadius: BorderRadius.circular(frozen ? 14 : 40),
            ),
            child: frozen ? const Icon(Icons.videocam_outlined, color: Colors.white) : null,
          ),
        ),
      ),
    );
  }
}

class _ModeChip extends StatelessWidget {
  const _ModeChip({required this.label, required this.selected, required this.onTap});
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 160),
        padding: const EdgeInsets.symmetric(horizontal: 16),
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: selected ? Colors.white : Colors.black.withValues(alpha: 0.42),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: Colors.white.withValues(alpha: selected ? 0 : 0.16)),
        ),
        child: Text(label,
            style: TextStyle(color: selected ? Colors.black : Colors.white, fontWeight: FontWeight.w700, fontSize: 13.5, letterSpacing: 0.2)),
      ),
    );
  }
}

class _SmallButton extends StatelessWidget {
  const _SmallButton({required this.icon, required this.label, required this.onTap});
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.white.withValues(alpha: 0.10),
      borderRadius: BorderRadius.circular(999),
      child: InkWell(
        borderRadius: BorderRadius.circular(999),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Icon(icon, size: 16, color: Colors.white),
            const SizedBox(width: 6),
            Text(label, style: const TextStyle(color: Colors.white, fontSize: 12.5, fontWeight: FontWeight.w600)),
          ]),
        ),
      ),
    );
  }
}

class _Confidence extends StatelessWidget {
  const _Confidence({required this.label, required this.value});
  final String label;
  final double value;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(10, 5, 10, 5),
      decoration: BoxDecoration(color: Colors.white.withValues(alpha: 0.10), borderRadius: BorderRadius.circular(999)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Text(label, style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600)),
        const SizedBox(width: 6),
        SizedBox(
          width: 28,
          height: 4,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(2),
            child: LinearProgressIndicator(value: value, backgroundColor: Colors.white24, color: context.palette.gold),
          ),
        ),
      ]),
    );
  }
}

/// 写真を読み取っている間の、上から下へ流れる光の線。
class _ScanLine extends StatefulWidget {
  const _ScanLine();

  @override
  State<_ScanLine> createState() => _ScanLineState();
}

class _ScanLineState extends State<_ScanLine> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1500))..repeat();

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = context.colors.primary;
    return IgnorePointer(
      child: AnimatedBuilder(
        animation: _c,
        builder: (context, _) => DecoratedBox(
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topCenter,
              end: Alignment.bottomCenter,
              colors: [Colors.transparent, color.withValues(alpha: 0.35), Colors.transparent],
              stops: [(_c.value - 0.12).clamp(0.0, 1.0), _c.value, (_c.value + 0.12).clamp(0.0, 1.0)],
            ),
          ),
        ),
      ),
    );
  }
}
