/// 利用者に合わせる(AIへの指示・覚えていること)と、今日の日付。PC 側(manager.py)と同じ決まり。
///
/// スマホの中のAIは小さいので、日付の計算や「覚えて」の判断はAIに任せず、ここで決まった形で行う。
library;

final _kana = RegExp(r'[぀-ヿ]');
final _han = RegExp(r'[一-鿿]');
final _foreign = RegExp(r'英語|英文|英訳|English|中国語|中文|韓国語|ハングル|フランス語|ドイツ語|スペイン語|イタリア語|ロシア語|ポルトガル語',
    caseSensitive: false);

/// 指示文に足す「利用者に合わせる」部分(どちらも空なら空)。
String personalPrompt(String instructions, List<String> memories) {
  final parts = <String>[];
  if (instructions.trim().isNotEmpty) parts.add('利用者からの指示(いつも守ること):\n${instructions.trim()}');
  if (memories.isNotEmpty) {
    parts.add('利用者について覚えていること(文中の「私」は利用者。関係があるときだけ使い、毎回は触れない):\n'
        '${memories.map((m) => '- $m').join('\n')}');
  }
  return parts.isEmpty ? '' : '\n\n${parts.join('\n\n')}';
}

// ---- 「覚えて」「忘れて」「何を覚えてる?」 ----

final _remember = [
  RegExp(r'^(?:覚えて(?:おいて)?|記憶して(?:おいて)?|メモして(?:おいて)?)(?:ください)?\s*[:：、,]\s*(.+)$', dotAll: true),
  RegExp(r'^(.+?)(?:だ|です)?(?:ということ|こと)?(?:を|と|って)(?:覚えて|記憶して)(?:おいて)?(?:ください|ね|ほしい)?[。.!！]*$', dotAll: true),
  RegExp(r'^remember(?: that)?[:,]?\s+(.+)$', caseSensitive: false, dotAll: true),
];
final _forget = [
  RegExp(r'^(?:忘れて|削除して)(?:ください)?\s*[:：、,]\s*(.+)$', dotAll: true),
  RegExp(r'^(.+?)(?:のこと)?(?:を|は)忘れて(?:ください|ね)?[。.!！]*$', dotAll: true),
  RegExp(r'^forget(?: that)?[:,]?\s+(.+)$', caseSensitive: false, dotAll: true),
];
final _recall = RegExp(
    r'^(?:何を覚えて(?:い)?る(?:の)?|覚えていること(?:を(?:教えて|見せて))?|メモリ(?:を見せて|の一覧)?|what do you remember)(?:ください)?[。.?？]*$',
    caseSensitive: false);
final _demonstrative = RegExp(r'^(?:それ|これ|あれ|この|その|あの|上の|上記|今の|さっき|先ほど)');

/// 「覚えて: 〜」→ ('remember', 内容)、「〜を忘れて」→ ('forget', 内容)、「何を覚えてる?」→ ('recall', '')。当てはまらなければ null。
(String, String)? memoryCommand(String text) {
  text = text.trim();
  if (text.length > 400 || text.contains('\n\n')) return null;
  if (_recall.hasMatch(text)) return ('recall', '');
  for (final (kind, patterns) in [('remember', _remember), ('forget', _forget)]) {
    for (final pattern in patterns) {
      final m = pattern.firstMatch(text);
      if (m == null) continue;
      final fact = m.group(1)!.replaceAll(RegExp(r'\s+'), ' ').trim().replaceAll(RegExp(r'^[ 「」『』"' "'" r'。.、,]+|[ 「」『』"' "'" r'。.、,]+$'), '');
      if (fact.isEmpty || _demonstrative.hasMatch(fact)) return null;
      return (kind, fact.length > 200 ? fact.substring(0, 200) : fact);
    }
  }
  return null;
}

// ---- 今日の日付 ----

