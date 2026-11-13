// PC 操作の画面で「拡大」したまま操作したとき、触れた位置が PC の画面の正しい位置になるかを確かめる。
import 'package:celestial/ui/remote_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  const box = Size(400, 225); // スマホの表示枠(PC の画面 1920x1080 と同じ 16:9)

  Offset? tapAt(Offset local, Matrix4 transform) => normalizeOnImage(sceneOf(local, transform), box, 1920, 1080);

  test('拡大していないとき', () {
    final p = tapAt(const Offset(200, 112.5), Matrix4.identity())!;
    expect(p.dx, closeTo(0.5, 1e-9));
    expect(p.dy, closeTo(0.5, 1e-9));
  });

  test('中央を2倍に拡大したまま、左上の角に触れる', () {
    // 拡大(InteractiveViewer)と同じ形の変換: 2倍にして、中央が枠の中央に来るようにずらす
    final zoom = Matrix4.identity()
      ..translateByDouble(-200, -112.5, 0, 1)
      ..scaleByDouble(2, 2, 1, 1);
    final corner = tapAt(Offset.zero, zoom)!;
    expect(corner.dx, closeTo(0.25, 1e-9)); // 見えているのは PC の画面の中央の半分
    expect(corner.dy, closeTo(0.25, 1e-9));
    final center = tapAt(const Offset(200, 112.5), zoom)!;
    expect(center.dx, closeTo(0.5, 1e-9));
    expect(center.dy, closeTo(0.5, 1e-9));
  });

  test('右下を3倍に拡大して、細かい所に触れる', () {
    final zoom = Matrix4.identity()
      ..translateByDouble(-800, -450, 0, 1)
      ..scaleByDouble(3, 3, 1, 1);
    // 枠の右下の角は、PC の画面の右下の角
    final p = tapAt(const Offset(400, 225), zoom)!;
    expect(p.dx, closeTo(1.0, 1e-9));
    expect(p.dy, closeTo(1.0, 1e-9));
  });

  test('縦長の枠では上下の余白に触れても操作しない', () {
    const tall = Size(400, 400); // 16:9 の画像は上下に 87.5 の余白ができる
    expect(normalizeOnImage(const Offset(200, 50), tall, 1920, 1080), isNull);
    final p = normalizeOnImage(const Offset(200, 200), tall, 1920, 1080)!;
    expect(p.dx, closeTo(0.5, 1e-9));
    expect(p.dy, closeTo(0.5, 1e-9));
  });

  testWidgets('2本指で拡大した状態が、操作に切り替えても残る(拡大の部品の動き)', (tester) async {
    final controller = TransformationController();
    await tester.pumpWidget(MaterialApp(
      home: Center(
        child: SizedBox(
          width: 400,
          height: 225,
          child: InteractiveViewer(transformationController: controller, minScale: 1, maxScale: 5, child: Container(color: Colors.black)),
        ),
      ),
    ));
    final center = tester.getCenter(find.byType(InteractiveViewer));
    final a = await tester.startGesture(center - const Offset(40, 0), pointer: 1);
    final b = await tester.startGesture(center + const Offset(40, 0), pointer: 2);
    for (var i = 1; i <= 10; i++) {
      await a.moveTo(center - Offset(40.0 + 8 * i, 0));
      await b.moveTo(center + Offset(40.0 + 8 * i, 0));
      await tester.pump(const Duration(milliseconds: 16));
    }
    await a.up();
    await b.up();
    await tester.pumpAndSettle();
    final scale = controller.value.getMaxScaleOnAxis();
    expect(scale, greaterThan(1.5)); // 指を広げた分だけ拡大されている
    // 拡大したまま、枠の中央に触れると PC の画面の中央付近になる(拡大の中心がずれていても枠の中に収まる)
    final p = normalizeOnImage(sceneOf(const Offset(200, 112.5), controller.value), box, 1920, 1080)!;
    expect(p.dx, inInclusiveRange(0.3, 0.7));
    expect(p.dy, inInclusiveRange(0.3, 0.7));
  });
}
