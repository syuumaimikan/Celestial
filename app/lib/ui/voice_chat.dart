import 'dart:async';

import 'package:flutter/material.dart';

import '../platform.dart';
import '../theme.dart';
import 'common.dart';

/// 音声会話(手を使わずに話し続ける): 聞く → AIに送る → 答えを読み上げる → また聞く、を繰り返す。
/// 「終わり」「ストップ」と言うか、終了ボタンで止まる。聞き取りは端末の音声認識(できれば端末の中だけ)を使う。
Future<void> showVoiceChat(BuildContext context, {required String? conversationId, required bool usePc, required ValueChanged<String> onConversation}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    isDismissible: false,
    enableDrag: false,
    builder: (_) => VoiceChatSheet(conversationId: conversationId, usePc: usePc, onConversation: onConversation),
  );
}

enum VoicePhase { listening, thinking, speaking, stopped }

final _stopWords = RegExp(r'^(終わり|おわり|終了|ストップ|やめて|止めて|もういい|stop|bye)(です|します)?[。.!！]?$', caseSensitive: false);

/// 音声会話を終える言葉か(「終わり」「ストップ」など)。
bool isStopWord(String text) => _stopWords.hasMatch(text.trim());

class VoiceChatSheet extends StatefulWidget {
  const VoiceChatSheet({super.key, required this.conversationId, required this.usePc, required this.onConversation});
  final String? conversationId;
  final bool usePc;
  final ValueChanged<String> onConversation;

  @override
  State<VoiceChatSheet> createState() => _VoiceChatSheetState();
}

class _VoiceChatSheetState extends State<VoiceChatSheet> with SingleTickerProviderStateMixin {
  VoicePhase phase = VoicePhase.listening;
  String heard = '';
  String answer = '';
  String? note;
  String? _conversation;
  bool _active = true;
  int _turn = 0;
  late final AnimationController _pulse = AnimationController(vsync: this, duration: const Duration(milliseconds: 1100))..repeat(reverse: true);

  @override
  void initState() {
    super.initState();
    _conversation = widget.conversationId;
    WidgetsBinding.instance.addPostFrameCallback((_) => _loop());
  }

  @override
  void dispose() {
    _active = false;
    _pulse.dispose();
    super.dispose();
  }

  void _set(VoicePhase value, {String? message}) {
    if (!mounted) return;
    setState(() {
      phase = value;
      note = message;
    });
  }

  /// 繰り返しても直らない聞き取りのエラー(音声データがない・マイクの許可がない など)。
  String? _fatal;

  /// 1回聞き取る(話し終わると端末の音声認識が自動で区切る)。聞き取れなければ空。
  Future<String> _listenOnce() async {
    final done = Completer<String>();
    final sub = PlatformBridge.voiceEvents.listen((event) {
      final (type, text) = event;
      if (type == 'partial' && mounted) setState(() => heard = text);
      if (type == 'final' && !done.isCompleted) done.complete(text.trim());
      if ((type == 'error' || type == 'end') && !done.isCompleted) {
        if (type == 'error') {
          // 「聞き取れなかった」「声が聞こえなかった」はもう一度聞けば直る。それ以外は止めて原因を伝える
          if (!text.contains('聞き取れ') && !text.contains('聞こえ')) _fatal = text;
          _set(VoicePhase.listening, message: text);
        }
        done.complete('');
      }
    });
    try {
      await PlatformBridge.startVoice();
      return await done.future.timeout(const Duration(seconds: 40), onTimeout: () => '');
    } catch (_) {
      return '';
    } finally {
      await sub.cancel();
    }
  }