final _timeWords = RegExp(
    r'今日|明日|明後日|昨日|一昨日|今週|来週|先週|今月|来月|先月|今年|来年|去年|昨年|曜日|何日|何時|時刻|現在|いま何|今何|'
    r'日付|日時|何歳|経過|あと\s*\d*\s*(?:日|週間|か月|ヶ月|年)|締め切り|期限|年末|月末|'
    r'\b(?:today|tomorrow|yesterday|tonight|weekday|now|current (?:date|time)|this (?:week|month|year)|next (?:week|month|year)|'
    r'last (?:week|month|year)|what day|what time|how many days)\b',
    caseSensitive: false);
const _weekdays = '月火水木金土日';
const _weekdaysEn = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const _monthsEn = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/// 今日の日付や今の時刻で答えが変わる依頼か。
bool timeSensitive(String task) => _timeWords.hasMatch(task);

String _wd(DateTime d) => _weekdays[d.weekday - 1];

String _jaDate(DateTime d, DateTime today) => '${d.year == today.year ? '' : '${d.year}年'}${d.month}月${d.day}日(${_wd(d)})';

/// 日付や時刻が関わる依頼のための「今日の日時」の一文(関わらない依頼なら空)。
String timeContext(String task, DateTime now, {List<String> memories = const []}) {
  if (!timeSensitive(task)) return '';
  if (_kana.hasMatch(task) || _han.hasMatch(task)) {
    final facts = dateFacts(task, memories, DateTime(now.year, now.month, now.day));
    return '\n\n今日は ${now.year}年${now.month}月${now.day}日(${_wd(now)}曜日)、今の時刻は ${now.hour}時${now.minute.toString().padLeft(2, '0')}分です。'
        '日付や曜日、日数の計算にはこれを使ってください。'
        '${facts.isEmpty ? '' : '\n計算済みの値(正確なので、日数や曜日はこれをそのまま使う): ${facts.join(' / ')}'}';
  }
  return '\n\nToday is ${_weekdaysEn[now.weekday - 1]}, ${_monthsEn[now.month - 1]} ${now.day.toString().padLeft(2, '0')}, ${now.year} '
      'and the current time is ${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}. Use this for dates and day counts.';
}

const _namedDays = {
  '大晦日': (12, 31), '大みそか': (12, 31), '年末': (12, 31), '元日': (1, 1), '元旦': (1, 1), '正月': (1, 1),
  'クリスマスイブ': (12, 24), 'クリスマス': (12, 25), 'バレンタイン': (2, 14), 'ホワイトデー': (3, 14),
  'ハロウィン': (10, 31), '七夕': (7, 7), 'こどもの日': (5, 5),
};
final _dateJa = RegExp(r'(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日');
final _relativeJa = RegExp(r'(\d+)\s*(日|週間|か月|ヶ月|カ月|年)\s*(後|前)');

/// 依頼に出てくる日付までの日数と曜日、「100日後」のような日付を、計算して文にする。
List<String> dateFacts(String task, List<String> memories, DateTime today) => [for (final (fact, _, _) in _dateItems(task, memories, today)) fact];

DateTime? _validDate(int y, int m, int d) {
  final date = DateTime(y, m, d);
  return date.month == m && date.day == d ? date : null;
}

int _daysBetween(DateTime from, DateTime to) => DateTime.utc(to.year, to.month, to.day).difference(DateTime.utc(from.year, from.month, from.day)).inDays;

