import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'models.dart';

/// 端末内の会話の保存。PC と同期する前提で、PC と同じ形のデータを JSON ファイルに置く。
/// 端末で変えたものは「未送信」として印を付け、PC につながったときにまとめて送る。
class LocalStore extends ChangeNotifier {
  LocalStore(this.file);

  final File file;
  final Map<String, Conversation> conversations = {};
  final Map<String, Message> messages = {};
  final Set<String> dirtyConversations = {};
  final Set<String> dirtyMessages = {};
  int lastRev = 0;
  String? dbId; // 同期している PC のデータベースの識別子(変わったら最初から同期し直す)
  Timer? _saveTimer;

  Future<void> load() async {
    if (!await file.exists()) return;
    try {
      final data = jsonDecode(await file.readAsString()) as Map<String, dynamic>;
      for (final c in (data['conversations'] as List? ?? [])) {
        final conv = Conversation.fromJson(Map<String, dynamic>.from(c as Map));
        conversations[conv.id] = conv;
      }
      for (final m in (data['messages'] as List? ?? [])) {
        final msg = Message.fromJson(Map<String, dynamic>.from(m as Map));
        messages[msg.id] = msg;
      }
      dirtyConversations.addAll((data['dirty_conversations'] as List? ?? []).cast<String>());
      dirtyMessages.addAll((data['dirty_messages'] as List? ?? []).cast<String>());
      lastRev = (data['last_rev'] as num?)?.toInt() ?? 0;
      dbId = data['db_id'] as String?;
    } catch (e) {
      // 壊れていたら退避して空から始める(会話は PC からもう一度同期できる)
      await file.rename('${file.path}.broken-${DateTime.now().millisecondsSinceEpoch}');
    }
  }

