import 'dart:math';

/// PC と同じ形の会話とメッセージ(同期でそのまま送り合う)。
class Conversation {
  Conversation({
    required this.id,
    this.title = '',
    required this.created,
    required this.updated,
    this.pinned = false,
    this.deleted = false,
    this.origin = 'phone',
    Map<String, dynamic>? meta,
  }) : meta = meta ?? {};

  final String id;
  String title;
  double created;
  double updated;
  bool pinned;
  bool deleted;
  String origin;
  Map<String, dynamic> meta;

  factory Conversation.fromJson(Map<String, dynamic> j) => Conversation(
        id: j['id'] as String,
        title: (j['title'] ?? '') as String,
        created: (j['created'] as num).toDouble(),
        updated: (j['updated'] as num).toDouble(),
        pinned: j['pinned'] == true || j['pinned'] == 1,
        deleted: j['deleted'] == true || j['deleted'] == 1,
        origin: (j['origin'] ?? 'pc') as String,
        meta: Map<String, dynamic>.from((j['meta'] as Map?) ?? {}),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'created': created,
        'updated': updated,
        'pinned': pinned,
        'deleted': deleted,
        'origin': origin,
        'meta': meta,
      };
}

class Message {
  Message({
    required this.id,
    required this.conversationId,
    required this.role,
    required this.content,
    required this.created,
    required this.updated,
    this.deleted = false,
    Map<String, dynamic>? meta,
  }) : meta = meta ?? {};

  final String id;
  final String conversationId;
  final String role;
  String content;
  double created;
  double updated;
  bool deleted;
  Map<String, dynamic> meta;

  bool get isUser => role == 'user';
  String get status => (meta['status'] ?? 'done') as String;
  bool get running => status == 'running';
  bool get fromLocalAi => (meta['model'] as String?)?.startsWith('local:') ?? false;

  factory Message.fromJson(Map<String, dynamic> j) => Message(
        id: j['id'] as String,
        conversationId: j['conversation_id'] as String,
        role: j['role'] as String,
        content: (j['content'] ?? '') as String,
        created: (j['created'] as num).toDouble(),
        updated: (j['updated'] as num).toDouble(),
        deleted: j['deleted'] == true || j['deleted'] == 1,
        meta: Map<String, dynamic>.from((j['meta'] as Map?) ?? {}),
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'conversation_id': conversationId,
        'role': role,
        'content': content,
        'created': created,
        'updated': updated,
        'deleted': deleted,
        'meta': meta,
      };
}

double nowSeconds() => DateTime.now().microsecondsSinceEpoch / 1e6;

final _random = Random.secure();

/// PC と同じ形式の id(32桁の16進数)。
String newId() => List.generate(16, (_) => _random.nextInt(256).toRadixString(16).padLeft(2, '0')).join();

/// 表示用の短い日時。
String shortTime(double seconds) {
  final d = DateTime.fromMicrosecondsSinceEpoch((seconds * 1e6).round());
  final now = DateTime.now();
  String two(int n) => n.toString().padLeft(2, '0');
  if (d.year == now.year && d.month == now.month && d.day == now.day) return '${two(d.hour)}:${two(d.minute)}';
  if (d.year == now.year) return '${d.month}/${d.day}';
  return '${d.year}/${d.month}/${d.day}';
}

String formatBytes(num bytes) {
  if (bytes >= 1e9) return '${(bytes / 1e9).toStringAsFixed(2)} GB';
  if (bytes >= 1e6) return '${(bytes / 1e6).toStringAsFixed(1)} MB';
  if (bytes >= 1e3) return '${(bytes / 1e3).toStringAsFixed(0)} KB';
  return '$bytes B';
}

String titleFrom(String text) {
  final first = text.trim().split('\n').first.replaceAll(RegExp(r'\s+'), ' ');
  return first.length > 40 ? '${first.substring(0, 40)}…' : first;
}

/// 会話の題名から、保存するファイルの名前(拡張子なし)を作る(PC の office.file_name と同じ決まり)。
/// ファイル名に使えない文字を除き、途中で切られた題名(末尾が「…」)は読点などの切れ目まで戻す。
String fileNameFrom(String title, {int limit = 40}) {
  var name = title.replaceAll(RegExp(r'[\\/:*?"<>|\x00-\x1f]+'), ' ').replaceAll(RegExp(r'\s+'), ' ').trim();
  final cut = name.endsWith('…') || name.endsWith('...') || name.runes.length > limit;
  name = String.fromCharCodes(name.replaceAll(RegExp(r'[…. ]+$'), '').runes.take(limit));
  if (cut) {
    var at = -1;
    for (final mark in const ['、', '。', '，', ',', ';', ':', '!', '?', '！', '？']) {
      final i = name.lastIndexOf(mark);
      if (i > at) at = i;
    }
    if (at < limit ~/ 3) at = name.lastIndexOf(' ');
    if (at >= limit ~/ 3) name = name.substring(0, at);
  }
  name = name.replaceAll(RegExp(r'^[ 、。,.]+|[ 、。,.]+$'), '');
  return name.isEmpty ? '回答' : name;
}

