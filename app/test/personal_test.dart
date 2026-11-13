import 'package:celestial/core/personal.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  // PC 側(tests/test_personal.py)と同じ例で、同じ結果になることを確かめる
  final today = DateTime(2026, 11, 13); // 金曜日

  test('「覚えて」「忘れて」「何を覚えてる?」', () {
    expect(memoryCommand('覚えて: 私の名前は佐藤です'), ('remember', '私の名前は佐藤です'));
    expect(memoryCommand('私の誕生日は5月3日だと覚えておいて'), ('remember', '私の誕生日は5月3日'));
    expect(memoryCommand('猫を2匹飼っていることを覚えてね。'), ('remember', '猫を2匹飼っている'));
    expect(memoryCommand('Remember that I prefer metric units'), ('remember', 'I prefer metric units'));
    expect(memoryCommand('誕生日のことは忘れて'), ('forget', '誕生日'));
    expect(memoryCommand('何を覚えてる?'), ('recall', ''));
    for (final text in ['英単語を効率よく覚える方法は?', 'この手順を覚えておいて', '次の文章を覚えて、要約して', '忘れ物を防ぐコツは?']) {
      expect(memoryCommand(text), isNull, reason: text);
    }
  });

  test('指示文に足す部分', () {
    expect(personalPrompt('', []), '');
    final text = personalPrompt('敬語を使わず簡潔に', ['私の名前は佐藤']);
    expect(text, contains('敬語を使わず簡潔に'));
    expect(text, contains('- 私の名前は佐藤'));
  });

  test('日付の計算(AIに計算させない)', () {
    expect(dateFacts('今年の大晦日まであと何日?', [], today), ['大晦日(12月31日(木))まで あと48日']);
    expect(dateFacts('私の誕生日まであと何日?', ['私の名前は佐藤', '誕生日は5月3日'], today), ['誕生日(2027年5月3日(月))まで あと171日']);
    expect(dateFacts('今日から100日後は何日?', [], today), ['100日後は 2027年2月21日(日)']);
    expect(dateFacts('3か月後の日付は?', [], today), ['3か月後は 2027年2月13日(土)']);
    expect(dateFacts('2020年1月1日から何日経った?', [], today), ['2020年1月1日(水)から今日まで 2508日']);
    expect(dateFacts('1か月後は?', [], DateTime(2026, 1, 31)), ['1か月後は 2月28日(土)']);
    expect(dateFacts('2月30日まであと何日?', [], today), isEmpty);
  });

  test('短い日付の質問には計算で答える', () {
    expect(dateAnswer('今日は何曜日?', [], today), '今日は 2026年11月13日(金曜日)です。');
    expect(dateAnswer('5月3日は何曜日?', [], today), '2027年5月3日は月曜日です。');
    expect(dateAnswer('3か月後の日付は?', [], today), '3か月後は 2027年2月13日(土)です。');
    expect(dateAnswer('私の誕生日まであと何日?', ['誕生日は5月3日'], today), '誕生日(2027年5月3日(月))まで あと171日です。');
    for (final text in ['大晦日まで営業日で何日?', '5月3日と12月25日まで何日?', '来週の会議の議事録を作って', '今何時?']) {
      expect(dateAnswer(text, [], today), isNull, reason: text);
    }
  });

  test('日付に関わる依頼にだけ今日の日付を渡す', () {
    final now = DateTime(2026, 11, 13, 17, 5);
    expect(timeContext('今日は何曜日?', now), contains('2026年11月13日(金曜日)'));
    expect(timeContext('今何時?', now), contains('17時05分'));
    expect(timeContext('What day is it today?', now), contains('Friday, November 13, 2026'));
    expect(timeContext('挨拶の例を1つ', now), '');
    expect(timeContext('大晦日まであと何日?', now), contains('計算済みの値'));
  });
}
