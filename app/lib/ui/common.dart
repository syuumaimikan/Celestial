import 'package:flutter/material.dart';

import '../app_state.dart';
import '../theme.dart';

/// アプリ全体の状態を、画面のどこからでも取り出せるようにする。
class AppScope extends InheritedNotifier<AppState> {
  const AppScope({super.key, required AppState state, required super.child}) : super(notifier: state);

  static AppState of(BuildContext context) => context.dependOnInheritedWidgetOfExactType<AppScope>()!.notifier!;
  static AppState read(BuildContext context) => context.getInheritedWidgetOfExactType<AppScope>()!.notifier!;
}

enum Tone { neutral, accent, gold, good, bad }

class Pill extends StatelessWidget {
  const Pill(this.text, {super.key, this.tone = Tone.neutral, this.icon});
  final String text;
  final Tone tone;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final p = context.palette, c = context.colors;
    final (bg, fg) = switch (tone) {
      Tone.neutral => (p.sunken, p.muted),
      Tone.accent => (p.accentSoft, c.primary),
      Tone.gold => (p.goldSoft, p.gold),
      Tone.good => (p.goodSoft, p.good),
      Tone.bad => (p.badSoft, p.bad),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(999)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) ...[Icon(icon, size: 13, color: fg), const SizedBox(width: 4)],
        Flexible(child: Text(text, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 11.5, color: fg, fontWeight: FontWeight.w600))),
      ]),
    );
  }
}

class SectionCard extends StatelessWidget {
  const SectionCard({super.key, required this.title, this.subtitle, this.trailing, required this.child, this.padding = const EdgeInsets.all(16)});
  final String title;
  final String? subtitle;
  final Widget? trailing;
  final Widget child;
  final EdgeInsets padding;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: padding,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, style: Theme.of(context).textTheme.titleMedium),
                if (subtitle != null) ...[
                  const SizedBox(height: 2),
                  Text(subtitle!, style: TextStyle(fontSize: 12.5, color: context.palette.muted)),
                ],
              ]),
            ),
            ?trailing,
          ]),
          const SizedBox(height: 12),
          child,
        ]),
      ),
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.title, required this.body, this.action});
  final IconData icon;
  final String title;
  final String body;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Container(
              width: 64,
              height: 64,
              decoration: BoxDecoration(color: context.palette.accentSoft, borderRadius: BorderRadius.circular(20)),
              child: Icon(icon, size: 30, color: context.colors.primary),
            ),
            const SizedBox(height: 16),
            Text(title, textAlign: TextAlign.center, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 6),
            Text(body, textAlign: TextAlign.center, style: TextStyle(color: context.palette.muted, height: 1.6)),
            if (action != null) ...[const SizedBox(height: 18), action!],
          ]),
        ),
      ),
    );
  }
}

/// PC との接続の状態を示す小さな表示。
class LinkBadge extends StatelessWidget {
  const LinkBadge({super.key});

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final (label, tone) = switch (state.link) {
      LinkState.online => (state.pcName, Tone.good),
      LinkState.connecting => ('接続中…', Tone.neutral),
      LinkState.offline => ('PC に未接続', Tone.bad),
      LinkState.unauthorized => ('登録が無効', Tone.bad),
      LinkState.unpaired => ('PC 未登録', Tone.neutral),
    };
    return Pill(label, tone: tone, icon: state.online ? Icons.link : Icons.link_off);
  }
}

/// Celestial の印(星をつないだ形)。絵文字の代わりに使う。
class CelestialMark extends StatelessWidget {
  const CelestialMark({super.key, this.size = 28});
  final double size;

  @override
  Widget build(BuildContext context) => CustomPaint(size: Size.square(size), painter: _MarkPainter(context.colors.primary, context.palette.gold));
}

class _MarkPainter extends CustomPainter {
  _MarkPainter(this.line, this.star);
  final Color line;
  final Color star;

  @override
  void paint(Canvas canvas, Size size) {
    final s = size.width / 32;
    final points = [Offset(6 * s, 22 * s), Offset(14 * s, 8 * s), Offset(25 * s, 14 * s), Offset(18 * s, 25 * s)];
    final path = Path()..addPolygon(points, true);
    canvas.drawPath(path, Paint()
      ..color = line.withValues(alpha: 0.55)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.4 * s);
    for (var i = 0; i < points.length; i++) {
      canvas.drawCircle(points[i], (i == 1 ? 3.2 : 2.4) * s, Paint()..color = i == 1 ? star : line);
    }
  }

  @override
  bool shouldRepaint(_MarkPainter old) => old.line != line || old.star != star;
}

Future<bool> confirm(BuildContext context, {required String title, required String body, String ok = 'OK', bool danger = false}) async {
  final result = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: Text(body),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('キャンセル')),
        FilledButton(
          style: danger ? FilledButton.styleFrom(backgroundColor: context.palette.bad) : null,
          onPressed: () => Navigator.pop(context, true),
          child: Text(ok),
        ),
      ],
    ),
  );
  return result ?? false;
}

Future<String?> askText(BuildContext context, {required String title, String initial = '', String hint = '', String ok = '保存'}) {
  final controller = TextEditingController(text: initial);
  return showDialog<String>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text(title),
      content: TextField(controller: controller, autofocus: true, decoration: InputDecoration(hintText: hint), onSubmitted: (v) => Navigator.pop(context, v)),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('キャンセル')),
        FilledButton(onPressed: () => Navigator.pop(context, controller.text), child: Text(ok)),
      ],
    ),
  );
}