/// 表の見出しの下の区切りの行(| --- | --- |)。
final tableDelimiter = RegExp(r'^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$');

/// 表の1行のセル。
List<String> tableCells(String line) =>
    line.trim().replaceAll(RegExp(r'^\|+|\|+$'), '').split('|').map((c) => c.trim()).toList();

/// 両端の | を省いた表(「月 | 売上」の次の行が「--- | ---」。GFM では正しい書き方)を、両端に | のある形にそろえる。
/// コードブロックの中は変えない。PC 側(Python・画面)と同じ決まり。
String pipeTables(String text) {
  final lines = text.split('\n');
  final out = <String>[];
  var fenced = false;
  for (var i = 0; i < lines.length; i++) {
    final line = lines[i];
    if (line.trim().startsWith('```')) {
      fenced = !fenced;
    } else if (!fenced &&
        line.contains('|') &&
        i + 1 < lines.length &&
        lines[i + 1].contains('|') &&
        tableDelimiter.hasMatch(lines[i + 1]) &&
        tableCells(line).length == tableCells(lines[i + 1]).length) {
      var j = i + 2;
      while (j < lines.length && lines[j].contains('|') && !lines[j].trim().startsWith('```')) {
        j++;
      }
      for (final row in lines.sublist(i, j)) {
        out.add('| ${tableCells(row).join(' | ')} |');
      }
      i = j - 1;
      continue;
    }
    out.add(line);
  }
  return out.join('\n');
}

/// Excel にできる表(2行以上の Markdown の表、または CSV のコードブロック)があるか。
bool hasTable(String text) {
  var rows = 0;
  for (final line in pipeTables(text).split('\n')) {
    if (!line.trim().startsWith('|')) {
      rows = 0;
      continue;
    }
    if (!tableDelimiter.hasMatch(line)) rows++;
    if (rows >= 2) return true;
  }
  return RegExp(r'```(?:csv|tsv)[ \t]*\n', caseSensitive: false).hasMatch(text);
}

final _tableRow = RegExp(r'^\s*\|.*\|\s*$');

/// 会話の一覧に出す1行: Markdown の書式の記号と添付ファイルの中身を除く。表は「セル セル / セル セル」にする。
/// 書式の記号だけを除き、掛け算の「17 * 23」のような記号は残す。
String previewText(String content) {
  final body = content
      .replaceAll(RegExp(r'\n\n\[添付ファイル: ([^\]\n]+)\]\n````\n[\s\S]*?\n````'), ' ')
      .replaceAll(RegExp(r'```[\s\S]*?(```|$)'), ' ');
  final text = pipeTables(body)
      .split('\n')
      .map((l) => l.contains('|') && tableDelimiter.hasMatch(l) ? '' : _tableRow.hasMatch(l) ? '${tableCells(l).join(' ')} /' : l)
      .join('\n')
      .replaceAll(RegExp(r'^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+', multiLine: true), '')
      .replaceAll(RegExp(r'\*\*|__|`'), '')
      .replaceAllMapped(RegExp(r'(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])'), (m) => m[1]!)
      .replaceAllMapped(RegExp(r'\[([^\]\n]+)\]\([^)\s]+\)'), (m) => m[1]!);
  return text.replaceAll(RegExp(r'\s+'), ' ').trim().replaceAll(RegExp(r'\s*/$'), '');
}

/// 末尾が同じ文の繰り返しになっていたら、繰り返しの1回分までを返す(なっていなければ null)。
///
/// 小さいAIは同じ文を上限まで書き続けることがあり、時間と電池を無駄にする。PC 側(Python)と同じ判定。
String? loopCut(String text, {int span = 80, int times = 3}) {
  if (text.length < span * times) return null;
  final tail = text.substring(text.length - span);
  if (tail.trim().isEmpty) return null;
  final window = text.length > 4000 ? text.substring(text.length - 4000) : text;
  var count = 0, from = 0;
  while (true) {
    final i = window.indexOf(tail, from);
    if (i < 0) break;
    count++;
    from = i + tail.length;
  }
  if (count < times) return null;
  final first = text.indexOf(tail);
  final second = text.indexOf(tail, first + 1);
  if (second <= 0) return null;
  final period = second - first;
  var start = first; // 繰り返しの始まりまで戻り、ちょうど1回分を残す
  while (start > 0 && text[start - 1] == text[start - 1 + period]) {
    start--;
  }
  final end = start + period;
  final rest = text.substring(end, min(text.length, end + period));
  var stop = -1; // 文の途中で切れないように、次の文の区切りまでは残す
  for (final c in const ['。', '\n', '.', '!', '?', '！', '？']) {
    final i = rest.indexOf(c);
    if (i >= 0 && (stop < 0 || i < stop)) stop = i;
  }
  return text.substring(0, stop >= 0 ? end + stop + 1 : end).trimRight();
}