  void _changed() {
    notifyListeners();
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 700), save);
  }

  Future<void> save() async {
    _saveTimer?.cancel();
    final data = {
      'version': 1,
      'last_rev': lastRev,
      'db_id': dbId,
      'conversations': [for (final c in conversations.values) c.toJson()],
      'messages': [for (final m in messages.values) m.toJson()],
      'dirty_conversations': dirtyConversations.toList(),
      'dirty_messages': dirtyMessages.toList(),
    };
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString(jsonEncode(data), flush: true);
    await tmp.rename(file.path);
  }

  // ---- 読み出し ----

  List<Conversation> listConversations({String query = ''}) {
    final q = query.trim().toLowerCase();
    final list = conversations.values.where((c) => !c.deleted).where((c) {
      if (q.isEmpty) return true;
      if (c.title.toLowerCase().contains(q)) return true;
      return messages.values.any((m) => m.conversationId == c.id && !m.deleted && m.content.toLowerCase().contains(q));
    }).toList();
    list.sort((a, b) {
      if (a.pinned != b.pinned) return a.pinned ? -1 : 1;
      return b.updated.compareTo(a.updated);
    });
    return list;
  }

  List<Message> messagesOf(String conversationId) {
    final list = messages.values.where((m) => m.conversationId == conversationId && !m.deleted).toList();
    // 同じ時刻なら、依頼(user)を回答より前に置く(id は乱数なので、id だけで決めると順番が入れ替わる)
    list.sort((a, b) => a.created != b.created
        ? a.created.compareTo(b.created)
        : a.role != b.role
            ? _roleOrder(a.role).compareTo(_roleOrder(b.role))
            : a.id.compareTo(b.id));
    return list;
  }

  static int _roleOrder(String role) => switch (role) { 'system' => 0, 'user' => 1, _ => 2 };

  String previewOf(String conversationId) {
    final list = messagesOf(conversationId);
    if (list.isEmpty) return '';
    return previewText(list.last.content);
  }

  // ---- 端末での変更(未送信の印を付ける) ----

  Conversation createConversation({String? id, String title = '', required String origin, bool dirty = true}) {
    final now = nowSeconds();
    final conv = Conversation(id: id ?? newId(), title: title, created: now, updated: now, origin: origin);
    conversations[conv.id] = conv;
    if (dirty) dirtyConversations.add(conv.id);
    _changed();
    return conv;
  }

  Message addMessage(String conversationId, String role, String content,
      {String? id, Map<String, dynamic>? meta, bool dirty = true, double? created}) {
    var now = nowSeconds();
    // 同じ会話の中では、後から足したメッセージの時刻を必ず後にする(時計の細かさが粗い端末でも順番が崩れないように)
    for (final m in messages.values) {
      if (m.conversationId == conversationId && m.created >= now) now = m.created + 1e-6;
    }
    final msg = Message(id: id ?? newId(), conversationId: conversationId, role: role, content: content, created: created ?? now, updated: now, meta: meta);
    messages[msg.id] = msg;
    final conv = conversations[conversationId];
    if (conv != null) {
      conv.updated = now;
      if (conv.title.isEmpty && role == 'user') {
        conv.title = titleFrom(content);
        if (dirty) dirtyConversations.add(conv.id);
      }
    }
    if (dirty) dirtyMessages.add(msg.id);
    _changed();
    return msg;
  }

  void updateMessage(String id, {String? content, Map<String, dynamic>? meta, bool dirty = true}) {
    final msg = messages[id];
    if (msg == null) return;
    if (content != null) msg.content = content;
    if (meta != null) msg.meta = {...msg.meta, ...meta};
    msg.updated = nowSeconds();
    if (dirty) dirtyMessages.add(id);
    _changed();
  }

  void updateConversation(String id, {String? title, bool? pinned, bool? deleted}) {
    final conv = conversations[id];
    if (conv == null) return;
    if (title != null) conv.title = title;
    if (pinned != null) conv.pinned = pinned;
    if (deleted != null) conv.deleted = deleted;
    conv.updated = nowSeconds();
    dirtyConversations.add(id);
    if (deleted == true) {
      for (final m in messages.values.where((m) => m.conversationId == id)) {
        m.deleted = true;
      }
    }
    _changed();
  }

  // ---- PC からの変更の取り込み(後勝ち。未送信の端末側の変更が新しければ残す) ----

  void applyRemote({required List<Map<String, dynamic>> conversations, required List<Map<String, dynamic>> messages, required int rev}) {
    for (final j in conversations) {
      final remote = Conversation.fromJson(j);
      final local = this.conversations[remote.id];
      if (local != null && dirtyConversations.contains(remote.id) && local.updated > remote.updated) continue;
      this.conversations[remote.id] = remote;
      dirtyConversations.remove(remote.id);
    }
    for (final j in messages) {
      final remote = Message.fromJson(j);
      final local = this.messages[remote.id];
      if (local != null && dirtyMessages.contains(remote.id) && local.updated > remote.updated) continue;
      this.messages[remote.id] = remote;
      dirtyMessages.remove(remote.id);
    }
    lastRev = rev;
    _changed();
  }

  /// PC に送る未送信の変更。
  ({List<Map<String, dynamic>> conversations, List<Map<String, dynamic>> messages}) pending() => (
        conversations: [for (final id in dirtyConversations) if (conversations[id] != null) conversations[id]!.toJson()],
        messages: [for (final id in dirtyMessages) if (messages[id] != null) messages[id]!.toJson()],
      );

  void markSent(Iterable<Map<String, dynamic>> conversations, Iterable<Map<String, dynamic>> messages) {
    for (final c in conversations) {
      final local = this.conversations[c['id']];
      if (local != null && local.updated == (c['updated'] as num).toDouble()) dirtyConversations.remove(c['id']);
    }
    for (final m in messages) {
      final local = this.messages[m['id']];
      if (local != null && local.updated == (m['updated'] as num).toDouble()) dirtyMessages.remove(m['id']);
    }
    _changed();
  }

  int get pendingCount => dirtyConversations.length + dirtyMessages.length;
}