List<(String, String, DateTime)> _dateItems(String task, List<String> memories, DateTime today) {
  final found = <(String, int?, int, int)>[];
  for (final m in _dateJa.allMatches(task)) {
    found.add(('', m.group(1) == null ? null : int.parse(m.group(1)!), int.parse(m.group(2)!), int.parse(m.group(3)!)));
  }
  _namedDays.forEach((name, md) {
    if (task.contains(name) && !(name == 'クリスマス' && task.contains('クリスマスイブ'))) found.add((name, null, md.$1, md.$2));
  });
  for (final memory in memories) {
    final words = RegExp(r'[一-鿿ァ-ヿ]{2,}').allMatches(memory.replaceAll(_dateJa, ' ')).map((m) => m.group(0)!).toList();
    final hit = words.where(task.contains).toList();
    if (hit.isEmpty) continue;
    for (final m in _dateJa.allMatches(memory)) {
      found.add((hit.first, m.group(1) == null ? null : int.parse(m.group(1)!), int.parse(m.group(2)!), int.parse(m.group(3)!)));
    }
  }
  final facts = <(String, String, DateTime)>[];
  for (final (name, year, month, day) in found) {
    var target = _validDate(year ?? today.year, month, day);
    if (target == null) continue;
    if (year == null && target.isBefore(today)) {
      target = _validDate(today.year + 1, month, day);
      if (target == null) continue;
    }
    final label = name.isEmpty ? _jaDate(target, today) : '$name(${_jaDate(target, today)})';
    final days = _daysBetween(today, target);
    final kind = days > 0 ? 'until' : days == 0 ? 'today' : 'since';
    final fact = days > 0 ? '$labelまで あと$days日' : days == 0 ? '$labelは今日' : '$labelから今日まで ${-days}日';
    if (!facts.any((f) => f.$1 == fact)) facts.add((fact, kind, target));
  }
  for (final m in _relativeJa.allMatches(task)) {
    final n = int.parse(m.group(1)!), unit = m.group(2)!, sign = m.group(3) == '後' ? 1 : -1;
    DateTime target;
    if (unit == '日' || unit == '週間') {
      target = DateTime(today.year, today.month, today.day + sign * n * (unit == '週間' ? 7 : 1));
    } else {
      final months = sign * n * (unit == '年' ? 12 : 1);
      final total = today.year * 12 + today.month - 1 + months;
      final y = total ~/ 12, mo = total % 12 + 1;
      final last = DateTime(y, mo + 1, 0).day;
      target = DateTime(y, mo, today.day < last ? today.day : last);
    }
    facts.add(('${m.group(0)}は ${_jaDate(target, today)}', 'relative', target));
  }
  return facts.length > 6 ? facts.sublist(0, 6) : facts;
}

final _simpleDateQ = RegExp(r'何曜日|あと何日|何日後|何日前|週間後|週間前|か月後|ヶ月後|カ月後|か月前|ヶ月前|カ月前|年後|年前|何日経|何日たっ|'
    r'何月何日|日付は|何日[?？]?$|いつ[?？]?$');
final _conditional = RegExp(r'営業日|平日|祝日|休日|土日|除い|除く|以外|時間|何時|分後|秒');
const _nearDays = {'今日': 0, '明日': 1, '明後日': 2, '昨日': -1, '一昨日': -2};

/// 短い日付の質問(「大晦日まであと何日?」「5月3日は何曜日?」など)に、計算した値でそのまま答える。AIに任せるなら null。
String? dateAnswer(String task, List<String> memories, DateTime today) {
  final text = task.trim();
  if (text.length > 40 || text.contains('\n') || !_simpleDateQ.hasMatch(text) || _conditional.hasMatch(text)) return null;
  final near = RegExp(r'^(今日|明日|明後日|昨日|一昨日)(?:は|って)?(?:何曜日|何日|何月何日|いつ)').firstMatch(text);
  if (near != null && !_dateJa.hasMatch(text)) {
    final day = DateTime(today.year, today.month, today.day + _nearDays[near.group(1)]!);
    return '${near.group(1)}は ${day.year}年${day.month}月${day.day}日(${_wd(day)}曜日)です。';
  }
  final items = _dateItems(text, memories, DateTime(today.year, today.month, today.day));
  if (items.length != 1) return null;
  final (fact, kind, target) = items.first;
  if (kind == 'relative') return '$factです。';
  if (text.contains('何曜日')) return '${target.year == today.year ? '' : '${target.year}年'}${target.month}月${target.day}日は${_wd(target)}曜日です。';
  return '$factです。';
}

/// 日本語で答えるべき依頼か(スマホの中のAIへの言語の指示に使う)。
bool wantsJapanese(String text) => _kana.hasMatch(text) && !_foreign.hasMatch(text);