  Future<void> _loop() async {
    final state = AppScope.read(context);
    var misses = 0;
    while (_active && mounted) {
      setState(() => heard = '');
      _set(VoicePhase.listening);
      final text = await _listenOnce();
      if (!_active || !mounted) break;
      if (text.isEmpty) {
        if (_fatal != null) {
          _set(VoicePhase.stopped, message: _fatal);
          break;
        }
        if (++misses >= 3) {
          _set(VoicePhase.stopped, message: '声が聞き取れなかったため止めました');
          break;
        }
        continue;
      }
      misses = 0;
      if (isStopWord(text)) break;
      setState(() => (heard = text, answer = ''));
      _set(VoicePhase.thinking);
      try {
        final id = await state.send(_conversation, text, usePc: widget.usePc && state.online);
        _conversation = id;
        widget.onConversation(id);
        while (_active && state.runs.containsKey(id)) {
          await Future.delayed(const Duration(milliseconds: 250));
          final live = state.runs[id];
          if (live != null && mounted && live.text.isNotEmpty) setState(() => answer = live.text);
        }
        final messages = state.store.messagesOf(id);
        answer = messages.isNotEmpty && messages.last.role == 'assistant' ? messages.last.content : answer;
      } catch (e) {
        _set(VoicePhase.stopped, message: '$e');
        break;
      }
      if (!_active || !mounted || answer.trim().isEmpty) continue;
      setState(() {});
      _set(VoicePhase.speaking);
      final key = 'voice-${_turn++}';
      await PlatformBridge.speak(answer, id: key);
      while (_active && PlatformBridge.speakingId.value == key) {
        await Future.delayed(const Duration(milliseconds: 200));
      }
    }
    if (mounted && phase != VoicePhase.stopped) _set(VoicePhase.stopped);
  }

  Future<void> _finish() async {
    _active = false;
    try {
      await PlatformBridge.stopVoice();
      await PlatformBridge.stopSpeaking();
    } catch (_) {}
    if (mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final colors = context.colors;
    final (label, icon) = switch (phase) {
      VoicePhase.listening => ('聞いています', Icons.mic),
      VoicePhase.thinking => (widget.usePc ? 'PC のAIが考えています' : 'スマホのAIが考えています', Icons.auto_awesome_outlined),
      VoicePhase.speaking => ('話しています', Icons.volume_up_outlined),
      VoicePhase.stopped => ('止まりました', Icons.stop_circle_outlined),
    };
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 20, 20, 16),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Text('音声会話', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          Text('「終わり」と言うと止まります', style: TextStyle(color: p.muted, fontSize: 12.5)),
          const SizedBox(height: 20),
          AnimatedBuilder(
            animation: _pulse,
            builder: (context, _) {
              final moving = phase != VoicePhase.stopped;
              final scale = moving ? 1 + 0.08 * _pulse.value : 1.0;
              return Transform.scale(
                scale: scale,
                child: Container(
                  width: 104,
                  height: 104,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: colors.primary.withValues(alpha: phase == VoicePhase.stopped ? 0.08 : 0.14 + 0.1 * _pulse.value),
                    border: Border.all(color: colors.primary.withValues(alpha: 0.6), width: 1.5),
                  ),
                  child: Icon(icon, size: 42, color: colors.primary),
                ),
              );
            },
          ),
          const SizedBox(height: 14),
          Text(label, style: const TextStyle(fontWeight: FontWeight.w600)),
          if (note != null) Padding(padding: const EdgeInsets.only(top: 4), child: Text(note!, style: TextStyle(color: p.bad, fontSize: 12.5), textAlign: TextAlign.center)),
          const SizedBox(height: 14),
          if (heard.isNotEmpty)
            Align(
              alignment: Alignment.centerRight,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(color: colors.primaryContainer.withValues(alpha: 0.5), borderRadius: BorderRadius.circular(14)),
                child: Text(heard),
              ),
            ),
          if (answer.isNotEmpty) ...[
            const SizedBox(height: 8),
            ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 180),
              child: SingleChildScrollView(child: Text(PlatformBridge.speakable(answer), style: const TextStyle(height: 1.5))),
            ),
          ],
          const SizedBox(height: 18),
          SizedBox(
            width: double.infinity,
            child: FilledButton.tonalIcon(onPressed: _finish, icon: const Icon(Icons.close), label: const Text('終了')),
          ),
        ]),
      ),
    );
  }
}
