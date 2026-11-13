import 'package:celestial/core/models.dart';
import 'package:celestial/theme.dart';
import 'package:celestial/ui/markdown.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  // 小さいAIは両端の | を省いた表(GFM では正しい書き方)をよく書く
  const answer = '売上表です。\n\n月 | 売上\n------- | --------\n4月 | 1,200,000円\n合計 | =SUM(above)\n\n以上です。';

  test('両端の | を省いた表をそろえる', () {
    expect(pipeTables(answer), contains('| 月 | 売上 |\n| ------- | -------- |\n| 4月 | 1,200,000円 |'));
    expect(hasTable(answer), isTrue);
    expect(hasTable('| a | b |\n|-|-|\n| 1 | 2 |'), isTrue);
    expect(hasTable('```csv\na,b\n1,2\n```'), isTrue);
    // 区切りの行がなければ表ではない。コードブロックの中は変えない
    expect(hasTable('a | b の違い\nどちらでもよい'), isFalse);
    expect(hasTable('| 見出しだけ |\n|---|'), isFalse);
    const fenced = '```\nx | y\n--- | ---\n```';
    expect(pipeTables(fenced), fenced);
  });

  test('会話の一覧の1行では、表をセルの並びにする', () {
    expect(previewText('| 店舗 | 売上 |\n| --- | --- |\n| 東京 | 1,200,000 |'), '店舗 売上 / 東京 1,200,000');
    expect(previewText(answer), '売上表です。 月 売上 / 4月 1,200,000円 / 合計 =SUM(above) / 以上です。');
  });

  testWidgets('両端の | がない表も表として表示する', (tester) async {
    await tester.pumpWidget(MaterialApp(
      theme: buildTheme(Brightness.light),
      home: const Scaffold(body: MarkdownView(answer, selectable: false)),
    ));
    expect(find.byType(Table), findsOneWidget);
    final table = tester.widget<Table>(find.byType(Table));
    expect(table.children.length, 3); // 見出し + 2行(区切りの行は表に出さない)
    expect(find.text('=SUM(above)', findRichText: true), findsOneWidget);
    expect(find.textContaining('-------', findRichText: true), findsNothing);
  });
}
