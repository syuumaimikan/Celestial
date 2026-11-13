import 'package:celestial/platform.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('読み上げ中の表示は、読み終わりの知らせで戻り、前の読み上げの知らせは無視する', () async {
    const channel = MethodChannel('dev.celestial/platform');
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
    Future<void> fromAndroid(bool on, int token) => messenger.handlePlatformMessage(
        'dev.celestial/platform', const StandardMethodCodec().encodeMethodCall(MethodCall('speaking', {'on': on, 'token': token})), (_) {});
    PlatformBridge.debugIsAndroid = true;
    addTearDown(() {
      PlatformBridge.debugIsAndroid = null;
      messenger.setMockMethodCallHandler(channel, null);
    });

    await PlatformBridge.speak('**一つ目**の回答', id: 'm1');
    expect(PlatformBridge.speakingId.value, 'm1');
    expect(calls.last.arguments['text'], '一つ目の回答');
    final first = calls.last.arguments['token'] as int;
    await PlatformBridge.speak('二つ目の回答', id: 'm2'); // 読み終わる前に別の回答を読む
    final second = calls.last.arguments['token'] as int;
    await fromAndroid(false, first); // 前の読み上げの終わりの知らせは無視する
    expect(PlatformBridge.speakingId.value, 'm2');
    await fromAndroid(false, second);
    expect(PlatformBridge.speakingId.value, isNull);

    await PlatformBridge.speak('三つ目', id: 'm3');
    await PlatformBridge.stopSpeaking();
    expect(PlatformBridge.speakingId.value, isNull);
    expect(calls.last.method, 'stopSpeaking');
  });

  test('読み上げでは記号を読まず、表は1行ずつ区切って読む', () {
    const answer = '## 売上\n\n| 店舗 | 売上 (円) |\n| -------- | -------- |\n| 東京 | 1,200,000 |\n| 合計 | 2,950,000 |\n\n---\n\n**以上**です。';
    expect(PlatformBridge.speakable(answer), '売上\n\n店舗、売上 (円)。\n東京、1,200,000。\n合計、2,950,000。\n\n以上です。');
    // 両端の | を省いた表も同じ
    expect(PlatformBridge.speakable('月 | 売上\n--- | ---\n4月 | 100'), '月、売上。\n4月、100。');
    // 掛け算の記号は「×」に、コードは省く
    expect(PlatformBridge.speakable('17 * 23 = 391\n```python\nprint(1)\n```'), '17 × 23 = 391\n(コードは省略します)');
  });
}
