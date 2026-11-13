// 会話の中のメッセージの順番が、時刻が同じでも崩れないことを確かめる。
import 'dart:io';

import 'package:celestial/core/models.dart';
import 'package:celestial/core/store.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('続けて足したメッセージは、足した順に並ぶ', () {
    final store = LocalStore(File('${Directory.systemTemp.path}/celestial-order-test.json'));
    final conv = store.createConversation(origin: 'phone');
    final ids = [for (var i = 0; i < 20; i++) store.addMessage(conv.id, i.isEven ? 'user' : 'assistant', 'm$i', dirty: false).id];
    expect(store.messagesOf(conv.id).map((m) => m.id).toList(), ids);
  });

  test('一覧の1行からは書式の記号だけを除く', () {
    expect(previewText('17 * 23 = 391'), '17 * 23 = 391');
    expect(previewText('加法混合では**赤・緑・青**です。'), '加法混合では赤・緑・青です。');
    expect(previewText('# 見出し\n- 項目 *強調* と `コード`'), '見出し 項目 強調 と コード');
    expect(previewText('[出典](https://example.com) を見て'), '出典 を見て');
    expect(previewText('要約して\n\n[添付ファイル: a.txt]\n````\n中身\n````'), '要約して');
  });

  test('PC から届いた時刻が同じなら、依頼を回答より前に置く', () {
    final store = LocalStore(File('${Directory.systemTemp.path}/celestial-order-test2.json'));
    final conv = store.createConversation(origin: 'pc');
    Map<String, dynamic> msg(String id, String role) => {
          'id': id, 'conversation_id': conv.id, 'role': role, 'content': role, 'created': 1000.0, 'updated': 1000.0,
          'deleted': false, 'meta': <String, dynamic>{}, 'rev': 1,
        };
    // id の並びとは逆になるようにする(以前は id で並べていたため、回答が先に出ることがあった)
    store.applyRemote(conversations: const [], messages: [msg('ffff', 'user'), msg('0000', 'assistant')], rev: 1);
    expect(store.messagesOf(conv.id).map((m) => m.role).toList(), ['user', 'assistant']);
    expect(Message.fromJson(msg('x', 'user')).role, 'user');
  });
}
