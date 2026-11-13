import 'dart:async';

import 'package:flutter/material.dart';

import '../platform.dart';
import '../theme.dart';
import 'common.dart';

/// 聞き取った文字のつなぎ方。
abstract final class VoiceButtonJoin {
  /// 始める前の文字に、聞き取った文字を続ける(英数字どうしの間だけ空白を入れる)。
  static String join(String base, String heard) {
    if (base.isEmpty) return heard;
    final gap = RegExp(r'[A-Za-z0-9.,!?]$').hasMatch(base) && RegExp(r'^[A-Za-z0-9]').hasMatch(heard) ? ' ' : '';
    return '$base$gap$heard';
  }
}

/// 入力欄のマイクのボタン。押すと聞き取りを始め、話した文字を入力欄に書き足す(もう一度押すと確定)。
/// 文字にするのは、できれば端末の中で行う(Android の端末内の音声認識)。
class VoiceButton extends StatefulWidget {
  const VoiceButton({super.key, required this.controller});
  final TextEditingController controller;

  @override
  State<VoiceButton> createState() => _VoiceButtonState();
}

class _VoiceButtonState extends State<VoiceButton> with SingleTickerProviderStateMixin {
  StreamSubscription<(String, String)>? _sub;
  String _base = ''; // 聞き取りを始める前に入力欄にあった文字
  bool _mine = false; // この入力欄が聞き取りを始めたか
  late final AnimationController _pulse = AnimationController(vsync: this, duration: const Duration(milliseconds: 900));

  @override
  void initState() {
    super.initState();
    _sub = PlatformBridge.voiceEvents.listen(_onVoice);
    PlatformBridge.voiceListening.addListener(_onListening);
  }

  @override
  void dispose() {
    _sub?.cancel();
    PlatformBridge.voiceListening.removeListener(_onListening);
    _pulse.dispose();
    super.dispose();
  }

  void _onListening() {
    final on = PlatformBridge.voiceListening.value && _mine;
    if (on) {
      _pulse.repeat(reverse: true);
    } else {
      _pulse.stop();
      _pulse.value = 0;
    }
    if (mounted) setState(() {});
  }

  void _onVoice((String, String) event) {
    if (!_mine) return;
    final (type, text) = event;
    switch (type) {
      case 'partial':
      case 'final':
        final value = VoiceButtonJoin.join(_base, text.trim());
        widget.controller.value = TextEditingValue(text: value, selection: TextSelection.collapsed(offset: value.length));
        if (type == 'final') _mine = false;
      case 'error':
        _mine = false;
        AppScope.of(context).toast(text);
      case 'end':
        _mine = false;
    }
  }

  Future<void> _toggle() async {
    if (PlatformBridge.voiceListening.value) {
      await PlatformBridge.stopVoice();
      return;
    }
    _base = widget.controller.text;
    _mine = true;
    try {
      await PlatformBridge.startVoice();
    } catch (e) {
      _mine = false;
      if (mounted) AppScope.of(context).toast('音声入力を始められませんでした');
    }
  }

  @override
  Widget build(BuildContext context) {
    final listening = PlatformBridge.voiceListening.value && _mine;
    final colors = context.colors;
    return Tooltip(
      message: listening ? '聞き取りを終える' : '音声で入力',
      child: InkResponse(
        onTap: _toggle,
        radius: 22,
        child: AnimatedBuilder(
          animation: _pulse,
          builder: (context, child) => Container(
            width: 36,
            height: 36,
            alignment: Alignment.center,
            decoration: listening
                ? BoxDecoration(
                    shape: BoxShape.circle,
                    color: colors.primary.withValues(alpha: 0.12 + 0.12 * _pulse.value),
                    border: Border.all(color: colors.primary.withValues(alpha: 0.5), width: 1.2),
                  )
                : null,
            child: Icon(listening ? Icons.mic : Icons.mic_none, size: 20, color: listening ? colors.primary : context.palette.muted),
          ),
        ),
      ),
    );
  }
}
