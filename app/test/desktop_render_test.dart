// PC(Windows)版の画面を、GPU や画面の状態に左右されない試験用の描画で実際に描いて確かめる。
// 日本語のフォントとアイコンを読み込み、画像(goldens/)として残す:
//   flutter test --update-goldens test/desktop_render_test.dart
@TestOn('windows')
library;

import 'dart:io';

import 'package:celestial/app_state.dart';
import 'package:celestial/ai/local_ai.dart';
import 'package:celestial/core/settings.dart';
import 'package:celestial/core/store.dart';
import 'package:celestial/main.dart';
import 'package:celestial/theme.dart';
import 'package:celestial/ui/common.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

Future<void> _loadFonts() async {
  Future<ByteData> read(String path) async => ByteData.sublistView(await File(path).readAsBytes());
  final japanese = read(r'C:\Windows\Fonts\meiryo.ttc');
  // 既定の文字(プラットフォームごとの既定の書体名)に日本語の書体を当てる
  // Windows 版が使う日本語の書体(windowsFont)と、試験の既定の書体名に、日本語の書体を当てる
  for (final family in [windowsFont, 'Roboto']) {
    await (FontLoader(family)..addFont(japanese)).load();
  }
  final flutterRoot = Platform.environment['FLUTTER_ROOT'] ?? r'C:\src\flutter';
  await (FontLoader('MaterialIcons')..addFont(read('$flutterRoot/bin/cache/artifacts/material_fonts/materialicons-regular.otf'))).load();
}

AppState _state(Directory dir) {
  final state = AppState();
  state.settings = Settings(File('${dir.path}/settings.json'));
  state.store = LocalStore(File('${dir.path}/conversations.json'));
  state.ai = LocalAi(Directory('${dir.path}/models'));
  state.ready = true;
  final conv = state.store.createConversation(title: '光の三原色を挙げて', origin: 'pc');
  state.store.addMessage(conv.id, 'user', '光の三原色を挙げて', meta: {'device': 'pc'});
  state.store.addMessage(conv.id, 'assistant', '加法混合では**赤・緑・青**です。減法混合では、シアン・マゼンタ・イエローです。', meta: {
    'status': 'done',
    'model': 'gemma3:4b',
    'role': 'general',
    'latency_s': 1.25,
    'escalated': {'from': 'qwen2.5:1.5b', 'to': 'gemma3:4b', 'reason': 'unsure'},
    'sources': [
      {'title': '原色', 'url': 'https://ja.wikipedia.org/wiki/%E5%8E%9F%E8%89%B2'},
    ],
  });
  // 一覧に出る日時を固定する(実行するたびに画像が変わらないように)
  conv
    ..created = DateTime(2025, 1, 2, 10).millisecondsSinceEpoch / 1000
    ..updated = DateTime(2025, 1, 2, 10).millisecondsSinceEpoch / 1000;
  return state;
}

void main() {
  testWidgets('PC 版の会話画面が描ける', (tester) async {
    await tester.runAsync(_loadFonts); // 実際のファイルの読み込みは、試験の仮の時間の外で行う
    tester.view.physicalSize = const Size(1240, 820);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final dir = Directory.systemTemp.createTempSync('celestial-render-');
    addTearDown(() {
      try {
        dir.deleteSync(recursive: true);
      } on FileSystemException {
        // 保存の途中のファイルが残っていれば、一時フォルダの掃除に任せる
      }
    });

    final state = _state(dir);
    await tester.pumpWidget(AppScope(state: state, child: const CelestialApp()));
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(find.text('会話'), findsWidgets);
    expect(find.text('設定'), findsWidgets);
    // 会話を開く
    await tester.tap(find.text('光の三原色を挙げて').first);
    // 最後まで送る動き(スクロール)が終わってから撮る(途中で撮ると、実行するたびに位置がずれる)
    await tester.pumpAndSettle(const Duration(milliseconds: 100), EnginePhase.sendSemanticsUpdate, const Duration(seconds: 10));
    expect(find.textContaining('出典: 原色'), findsOneWidget);
    expect(find.textContaining('答え直し qwen2.5:1.5b → gemma3:4b'), findsOneWidget);
    await expectLater(find.byType(CelestialApp), matchesGoldenFile('goldens/desktop_chat.png'));
  });
}
