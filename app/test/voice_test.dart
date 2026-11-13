import 'package:celestial/platform.dart';
import 'package:celestial/theme.dart';
import 'package:celestial/ui/voice_button.dart';
import 'package:celestial/ui/voice_chat.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('マイクのボタンで聞き取り、途中の文字と確定した文字を入力欄に書き足す', (tester) async {
    const channel = MethodChannel('dev.celestial/platform');
    final messenger = TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      return null;
    });
    Future<void> fromAndroid(String type, String text) => messenger.handlePlatformMessage(
        'dev.celestial/platform', const StandardMethodCodec().encodeMethodCall(MethodCall('speech', {'type': type, 'text': text})), (_) {});
    PlatformBridge.debugIsAndroid = true;
    addTearDown(() {
      PlatformBridge.debugIsAndroid = null;
      messenger.setMockMethodCallHandler(channel, null);
    });

    final input = TextEditingController(text: '天気について。');
    await tester.pumpWidget(MaterialApp(
      theme: buildTheme(Brightness.light),
      home: Scaffold(body: TextField(controller: input, decoration: InputDecoration(suffixIcon: VoiceButton(controller: input)))),
    ));
    expect(find.byIcon(Icons.mic_none), findsOneWidget);
    await tester.tap(find.byIcon(Icons.mic_none));
    await tester.pump();
    expect(calls.last.method, 'listen');
    expect(calls.last.arguments['lang'], 'ja-JP');
    expect(PlatformBridge.voiceListening.value, isTrue);
    expect(find.byIcon(Icons.mic), findsOneWidget); // 聞き取り中の表示

    await fromAndroid('partial', '明日の');
    await tester.pump();
    expect(input.text, '天気について。明日の');
    await fromAndroid('final', '明日の天気を教えて');
    await tester.pump();
    expect(input.text, '天気について。明日の天気を教えて');
    expect(PlatformBridge.voiceListening.value, isFalse);
    await tester.pump(const Duration(milliseconds: 100));
    expect(find.byIcon(Icons.mic_none), findsOneWidget); // 確定したら元に戻る
  });

  test('音声会話を終える言葉', () {
    for (final t in ['終わり', '終わりです。', 'ストップ', 'stop', 'もういい']) {
      expect(isStopWord(t), isTrue, reason: t);
    }
    for (final t in ['終わりの時間を教えて', 'ストップウォッチの使い方は?', '今日の予定']) {
      expect(isStopWord(t), isFalse, reason: t);
    }
  });

  test('英数字どうしの間だけ空白を入れる', () {
    expect(VoiceButtonJoin.join('Hello', 'world'), 'Hello world');
    expect(VoiceButtonJoin.join('こんにちは', '元気?'), 'こんにちは元気?');
    expect(VoiceButtonJoin.join('', 'テスト'), 'テスト');
  });
}
