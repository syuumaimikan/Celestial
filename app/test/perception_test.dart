import 'dart:ui';

import 'package:celestial/vision/perception.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_mlkit_image_labeling/google_mlkit_image_labeling.dart';
import 'package:google_mlkit_text_recognition/google_mlkit_text_recognition.dart';

void main() {
  test('読み取った結果を、端末内のAIに渡す文章にする', () {
    final seen = Seen(
      size: const Size(720, 1280),
      labels: [ImageLabel(confidence: 0.93, label: 'Dog', index: 0), ImageLabel(confidence: 0.71, label: 'Grass', index: 1)],
      text: RecognizedText(text: '営業時間 9:00-18:00', blocks: const []),
    );
    final text = seen.describe();
    expect(text, contains('犬(93%)'));
    expect(text, contains('草(71%)'));
    expect(text, contains('営業時間 9:00-18:00'));
    expect(seen.isEmpty, isFalse);
    expect(Seen(size: const Size(1, 1)).describe(), '(読み取れたものはありません)');
  });

  test('ラベルと物の分類の日本語名', () {
    expect(labelJa('Cat'), '猫');
    expect(labelJa('Something new'), 'Something new'); // 訳のないものは英語のまま
    expect(objectJa('Food'), '食べ物');
    expect(hasJapanese('Hello'), isFalse);
    expect(hasJapanese('こんにちは'), isTrue);
  });
}
