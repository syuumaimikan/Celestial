import 'package:celestial/core/models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('繰り返しを1回分で切る', () {
    final looped = '答えは次のとおりです。${'同じ文をずっと繰り返してしまう小さなモデルの出力の例です。' * 12}';
    expect(loopCut(looped), '答えは次のとおりです。同じ文をずっと繰り返してしまう小さなモデルの出力の例です。');
    expect(loopCut('あいう' * 200), 'あいう');
  });

  test('普通の文章は切らない', () {
    final text = '正常な文章です。${[for (var i = 0; i < 40; i++) '$i番目の項目について、それぞれ違う内容を書いています。'].join()}';
    expect(loopCut(text), isNull);
    expect(loopCut('短い'), isNull);
  });
}
