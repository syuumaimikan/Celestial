"""管理AI: タスクを振り分け、ワーカー(小型AI)群を動かし、結果を学習層に記録する。"""

from __future__ import annotations

import datetime
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ground
from .backends import Backend, BackendError, Generation, Message, ModelInfo, create_backend
from .backends.base import TIERS
from .backends.native import read_models
from .config import Config, load_config
from .evolve import Embedder, LearnedRouter, Memory, Store
from . import decide, project, web
from .agent import run_agent
from .tools import ToolContext, ToolError, summarize_changes
from .workers import COMPLEX, COMPLEX_DESCRIPTION, ROLES, heuristic_role, plausible_role

OnToken = Callable[[str], None]
OnStage = Callable[[str, dict], None]  # 進行状況の通知(画面表示用)
TUNABLE: dict[str, type] = {"cache": bool, "learned_routing": bool, "swarm_auto": bool, "fewshot_k": int, "manager_model": str,
                             "semantic_search": bool, "cascade": bool, "workspace": str, "tools_write": bool, "tools_run": bool,
                             "tools_pc": bool, "tools_net": bool, "tools_confirm": bool,
                             "remote_enabled": bool, "remote_control": bool, "keep_running": bool,
                             "instructions": str, "memories": list, "memory_suggest": bool}
MAX_INSTRUCTIONS = 1000   # AIへの指示の長さの上限(小さいAIの指示文を長くしすぎない)
MAX_MEMORIES = 50         # 覚えておける件数
MAX_MEMORY_CHARS = 200    # 1件の長さの上限
_TEACHER_ROUTES = ("manager", "forced", "plan")
_CLIP = 1500

# ---- 確からしさで答え直す(カスケード) --------------------------------------------
# 回答の出だし _WINDOW トークンの平均の対数確率を「確からしさ」とする。実験(50問・2モデル)では、
# 小さいモデルの正解は平均 -0.25、誤答は -0.40 で、出だしだけでも誤答をかなり見分けられた。
_WINDOW = 40
_MIN_TOKENS = 4                    # これより短い回答は、確からしさの値がぶれるので判断に使わない
DEFAULT_CASCADE_THRESHOLD = -0.25  # 計測データがまだないときの、答え直す境目
FACT_THRESHOLD = -0.015            # 事実の短い回答で、これより自信がなければ資料で確かめる(計測の誤答はすべて下回った)
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")
_ZH_MARKS = re.compile(r"[的是了这们个为说么吗呢让对时间请设计还过给从样关应该现，]")
_FOREIGN = re.compile(r"英語|英文|英訳|English|中国語|中文|韓国語|ハングル|フランス語|ドイツ語|スペイン語|イタリア語|ロシア語|ポルトガル語", re.I)
_JA_HINT = "\n必ず自然な日本語で答えてください。"  # 小さいモデルが中国語や英語で答えてしまうのを防ぐ(12問で 5→12問が日本語に)
_LANG_ROLES = ("general", "code", "reason", "summarize", "write")


_ATTACHMENT = re.compile(r"\n\n\[添付ファイル: [^\]\n]+\]\n````\n[\s\S]*?\n````")


def routing_text(task: str) -> str:
    """振り分け用の依頼文: 添付ファイルの中身を除く(中身の ``` やプログラムで「コード」に誤って振り分けないように)。"""
    stripped = _ATTACHMENT.sub("", task).strip()
    if stripped != task.strip():
        stripped += "\n(文書ファイルが添付されている)"
    return stripped or task


def wants_japanese(task: str, role_name: str) -> bool:
    """日本語で答えるべき依頼か(翻訳・抽出や、他の言語を指定した依頼は除く)。"""
    return role_name in _LANG_ROLES and bool(_KANA.search(task)) and not _FOREIGN.search(task)


def wrong_language(text: str, role_name: str) -> bool:
    """日本語の依頼への回答(の出だし)が、中国語や英語になっていないか。"""
    kana, han = len(_KANA.findall(text)), len(_HAN.findall(text))
    if han >= 12 and kana < 0.1 * (kana + han) and len(_ZH_MARKS.findall(text)) >= 2:
        return True  # 漢字ばかりでかながほぼなく、中国語に特有の字がある
    letters = len(re.findall(r"[A-Za-z]", text))
    return role_name != "code" and kana == 0 and han == 0 and letters >= 40 and "```" not in text


_ZH_ONLY = re.compile(r"[这们个为说么吗呢让对时间请设计还过给从样关应该现]")  # 日本語では使わない簡体字
# 題名の付け方: 決まりと、例を会話の形で渡す(例を指示文に並べるだけだと、小さいAIは例の続きを作ってしまう)。
# 実験(10問): 回答の出だしも渡すと「日付: 12月3日」のように回答を写すので、依頼だけを渡す。
_TITLE_RULES = {
    "ja": ("会話の一覧に出す短い題名を付けます。依頼の内容を表す、名詞で終わる短い題名(6〜20文字)だけを1行で書いてください。"
           "金額・日付・メールアドレス・人名などの細かいデータや答えそのものは書かず、「〜しました」のような文にもしないでください。"),
    "en": ("You title conversations for a conversation list. Reply with only a short noun phrase (2-6 words) describing the request, "
           "in English, without quotes or a period. Do not copy specific data or the answer."),
}
_TITLE_SHOTS = {
    "ja": [("来月の旅行の持ち物リストを作って", "旅行の持ち物リスト"),
           ("次の文章を3行で要約して: 近年、都市部では…", "文章の3行要約"),
           ("JavaScriptで配列を並べ替える方法は?", "JavaScript の配列の並べ替え"),
           ("次の文から人名を抜き出して: 佐藤さんと鈴木さんが…", "文からの人名の抽出")],
    "en": [("Make a packing list for my trip next month", "Trip packing list"),
           ("Summarize this text in 3 lines: In recent years…", "Three-line text summary"),
           ("How do I sort an array in JavaScript?", "Sorting arrays in JavaScript")],
}
_SENTENCE_END = re.compile(r"(です|ます|ました|ません|ください|して|しよう|ですか|[??])$")


def title_messages(task: str) -> list[dict]:
    """題名を付けてもらうための会話(依頼の言語に合わせる)。"""
    lang = "ja" if (_KANA.search(task) or _HAN.search(task)) else "en"
    label = "依頼" if lang == "ja" else "Request"
    messages: list[dict] = [{"role": "system", "content": _TITLE_RULES[lang]}]
    for question, answer in _TITLE_SHOTS[lang]:
        messages += [{"role": "user", "content": f"{label}: {question}"}, {"role": "assistant", "content": answer}]
    return messages + [{"role": "user", "content": f"{label}: {routing_text(task)[:300]}"}]


def clean_title(text: str, task: str) -> str | None:
    """AIが書いた題名を整える。題名として使えない形(長すぎる・文になっている・言語が違うなど)なら None。"""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    line = next((l.strip() for l in text.strip().splitlines() if l.strip()), "")
    line = re.sub(r"^(題名|タイトル|件名|title)\s*[:：]\s*", "", line, flags=re.I)
    line = re.sub(r"\s+", " ", line.strip(" \t「」『』【】\"'`*#・-—。.")).strip()
    if not line or len(line) > 30 or _SENTENCE_END.search(line) or re.search(r"\S+@\S+|https?://", line):
        return None
    if not re.search(r"[A-Za-z぀-ヿ一-鿿]", line):
        return None  # 数や記号だけ(答えを書いてしまった)
    if _KANA.search(task) or _HAN.search(task):
        if not _FOREIGN.search(task) and (not (_KANA.search(line) or _HAN.search(line)) or (not _KANA.search(line) and _ZH_ONLY.search(line))):
            return None  # 日本語の依頼に、英語や中国語の題名
    elif _KANA.search(line) or _HAN.search(line):
        return None  # 英語の依頼に日本語の題名
    return line


# ---- 今日の日付 --------------------------------------------------------------------
# AIは今日の日付を知らないので、日付や時刻が関わる依頼にだけ今の日時を渡す(関わらない依頼の指示文は変えない:
# 計測の比べやすさと、Ollama の指示文の再利用のため)。日時で答えが変わるので、こうした依頼はキャッシュを使わない。
_TIME_WORDS = re.compile(
    r"今日|明日|明後日|昨日|一昨日|今週|来週|先週|今月|来月|先月|今年|来年|去年|昨年|曜日|何日|何時|時刻|現在|いま何|今何|"
    r"日付|日時|何歳|経過|あと\s*\d*\s*(?:日|週間|か月|ヶ月|年)|締め切り|期限|年末|月末|"
    r"\b(?:today|tomorrow|yesterday|tonight|weekday|now|current (?:date|time)|this (?:week|month|year)|next (?:week|month|year)|"
    r"last (?:week|month|year)|what day|what time|how many days)\b", re.I)
_WEEKDAYS_JA = "月火水木金土日"


def time_sensitive(task: str) -> bool:
    """今日の日付や今の時刻で答えが変わる依頼か(最新のニュースや情報を求める依頼も含む)。"""
    return bool(_TIME_WORDS.search(task) or _LIVE.search(task))


# 最新の情報が要る依頼。ネットが使えれば最新のニュースを材料に答え、使えなければ「取得できない」と最初に伝えさせる
# (使わないと、小さいAIは学習した古い知識から「最新ニュース」を作り上げてしまう)
_NEWS = re.compile(r"ニュース(?!レター|リリース)|速報|時事|今日の出来事|最近の出来事|話題になって|\b(news|headlines)\b(?!\s*(?:letter|release))", re.I)
_LIVE = re.compile(r"最新|ニュース(?!レター|リリース)|速報|時事|天気予報|今日の天気|明日の天気|株価|為替|相場|現在の(価格|値段|状況)"
                   r"|\b(news|headlines|latest|weather forecast|stock price)\b(?!\s*(?:letter|release))", re.I)
# ニュースの話題: 「AIに関する最新ニュース」の「AI」のような、漢字・カタカナ・英数字の続き(「AIや半導体」のような並びも)
_TOPIC_CHARS = "一-鿿々〆゠-ヿｦ-ﾟA-Za-z0-9０-９Ａ-Ｚａ-ｚ"
_TOPIC_RUN = rf"[{_TOPIC_CHARS}][{_TOPIC_CHARS}+#&.\- ]*"
_TOPIC_JA = re.compile(rf"((?:{_TOPIC_RUN})(?:[やと、](?:{_TOPIC_RUN}))*)\s*"
                       r"(?:に関する|についての|に関連する|に関係する|関連の|関係の|まわりの|界隈の|の)?"
                       r"(?:最新|最近|今日|本日|直近|主要|新着)?の?(?:ニュース(?!レター|リリース)|速報)")
_TOPIC_LEAD = re.compile(r"^(?:最新|最近|今日|本日|昨日|今週|今月|直近|新着)の?")
_TOPIC_GENERIC = re.compile(r"(?:最新|最近|今日|本日|昨日|今週|今月|直近|新着|主要|注目|話題|重大|重要|トップ|ニュース|速報"
                            r"|国内|日本|全国|全般|一般|色々|様々|今|[・ ])+")
_TOPIC_EN = re.compile(r"\bnews\s+(?:about|on|regarding|related to|of|in)\s+(?:the\s+)?([A-Za-z0-9][\w.+#&'\- ]{0,40}?)"
                       r"\s*(?:[?.!,;]|$|\b(?:today|this week|lately|recently|please|and summarize)\b)", re.I)
_TOPIC_EN_BEFORE = re.compile(r"\b([A-Za-z0-9][\w.+#&'\-]*(?:\s+[A-Za-z0-9][\w.+#&'\-]*){0,3})\s+(?:news|headlines)\b(?!\s*(?:letter|release))", re.I)
_EN_FILLER = set("latest recent today today's todays top the some any me us give tell show get find fetch list read check search "
                 "summarize summarise collect gather what what's whats is are breaking current big major important a few of for "
                 "and all daily new news please can you lot lots many headlines key main this week's".split())
_OFFLINE_NOTE = ("\n\n注意: あなたはインターネットに接続できず、最新のニュース・天気・価格などは取得できません。"
                 "そうした情報を求められたら、まずそのことを一言伝え、知っている範囲のことは古い可能性があると分かるように答えてください。"
                 "作り話のニュースを書いてはいけません。")
_OFFLINE_NOTE_EN = ("\n\nNote: you cannot access the internet, so you cannot get the latest news, weather or prices. If asked for them, "
                    "say so first, and mark anything you know as possibly outdated. Never invent news.")


def news_topic(task: str) -> str:
    """ニュースの依頼の話題(「AIに関する最新ニュースを要約して」→「AI」)。話題の指定がなければ空。
    複数の話題(「AIや半導体のニュース」)は「AI OR 半導体」にする。"""
    if _KANA.search(task) or _HAN.search(task):
        found = _TOPIC_JA.search(task)
        if not found:
            return ""
        parts = [_TOPIC_LEAD.sub("", p.strip(" ・")).strip(" ・") for p in re.split(r"[やと、]", found.group(1))]
        parts = [p for p in parts if p and not _TOPIC_GENERIC.fullmatch(p)]
        return " OR ".join(dict.fromkeys(parts))
    found = _TOPIC_EN.search(task)
    if found:
        words = found.group(1).split()
    else:
        found = _TOPIC_EN_BEFORE.search(task)
        words = []
        for word in reversed(found.group(1).split() if found else []):  # 「give me the latest AI news」→ AI
            if word.lower() in _EN_FILLER:
                break
            words.insert(0, word)
    while words and words[-1].lower() in _EN_FILLER:
        words.pop()
    return " ".join(words)


def news_context(source: str, items: list[dict], now: float | None = None, topic: str = "") -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))

    def line(item: dict) -> str:
        when = web.news_time(item)
        day = time.localtime(when) if when else None
        extra = "、".join(x for x in (item.get("outlet", ""), f"{day.tm_mon}/{day.tm_mday}" if day else "") if x)
        return f"- {item['title']}" + (f"({extra})" if extra else "") + (f": {item['summary']}" if item.get("summary") else "")

    about = f"「{topic}」に関する" if topic else ""
    return (f"\n\n{about}最新のニュース({source} の公開の見出し、{stamp} に取得)。ニュースについての依頼には、これだけをもとに答える。"
            f"ここにないことを付け足したり推測したりしない:\n" + "\n".join(line(i) for i in items))


def time_context(task: str, now: float | None = None, memories: list[str] | None = None) -> str:
    """日付や時刻が関わる依頼のための「今日の日時」の一文(関わらない依頼なら空)。
    日本語の依頼には、依頼文と覚えていることに出てくる日付までの日数などを計算して添える(小さいAIは日付の引き算を誤る)。"""
    if not time_sensitive(task):
        return ""
    t = time.localtime(now)
    if _KANA.search(task) or _HAN.search(task):
        facts = date_facts(task, memories or [], datetime.date(t.tm_year, t.tm_mon, t.tm_mday))
        return (f"\n\n今日は {t.tm_year}年{t.tm_mon}月{t.tm_mday}日({_WEEKDAYS_JA[t.tm_wday]}曜日)、"
                f"今の時刻は {t.tm_hour}時{t.tm_min:02d}分です。日付や曜日、日数の計算にはこれを使ってください。"
                + (f"\n計算済みの値(正確なので、日数や曜日はこれをそのまま使う): {' / '.join(facts)}" if facts else ""))
    return f"\n\nToday is {time.strftime('%A, %B %d, %Y', t)} and the current time is {time.strftime('%H:%M', t)}. Use this for dates and day counts."


_NAMED_DAYS = {"大晦日": (12, 31), "大みそか": (12, 31), "年末": (12, 31), "元日": (1, 1), "元旦": (1, 1), "正月": (1, 1),
               "クリスマスイブ": (12, 24), "クリスマス": (12, 25), "バレンタイン": (2, 14), "ホワイトデー": (3, 14),
               "ハロウィン": (10, 31), "七夕": (7, 7), "こどもの日": (5, 5)}
_DATE_JA = re.compile(r"(?:(?P<y>\d{4})\s*年\s*)?(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")
_RELATIVE_JA = re.compile(r"(?P<n>\d+)\s*(?P<unit>日|週間|か月|ヶ月|カ月|年)\s*(?P<dir>後|前)")


def _ja_date(d: datetime.date, today: datetime.date) -> str:
    year = "" if d.year == today.year else f"{d.year}年"
    return f"{year}{d.month}月{d.day}日({_WEEKDAYS_JA[d.weekday()]})"


def date_facts(task: str, memories: list[str], today: datetime.date) -> list[str]:
    """依頼に出てくる日付(「5月3日」「大晦日」、覚えていることの「誕生日は5月3日」など)までの日数と曜日、
    「100日後」のような日付を、計算して文にする。"""
    return [fact for fact, _ in _date_items(task, memories, today)]


_SIMPLE_DATE_Q = re.compile(r"何曜日|あと何日|何日後|何日前|週間後|週間前|か月後|ヶ月後|カ月後|か月前|ヶ月前|カ月前|年後|年前|何日経|何日たっ|"
                            r"何月何日|日付は|何日[?？]?$|いつ[?？]?$")
_CONDITIONAL = re.compile(r"営業日|平日|祝日|休日|土日|除い|除く|以外|時間|何時|分後|秒")
_NEAR_DAYS = {"今日": 0, "明日": 1, "明後日": 2, "昨日": -1, "一昨日": -2}


def date_answer(task: str, memories: list[str], today: datetime.date) -> str | None:
    """「今年の大晦日まであと何日?」「5月3日は何曜日?」「100日後は何日?」のような短い日付の質問に、
    計算した値でそのまま答える(小さいAIは計算済みの値を書き写すときにも誤ることがある)。
    条件のつく質問(営業日・祝日など)や、答えが1つに決まらない質問は None(AIに任せる)。"""
    text = task.strip()
    if len(text) > 40 or "\n" in text or not _SIMPLE_DATE_Q.search(text) or _CONDITIONAL.search(text):
        return None
    near = re.match(r"^(今日|明日|明後日|昨日|一昨日)(?:は|って)?(?:何曜日|何日|何月何日|いつ)", text)
    if near and not _DATE_JA.search(text):
        day = today + datetime.timedelta(days=_NEAR_DAYS[near.group(1)])
        return f"{near.group(1)}は {day.year}年{day.month}月{day.day}日({_WEEKDAYS_JA[day.weekday()]}曜日)です。"
    items = _date_items(text, memories, today)
    if len(items) != 1:
        return None
    fact, (kind, target) = items[0]
    if kind == "relative":
        return fact + "です。"
    if "何曜日" in text:
        return f"{_ja_date(target, today).replace('(', 'は').rstrip(')')}曜日です。"
    return fact + "です。"


def _date_items(task: str, memories: list[str], today: datetime.date) -> list[tuple[str, tuple[str, datetime.date]]]:
    """[(文, (種類, 日付))]。種類は until(これから)/ today / since(過ぎた)/ relative(〜日後・前)。"""
    found: list[tuple[str, int | None, int, int]] = []  # (呼び名, 年, 月, 日)
    for m in _DATE_JA.finditer(task):
        found.append(("", int(m["y"]) if m["y"] else None, int(m["m"]), int(m["d"])))
    for name, (month, day) in _NAMED_DAYS.items():
        if name in task and not (name == "クリスマス" and "クリスマスイブ" in task):
            found.append((name, None, month, day))
    for memory in memories:  # 依頼の言葉が出てくる「覚えていること」の日付(「誕生日まであと何日?」と「誕生日は5月3日」)
        words = re.findall(r"[一-鿿ァ-ヿ]{2,}", _DATE_JA.sub(" ", memory))
        if any(w in task for w in words):
            for m in _DATE_JA.finditer(memory):
                found.append((next(w for w in words if w in task), int(m["y"]) if m["y"] else None, int(m["m"]), int(m["d"])))
    facts: list[tuple[str, tuple[str, datetime.date]]] = []
    for name, year, month, day in found:
        try:
            target = datetime.date(year or today.year, month, day)
            if year is None and target < today:
                target = datetime.date(today.year + 1, month, day)
        except ValueError:
            continue
        label = f"{name}({_ja_date(target, today)})" if name else _ja_date(target, today)
        days = (target - today).days
        kind = "until" if days > 0 else "today" if days == 0 else "since"
        fact = (f"{label}まで あと{days}日" if days > 0 else f"{label}は今日" if days == 0 else f"{label}から今日まで {-days}日")
        if all(fact != f for f, _ in facts):
            facts.append((fact, (kind, target)))
    for m in _RELATIVE_JA.finditer(task):
        n, unit, sign = int(m["n"]), m["unit"], (1 if m["dir"] == "後" else -1)
        if unit in ("日", "週間"):
            target = today + datetime.timedelta(days=sign * n * (7 if unit == "週間" else 1))
        else:
            months = sign * n * (12 if unit == "年" else 1)
            total = today.year * 12 + today.month - 1 + months
            y, mo = divmod(total, 12)
            mo += 1
            last = (datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1)).day
            target = datetime.date(y, mo, min(today.day, last))
        facts.append((f"{m.group(0)}は {_ja_date(target, today)}", ("relative", target)))
    return facts[:6]


# ---- 利用者に合わせる(AIへの指示と、覚えていること) -------------------------------------
# 「覚えて」「忘れて」は、AIに判断させずに決まった言い方で受け付ける(小さいAIは記憶の道具をうまく使えないため)。
_REMEMBER = (
    re.compile(r"^(?:覚えて(?:おいて)?|記憶して(?:おいて)?|メモして(?:おいて)?)(?:ください)?\s*[:：、,]\s*(?P<fact>.+)$", re.S),
    re.compile(r"^(?P<fact>.+?)(?:だ|です)?(?:ということ|こと)?(?:を|と|って)(?:覚えて|記憶して)(?:おいて)?(?:ください|ね|ほしい)?[。.!！]*$", re.S),
    re.compile(r"^remember(?: that)?[:,]?\s+(?P<fact>.+)$", re.I | re.S),
)
_FORGET = (
    re.compile(r"^(?:忘れて|削除して)(?:ください)?\s*[:：、,]\s*(?P<fact>.+)$", re.S),
    re.compile(r"^(?P<fact>.+?)(?:のこと)?(?:を|は)忘れて(?:ください|ね)?[。.!！]*$", re.S),
    re.compile(r"^forget(?: that)?[:,]?\s+(?P<fact>.+)$", re.I | re.S),
)
_RECALL = re.compile(r"^(?:何を覚えて(?:い)?る(?:の)?|覚えていること(?:を(?:教えて|見せて))?|メモリ(?:を見せて|の一覧)?|what do you remember)"
                     r"(?:ください)?[。.??]*$", re.I)
_DEMONSTRATIVE = re.compile(r"^(?:それ|これ|あれ|この|その|あの|上の|上記|今の|さっき|先ほど)")


def memory_command(text: str) -> tuple[str, str] | None:
    """「覚えて: 〜」「〜と覚えておいて」「〜を忘れて」「何を覚えてる?」→ (種類, 内容)。当てはまらなければ None。
    「これを覚えて」のように、会話の流れがないと意味の分からないものは、普通の依頼として扱う。"""
    text = text.strip()
    if len(text) > 400 or "\n\n" in text:
        return None
    if _RECALL.match(text):
        return "recall", ""
    for kind, patterns in (("remember", _REMEMBER), ("forget", _FORGET)):
        for pattern in patterns:
            found = pattern.match(text)
            if found:
                fact = re.sub(r"\s+", " ", found.group("fact")).strip(" 「」『』\"'。.、,")
                if not fact or _DEMONSTRATIVE.match(fact):
                    return None
                return kind, fact[:MAX_MEMORY_CHARS]
    return None


_FIRST_PERSON = re.compile(r"私|僕|俺|わたし|あたし|うち(?:の|は)|自分|我が家|うちの|妻|夫|息子|娘|子ども|子供|家族|\b(I|I'm|I am|my|me)\b")
_MEMORY_GATE = ("この発言には、今後の会話でも役立つ、利用者自身についての事実(名前・好み・習慣・仕事・家族・住んでいる所・"
                "予定など)が含まれていますか?")
_MEMORY_REWRITE = ("Extract the lasting personal fact about the user from the message, as one short declarative sentence in the user's "
                   "language, written from the user's point of view (in Japanese, start with 私は and end with a plain form like 〜している。). "
                   "Do not reply to the message. Output only the sentence. If there is no such fact, output NONE.")
_GATE_SHOTS = [("私は猫を2匹飼っています。名前の案を出して", 0), ("東京タワーの高さは?", 1),
               ("僕は看護師で夜勤があります。眠りの質を上げるには?", 0), ("私にもわかるように量子力学を説明して", 1)]
_MEMORY_SHOTS = [("私は猫を2匹飼っています。名前の案を出して", "私は猫を2匹飼っている。"),
                 ("東京タワーの高さは?", "NONE"),
                 ("妻が来週誕生日なので、プレゼントの案を考えて", "私の妻の誕生日は来週。")]


def personal_prompt(instructions: str, memories: list[str]) -> str:
    """指示文に足す「利用者に合わせる」部分(どちらも空なら空)。スマホの中のAIも同じ形で使う。"""
    parts = []
    if instructions.strip():
        parts.append("利用者からの指示(いつも守ること):\n" + instructions.strip())
    if memories:
        parts.append("利用者について覚えていること(文中の「私」は利用者。関係があるときだけ使い、毎回は触れない):\n"
                     + "\n".join(f"- {m}" for m in memories))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def learn_threshold(draft: dict[str, dict], fallback: dict[str, dict], min_shared: int = 12) -> float | None:
    """計測の各問題の結果から、答え直す境目を決める: 正解数が最も多くなり、その中で最も速い値。

    draft/fallback は {問題ID: {"ok", "confidence", "latency_s"}}。共通の問題が少なければ None。
    """
    shared = [(d, fallback[k]) for k, d in draft.items() if k in fallback and d.get("confidence") is not None]
    if len(shared) < min_shared:
        return None
    values = sorted({d["confidence"] for d, _ in shared})
    best_t, best_key = None, None
    for t in [values[0] - 1e-6] + [v + 1e-9 for v in values]:  # 確からしさが t 未満なら答え直す
        correct, cost = 0, 0.0
        for d, f in shared:
            if d["confidence"] < t:
                correct += bool(f["ok"])
                cost += d["latency_s"] + f["latency_s"]  # 下書きの時間も(多めに)数える
            else:
                correct += bool(d["ok"])
                cost += d["latency_s"]
        key = (correct, -cost)
        if best_key is None or key > best_key:
            best_t, best_key = t, key
    return best_t


class _Reject(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "unsure"(自信がない) / "lang"(言語の取り違え)


class _Gate:
    """回答の出だしを溜めて確かめ、問題がなければ溜めた分を流して素通しにする。

    問題があれば _Reject を投げる。バックエンドはそれで生成を打ち切る(無駄な続きを作らない)。
    溜めている間は利用者に何も見せないので、答え直しても画面が書き換わらない。
    """

    def __init__(self, on_token: OnToken | None, threshold: float | None, check_language: bool, role_name: str):
        self.on_token = on_token
        self.threshold = threshold
        self.check_language = check_language
        self.role = role_name
        self.logprobs: list[float] = []
        self.parts: list[str] = []
        self.open = False
        self.finished = False

    @property
    def confidence(self) -> float | None:
        head = self.logprobs[:_WINDOW]
        return sum(head) / len(head) if head else None

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def __call__(self, piece: str) -> None:
        if self.open:
            if self.on_token:
                self.on_token(piece)
            return
        self.parts.append(piece)
        # 確率が返らないバックエンドでも、長い回答をいつまでも溜めないようにする
        if len(self.logprobs) >= _WINDOW or (not self.logprobs and sum(map(len, self.parts)) >= 200):
            self.check()

    def finish(self) -> None:
        self.finished = True
        self.check()

    def check(self) -> None:
        if self.open:
            return
        text = self.text
        if self.check_language and wrong_language(text, self.role):
            raise _Reject("lang")
        conf = self.confidence
        if self.threshold is not None and conf is not None and len(self.logprobs) >= _MIN_TOKENS and conf < self.threshold:
            raise _Reject("unsure")
        self.release()

    def release(self) -> None:
        """溜めた分を流し、以後は素通しにする。"""
        self.open = True
        if self.on_token and self.parts:
            self.on_token(self.text)


@dataclass
class Result:
    id: int
    text: str
    role: str
    model: str
    source: str  # llm / cache / swarm / chat
    route_source: str
    tokens: int  # 管理AIのオーバーヘッドを含む総トークン
    latency_s: float
    examples_used: int = 0
    subresults: list["Result"] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)  # 使った道具の記録(名前・対象・成否)
    confidence: float | None = None  # 最初に答えたモデルの確からしさ(出だしの平均の対数確率)
    escalated: dict | None = None    # 答え直したとき {"from", "to", "reason", "confidence"}
    sources: list[dict] = field(default_factory=list)  # 裏付けに使った資料 [{"title", "url"}]
    changes: list[dict] = field(default_factory=list)  # エージェントが変えたファイルと差分 [{"path", "status", "added", "removed", "diff"}]


def _emit(on_stage: OnStage | None, stage: str, **data) -> None:
    if on_stage:
        on_stage(stage, data)


@dataclass
class _Usage:
    tokens: int = 0

    def add(self, gen: Generation) -> None:
        self.tokens += gen.tokens


class Celestial:
    def __init__(self, config: Config | None = None, *, backends: dict[str, Backend] | None = None, store: Store | None = None):
        self.cfg = config or load_config()
        self.backends = backends or {name: self._create_backend(name, opts) for name, opts in self.cfg.backends.items()}
        self.store = store or Store(self.cfg.home / "celestial.db")
        self._models: list[ModelInfo] | None = None
        self._thresholds: dict[tuple[str, str, int], float] = {}
        saved = self.store.settings()
        for key, value in saved.items():  # 画面から変えた設定は celestial.toml より優先
            if key in TUNABLE:
                setattr(self.cfg, key, value)
        # AIへの指示・覚えていることを最後に変えたときの、記録の最後の番号(それ以前のキャッシュは古い指示で作った回答なので使わない)
        self._personal_epoch = int(saved.get("personal_epoch", 0) or 0)
        self._personal_lock = threading.Lock()

        # 履歴から学習状態を復元する(どちらも一瞬で終わる)
        self.router = LearnedRouter()
        for row in self.store.routing_examples():
            self.router.add(row["task"], row["role"])
        for row in self.store.study_rows():  # 自習で作った練習問題は、担当が最初から分かっている
            self.router.add(row["task"], row["role"])
        self.embedder = Embedder(self.backends, self.store, self.cfg.embed_model)
        self.memory = Memory(embedder=self.embedder, semantic=lambda: self.cfg.semantic_search,
                             min_sim_semantic=self.cfg.fewshot_min_sim_semantic)
        for row in self.store.good_examples():
            self.memory.add(row["id"], row["role"], row["task"], row["answer"])

    def _create_backend(self, name: str, opts: dict) -> Backend:
        if opts.get("type", name) == "native":
            opts = {"models_dir": str(self.cfg.home / "models"), **opts}
        return create_backend(name, **opts)

    # ---- モデル -------------------------------------------------------------

    def models(self, refresh: bool = False) -> list[ModelInfo]:
        if self._models is None or refresh:
            if self.cfg.models:
                found = [ModelInfo(m["name"], m.get("backend", next(iter(self.backends))), int(m.get("size_bytes", 0)), m.get("tier"))
                         for m in self.cfg.models]
            else:
                found, errors = [], []
                for backend in self.backends.values():
                    try:
                        found += backend.list_models()
                    except BackendError as e:
                        errors.append(str(e))
                if not found:
                    raise BackendError("; ".join(errors) or "利用可能なモデルがありません。`ollama pull <model>` で導入してください。")
            # Ollama に書き出し済みの自作AIは Ollama 側だけを使う(同じモデルを二重に数えない)。
            # 自作AIは小さくても管理AIには選ばない。
            exported = {m["name"]: m["ollama"] for m in read_models(self.cfg.home / "models") if m.get("ollama")}
            names = {m.name for m in found}
            found = [m for m in found if not (m.name in exported and exported[m.name] in names)]
            for m in found:
                if m.name in exported.values() or m.name.startswith("celestial-"):
                    m.can_manage = False
            self._models = found
        return self._models

    def _loaded_models(self) -> dict[str, set[str]]:
        """{バックエンド名: いまメモリに読み込まれているモデル}(分かるバックエンドだけ)。"""
        found = {}
        for name, backend in self.backends.items():
            names = backend.loaded_models()
            if names:
                found[name] = names
        return found

    def manager_model(self) -> ModelInfo:
        models = self.models()
        if self.cfg.manager_model != "auto":
            for m in models:
                if m.name == self.cfg.manager_model:
                    return m
        return min([m for m in models if m.can_manage] or models, key=lambda m: m.size_bytes)

    def _generate(self, model: ModelInfo, messages: list[Message], **kw) -> Generation:
        return self.backends[model.backend].generate(model.name, messages, **kw)

    def make_title(self, task: str) -> str | None:
        """会話の題名を短く付ける(会話の一覧・保存する文書の題名に使う)。
        読み込み済みのAIのうち 6GB 以下で最も大きいものを使う(入れ替えを起こさず、1.5B より 4B の方が題名がよい)。"""
        models = [m for m in self.models() if m.can_manage]
        loaded = self._loaded_models()
        ready = [m for m in models if m.name in loaded.get(m.backend, set()) and m.size_bytes <= 6e9]
        model = max(ready, key=lambda m: m.size_bytes) if ready else self.manager_model()
        gen = self._generate(model, title_messages(task), temperature=0.0, max_tokens=40)
        return clean_title(gen.text, task)

    def _ask_json(self, system: str, user: str, schema: dict, usage: _Usage, max_tokens: int) -> dict:
        gen = self._generate(
            self.manager_model(),
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0, max_tokens=max_tokens, json_schema=schema,
        )
        usage.add(gen)
        data = json.loads(gen.text)  # 失敗時は ValueError(JSONDecodeError)
        if not isinstance(data, dict):
            raise ValueError("JSONオブジェクトではありません")
        return data

    def _pick_model(self, role_name: str, managers_only: bool = False) -> ModelInfo:
        """UCBバンディット: 実績(品質−時間)の良いモデルを選びつつ、未知のモデルも少し試す。

        精度の計測結果があれば、それを「事前の実績」として数回分の重みで混ぜる。
        これにより、評価が付いていなくても「速いだけで不正確なモデル」に流れない。
        """
        models = self.models()
        if managers_only:  # JSON の構造を守れるモデルに限る(道具を使う回答など)
            models = [m for m in models if m.can_manage] or models
        stats = self.store.model_stats(role_name)
        priors = self.bench_priors()
        # この担当を計測済みのモデルがあれば、その中から選ぶ。未計測のモデルを「未知だから試す」と、
        # 古い自作AI(読み込みに数十秒かかり、確からしさも返さない)などに流れて遅く不正確になる。
        # 新しいモデルは「精度の計測」をすると候補に入る
        models = [m for m in models if role_name in priors.get(m.name, {})] or models
        total = sum(n for n, _ in stats.values())
        want = TIERS.index(ROLES[role_name].tier)
        loaded = self._loaded_models()

        def swap_cost(m: ModelInfo) -> float:
            """読み込まれていないモデルを使うと、読み込みの数秒に加え、ほかのモデルを追い出して次の依頼も遅くなる。
            はっきり正確なときだけ選ぶよう、大きさに応じて差し引く(何も読み込まれていなければ差はない)。"""
            names = loaded.get(m.backend)
            if not names or m.name in names:
                return 0.0
            return 0.04 + 0.03 * m.size_bytes / 1e9

        def score(m: ModelInfo) -> float:
            n, mean = stats.get(m.name, (0, 0.0))
            prior = priors.get(m.name, {}).get(role_name)  # (正解率, 平均秒, 課題数)
            if prior is not None:
                accuracy, latency, count = prior
                weight = min(count, 5)
                prior_reward = accuracy - 0.3 * min(1.0, latency / 60.0)
                blended = (n * mean + weight * prior_reward) / (n + weight)
                return blended + self.cfg.explore * math.sqrt(math.log(total + weight + 1) / (n + weight)) - swap_cost(m)
            if n == 0:
                return 0.8 - 0.15 * abs(TIERS.index(m.tier) - want) - swap_cost(m)
            return mean + self.cfg.explore * math.sqrt(math.log(total + 1) / n) - swap_cost(m)

        return max(models, key=score)

    # ---- 実行 ---------------------------------------------------------------

    def run(self, task: str, *, role: str | None = None, model: str | None = None, use_cache: bool = True,
            verify: bool = False, swarm: bool | None = None, on_token: OnToken | None = None,
            history: list[tuple[str, str]] | None = None, learn: bool = True,
            on_stage: OnStage | None = None, tools: bool = False, agent: bool = False,
            approve: Callable[[dict], bool] | None = None, images: list[bytes] | None = None,
            folder: str | None = None) -> Result:
        """タスクを1件処理する。

        role/model を指定すると振り分け・モデル選択を固定できる。swarm は None=自動, True=必ず分解, False=分解しない。
        history は直近の (ユーザー発話, 回答) の列(対話モード用)。
        learn=False では履歴に一切書き込まず、キャッシュも使わない(計測用。結果の id は 0)。
        agent=True(tools=True も同じ)では AIエージェントとして動く: 設定で許可された道具(計算・ファイル・
        コマンド・PC操作・ネット・ワーカーへの依頼)を使いながら、自分で段取りして目標まで進める。
        approve は影響のある操作の前に呼ばれる確認の関数(True で許可)。images は添付画像。
        """
        task = task.strip()
        if not task:
            raise ValueError("タスクが空です")
        if role is not None and role not in ROLES:
            raise ValueError(f"未知の役割: {role}(選択肢: {', '.join(ROLES)})")
        t0 = time.perf_counter()

        forced_model = None
        if model is not None:
            forced_model = next((m for m in self.models() if m.name == model), None)
            if forced_model is None:
                raise ValueError(f"未知のモデル: {model}")

        if learn and not images:  # 短い依頼は会話の続きとして履歴つきで来るが、ここでは履歴に関係なく答えられる
            reply = self._memory_reply(task)
            if reply is not None:  # 「覚えて」「忘れて」「何を覚えてる?」は、AIを呼ばずにすぐ答える
                _emit(on_stage, "memory")
                if on_token:
                    on_token(reply)
                return Result(0, reply, "general", "メモリ", "memory", "memory", 0, time.perf_counter() - t0)
            if role is None and forced_model is None and not agent and time_sensitive(task):
                now = time.localtime()
                reply = date_answer(task, list(self.cfg.memories), datetime.date(now.tm_year, now.tm_mon, now.tm_mday))
                if reply is not None:  # 短い日付の質問は、計算した値でそのまま答える(AIは日付の計算を誤る)
                    _emit(on_stage, "calc")
                    if on_token:
                        on_token(reply)
                    return Result(0, reply, "reason", "計算", "calc", "calc", 0, time.perf_counter() - t0)

        tools = tools or agent
        plain = role is None and forced_model is None and not history and swarm is not True and not tools and not images
        if plain and learn and use_cache and self.cfg.cache and not time_sensitive(task):
            hit = self.store.cache_lookup(task, after_id=self._personal_epoch)
            if hit is not None and hit["role"] not in self.cfg.no_cache_roles:
                latency = time.perf_counter() - t0
                new_id = self.store.log(task=task, role=hit["role"], model=hit["model"], answer=hit["answer"],
                                        source="cache", route_source="cache", latency_s=latency, origin_id=hit["id"])
                _emit(on_stage, "cache")
                if on_token:
                    on_token(hit["answer"])
                return Result(new_id, hit["answer"], hit["role"], hit["model"], "cache", "cache", 0, latency)

        usage = _Usage()
        if swarm is True and not tools and not images:
            return self._run_swarm(task, "forced", usage, t0, verify, on_token, learn, on_stage)
        if role is not None:
            role_name, route_source = role, "forced"
        else:
            allow_complex = (swarm is None and not tools and not images and self.cfg.swarm_auto and not history
                             and len(task) >= self.cfg.swarm_min_chars)
            role_name, route_source = self._route(routing_text(task), usage, allow_complex)
        _emit(on_stage, "route", role=role_name, source=route_source)
        if role_name == COMPLEX:
            return self._run_swarm(task, route_source, usage, t0, verify, on_token, learn, on_stage)
        if tools:
            # 「〜アプリを作って」は、計画してファイルごとに書き、確かめて直す手順で作る(1回の応答で全部を書かせない)
            if (not images and forced_model is None and project.looks_like_project_request(task) and self.cfg.tools_write
                    and self.work_folder(folder) is not None):
                return self._run_project(task, usage=usage, t0=t0, on_token=on_token, on_stage=on_stage, learn=learn,
                                         approve=approve, folder=folder)
            return self._run_agent(task, role_name, route_source=route_source, usage=usage, t0=t0, model=forced_model,
                                   on_token=on_token, on_stage=on_stage, history=history, learn=learn,
                                   approve=approve, images=images, folder=folder)
        return self._solve(task, role_name, route_source=route_source, usage=usage, t0=t0, model=forced_model,
                           verify=verify, on_token=on_token, history=history, learn=learn, on_stage=on_stage,
                           images=images)

    def _route(self, task: str, usage: _Usage, allow_complex: bool) -> tuple[str, str]:
        if self.cfg.learned_routing:
            role, conf = self.router.predict(task)
            if role and conf >= self.cfg.route_confidence and (role != COMPLEX or allow_complex) and plausible_role(role, task):
                return role, "learned"
        options = {name: r.description for name, r in ROLES.items()}
        if allow_complex:
            options[COMPLEX] = COMPLEX_DESCRIPTION
        system = ("You are a task router. Pick the single best category for the user's task.\n"
                  + "\n".join(f"- {name}: {desc}" for name, desc in options.items()))
        schema = {"type": "object", "properties": {"role": {"type": "string", "enum": list(options)}}, "required": ["role"]}
        try:
            picked = self._ask_json(system, task[:_CLIP], schema, usage, 24).get("role")
            if picked in options and plausible_role(picked, task):
                return picked, "manager"
        except (BackendError, ValueError):
            pass
        return heuristic_role(task), "heuristic"

    def _solve(self, task: str, role_name: str, *, route_source: str, usage: _Usage, t0: float,
               model: ModelInfo | None = None, verify: bool = False, on_token: OnToken | None = None,
               history: list[tuple[str, str]] | None = None, parent_id: int | None = None,
               learn: bool = True, on_stage: OnStage | None = None, images: list[bytes] | None = None) -> Result:
        role = ROLES[role_name]
        examples = self.memory.search(role_name, task, self.cfg.fewshot_k, self.cfg.fewshot_min_sim)
        japanese = wants_japanese(task, role_name)
        # 計測(learn=False)では利用者の指示を混ぜない(モデルの素の力を比べるため)
        system = role.system + (_JA_HINT if japanese else "") + time_context(task, memories=list(self.cfg.memories) if learn else None) +(self.personal_prompt() if learn else "")
        news_sources: list[dict] = []
        if learn and _LIVE.search(task):
            system, news_sources = self._live_context(task, system, on_stage)
        messages: list[Message] = [{"role": "system", "content": system}]
        for ex in examples:  # 過去の成功例を手本として渡す
            messages += [{"role": "user", "content": ex.task[:_CLIP]}, {"role": "assistant", "content": ex.answer[:_CLIP]}]
        for user_text, answer in history or []:
            messages += [{"role": "user", "content": user_text[:_CLIP]}, {"role": "assistant", "content": answer[:_CLIP]}]
        messages.append({"role": "user", "content": task})

        chosen = model or self._pick_model(role_name)
        if images and not chosen.vision:  # 画像つきの依頼は、画像を見られるモデルに任せる
            vision = self._vision_model()
            if vision is None or model is not None:
                raise BackendError("画像を扱えるAIがありません(例: ollama pull gemma3:4b)" if vision is None
                                   else f"{chosen.name} は画像を扱えません")
            chosen = vision
        _emit(on_stage, "worker", role=role_name, model=chosen.name, examples=len(examples))
        gen, chosen, confidence, escalated, sources = self._answer(
            task, role_name, messages, chosen, forced=model is not None, japanese=japanese, history=bool(history),
            on_token=on_token, on_stage=on_stage, images=images, learn=learn, route_source=route_source, parent_id=parent_id)
        sources = news_sources + list(sources)
        verified = 0
        if verify:
            _emit(on_stage, "verify")
            ok, issue = self._verify(task, gen.text, usage)
            if ok:
                verified = 1
            elif ok is False:
                # 不合格を記録してモデル選択に反映し、より大きいモデルで指摘つきの再回答をさせる
                if learn:
                    self.store.log(task=task, role=role_name, model=chosen.name, answer=gen.text, source="rejected",
                                   route_source=route_source, prompt_tokens=gen.prompt_tokens,
                                   completion_tokens=gen.completion_tokens, gen_latency_s=gen.latency_s,
                                   verified=-1, parent_id=parent_id)
                usage.add(gen)
                if model is None:  # 計測で最も正確なモデルに答え直させる(計測がなければ最も大きいモデル)
                    chosen = self._most_accurate(role_name) or chosen
                messages += [{"role": "assistant", "content": gen.text},
                             {"role": "user", "content": f"A reviewer found a problem with your answer: {issue}\n"
                                                         "Give a corrected, complete answer to the original request."}]
                _emit(on_stage, "retry", model=chosen.name, issue=issue)
                if on_token:
                    on_token("\n\n――― 検証で問題が見つかったため再回答 ―――\n\n")
                gen = self._generate(chosen, messages, temperature=role.temperature, on_token=on_token)

        source = "chat" if history or images else "llm"
        latency = time.perf_counter() - t0
        new_id = 0
        if learn:
            new_id = self.store.log(task=task, role=role_name, model=chosen.name, answer=gen.text, source=source,
                                    route_source=route_source, prompt_tokens=gen.prompt_tokens,
                                    completion_tokens=gen.completion_tokens, overhead_tokens=usage.tokens,
                                    latency_s=latency, gen_latency_s=gen.latency_s, verified=verified, parent_id=parent_id)
        if learn and source == "llm":
            if verified == 1:
                self.memory.add(new_id, role_name, task, gen.text)
            if route_source in _TEACHER_ROUTES:
                self.router.add(routing_text(task), role_name)
        return Result(new_id, gen.text, role_name, chosen.name, source, route_source,
                      gen.tokens + usage.tokens, latency, len(examples),
                      confidence=confidence, escalated=escalated, sources=sources)

    def _live_context(self, task: str, system: str, on_stage: OnStage | None) -> tuple[str, list[dict]]:
        """最新の情報が要る依頼: ネットが使えるならニュースの依頼に最新の見出しを添え(出典つき)、
        使えないなら「最新の情報は取得できない」と最初に伝えさせる。"""
        ja = bool(_KANA.search(task) or _HAN.search(task))
        if not self.cfg.tools_net:
            return system + (_OFFLINE_NOTE if ja else _OFFLINE_NOTE_EN), []
        if not _NEWS.search(task):
            return system, []
        topic = news_topic(task)
        _emit(on_stage, "news", topic=topic)
        try:
            source, items = web.news("ja" if ja else "en", limit=12 if topic else 10, topic=topic)
        except web.WebError:
            return system + (_OFFLINE_NOTE if ja else _OFFLINE_NOTE_EN), []
        if not items:
            if not topic:
                return system, []
            return system + (f"\n\n「{topic}」に関する最近のニュースは、ニュースの検索で見つかりませんでした。そのことを伝え、作り話のニュースを書かない。"
                             if ja else f"\n\nNo recent news about \"{topic}\" was found. Say so, and never invent news."), []
        sources = [{"title": i["title"] + (f"({i['outlet']})" if i.get("outlet") else ""), "url": i["url"]}
                   for i in items[:6 if topic else 5] if i["url"]]
        return system + news_context(source, items, topic=topic), sources

    def _answer(self, task: str, role_name: str, messages: list[Message], chosen: ModelInfo, *, forced: bool, japanese: bool,
                history: bool, on_token: OnToken | None, on_stage: OnStage | None, images: list[bytes] | None,
                learn: bool, route_source: str, parent_id: int | None
                ) -> tuple[Generation, ModelInfo, float | None, dict | None, list[dict]]:
        """回答を作る。出だしの確からしさと言語を確かめ、必要なら答え直す(より正確なAI・資料での裏付け)。

        戻り値は (生成結果, 最終的に答えたモデル, 最初のモデルの確からしさ, 答え直しの記録, 裏付けの資料)。
        """
        role = ROLES[role_name]
        fallback, threshold = (None, None)
        if not forced and not images and self.cfg.cascade:
            fallback, threshold = self._cascade_plan(role_name, chosen)
        # 今日の日付で答えが変わる質問は、Wikipedia では確かめられない(日付は指示文で渡している)
        factual = (not forced and not images and not history and self.cfg.tools_net and role_name == "general"
                   and ground.looks_factual(task) and not time_sensitive(task))

        if fallback is None and not factual:  # 確かめる必要がない(最も正確なモデルが答える)ので、そのまま流す
            logprobs: list[float] = []
            gen = self._generate(chosen, messages, temperature=role.temperature, on_token=on_token, images=images or None,
                                 logprobs=logprobs)
            head = logprobs[:_WINDOW]
            confidence = sum(head) / len(head) if head else None
            if japanese and wrong_language(gen.text[:400], role_name):  # まれに起きる言語の取り違えは、指示を強めて答え直す
                self._log_rejected(task, role_name, chosen, gen.text, route_source, parent_id, learn)
                _emit(on_stage, "retry", model=chosen.name, issue="日本語で答え直し")
                if on_token:
                    on_token("\n\n――― 日本語で答え直します ―――\n\n")
                redo = messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + "\n\n(日本語で答えてください)"}]
                gen = self._generate(chosen, redo, temperature=role.temperature, on_token=on_token, images=images or None)
                return gen, chosen, confidence, {"from": chosen.name, "to": chosen.name, "reason": "lang", "confidence": confidence}, []
            return gen, chosen, confidence, None, []

        gate = _Gate(on_token, threshold if fallback is not None else FACT_THRESHOLD, japanese, role_name)
        gen: Generation | None = None
        try:
            gen = self._generate(chosen, messages, temperature=role.temperature, on_token=gate, images=images or None,
                                 logprobs=gate.logprobs)
            gate.finish()
            return gen, chosen, gate.confidence, None, []
        except _Reject as e:
            reason = e.reason
        confidence = gate.confidence
        target = fallback or chosen
        if reason == "lang":
            self._log_rejected(task, role_name, chosen, gate.text, route_source, parent_id, learn)
        materials = []
        if reason == "unsure" and factual:
            _emit(on_stage, "ground", model=target.name)
            materials = ground.lookup(task)
        escalated = {"from": chosen.name, "to": target.name, "reason": reason, "confidence": confidence}
        if materials:
            # 最も正確なモデル自身が迷ったときは、その答えを添えて「資料と食い違うときだけ直す」形にする
            # (関係のない資料しか見つからなくても、正しかった答えが崩れないように)
            draft = gate.text if target is chosen else ""
            redo = [{"role": "system", "content": messages[0]["content"] + ground.context_prompt(materials, draft)}] + messages[1:]
        elif target is chosen and gen is not None and reason == "unsure":
            gate.release()  # 確かめる資料も答え直す先もない: 書き終えていた回答をそのまま使う
            return gen, chosen, confidence, None, []
        else:
            redo = messages
            if reason == "lang":
                redo = messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + "\n\n(日本語で答えてください)"}]
        if target is not chosen:
            _emit(on_stage, "escalate", model=target.name, previous=chosen.name, reason=reason)
        gen = self._generate(target, redo, temperature=role.temperature, on_token=on_token, images=images or None)
        sources = [{"title": m["title"], "url": m["url"]} for m in materials]
        return gen, target, confidence, escalated, sources

    def _log_rejected(self, task: str, role_name: str, model: ModelInfo, text: str, route_source: str,
                      parent_id: int | None, learn: bool) -> None:
        """言語を取り違えた回答は誤答として記録し、そのモデルをこの役割で選びにくくする。"""
        if learn and text.strip():
            self.store.log(task=task, role=role_name, model=model.name, answer=text, source="rejected",
                           route_source=route_source, verified=-1, parent_id=parent_id)

    def _cascade_plan(self, role_name: str, chosen: ModelInfo) -> tuple[ModelInfo | None, float | None]:
        """chosen より正確だと計測で分かっているモデル(答え直し役)と、答え直す確からしさの境目。

        正確さは (この役割の正解率, 全体の正解率) で比べる。chosen が未計測なら、計測済みの最良のモデルが控えになる。
        """
        scores = self._bench_scores()

        def key(m: ModelInfo) -> tuple[float, float] | None:
            s = scores.get(m.name)
            return None if s is None else (s.get(role_name, s["all"]), s["all"])

        mine = key(chosen)
        best = self._most_accurate(role_name, exclude=chosen.name, measured_only=True)
        if best is None or (mine is not None and key(best) <= mine):
            return None, None
        return best, self.cascade_threshold(chosen.name, best.name)

    def _most_accurate(self, role_name: str, exclude: str | None = None, measured_only: bool = False) -> ModelInfo | None:
        """計測で最も正確なモデル((この役割の正解率, 全体の正解率) で比べ、同じなら小さいほう)。

        計測がなければ最も大きいモデル(measured_only なら None)。
        """
        scores = self._bench_scores()
        candidates = [m for m in self.models() if m.name != exclude]
        measured = [m for m in candidates if m.name in scores]
        if measured:
            def rank(m: ModelInfo) -> tuple:
                s = scores[m.name]
                return (s.get(role_name, s["all"]), s["all"], -m.size_bytes)
            return max(measured, key=rank)
        if measured_only or not candidates:
            return None
        return max(candidates, key=lambda m: m.size_bytes)

    def cascade_threshold(self, draft: str, fallback: str) -> float:
        """計測の各問題の結果から学んだ境目(計測がまだなら既定値)。計測するたびに学び直す。"""
        cache_key = (draft, fallback, self.store.bench_version())
        if cache_key not in self._thresholds:
            learned = learn_threshold(self.store.bench_items(draft), self.store.bench_items(fallback))
            self._thresholds[cache_key] = DEFAULT_CASCADE_THRESHOLD if learned is None else learned
        return self._thresholds[cache_key]

    def _verify(self, task: str, answer: str, usage: _Usage) -> tuple[bool | None, str]:
        """(合否, 指摘)。検証自体に失敗したら (None, "")。"""
        system = ("You are a strict reviewer. Decide whether the answer correctly and completely fulfils the task. "
                  "Set ok=false only for a real error or omission, and describe it briefly in 'issue'.")
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "issue": {"type": "string"}}, "required": ["ok", "issue"]}
        try:
            data = self._ask_json(system, f"Task:\n{task[:_CLIP]}\n\nAnswer:\n{answer[:3000]}", schema, usage, 200)
        except (BackendError, ValueError):
            return None, ""
        if not isinstance(data.get("ok"), bool):
            return None, ""
        return data["ok"], str(data.get("issue", ""))

    def _run_swarm(self, task: str, route_source: str, usage: _Usage, t0: float, verify: bool,
                   on_token: OnToken | None, learn: bool = True, on_stage: OnStage | None = None) -> Result:
        """管理AIがタスクを分解 → ワーカー群が並列に処理 → 統合。"""
        limit = self.cfg.swarm_max_subtasks
        system = (f"You are a planner. Split the user's task into 2 to {limit} independent subtasks that can be done in parallel "
                  "by specialist workers. Each subtask must be self-contained: include all text and details the worker needs.\n"
                  "Worker roles:\n" + "\n".join(f"- {name}: {r.description}" for name, r in ROLES.items()))
        schema = {"type": "object", "required": ["subtasks"], "properties": {"subtasks": {"type": "array", "items": {
            "type": "object", "required": ["role", "task"],
            "properties": {"role": {"type": "string", "enum": list(ROLES)}, "task": {"type": "string"}}}}}}
        subtasks: list[tuple[str, str]] = []
        try:
            plan = self._ask_json(system, task, schema, usage, 800)
            for item in plan.get("subtasks") or []:
                if isinstance(item, dict) and item.get("role") in ROLES and str(item.get("task", "")).strip():
                    subtasks.append((item["role"], str(item["task"]).strip()))
        except (BackendError, ValueError):
            pass
        subtasks = subtasks[:limit]
        if len(subtasks) < 2:  # 分解できなかったら単独のワーカーに任せる
            return self._solve(task, heuristic_role(task), route_source="heuristic", usage=usage, t0=t0,
                               verify=verify, on_token=on_token, learn=learn, on_stage=on_stage)

        _emit(on_stage, "plan", subtasks=[{"role": r, "task": t} for r, t in subtasks])

        def work(indexed: tuple[int, tuple[str, str]]) -> Result:
            index, item = indexed
            result = self._solve(item[1], item[0], route_source="plan", usage=_Usage(), t0=time.perf_counter(),
                                 verify=verify, parent_id=0, learn=learn)
            _emit(on_stage, "sub_done", index=index, model=result.model)
            return result

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.swarm_parallel)) as pool:
            subs = list(pool.map(work, enumerate(subtasks)))

        parts = "\n\n".join(f"### Part {i} ({r.role}): {sub}\n{r.text}" for i, ((_, sub), r) in enumerate(zip(subtasks, subs), 1))
        messages = [
            {"role": "system", "content": "You combine partial results from specialist workers into one complete, coherent final "
                                          "answer to the original task. Do not mention the workers or the splitting. "
                                          "Answer in the same language as the original task."
                                          + time_context(task, memories=list(self.cfg.memories) if learn else None) +(self.personal_prompt() if learn else "")},
            {"role": "user", "content": f"Original task:\n{task}\n\nPartial results:\n\n{parts}"},
        ]
        synth = self._pick_model("general")
        _emit(on_stage, "synth", model=synth.name)
        gen = self._generate(synth, messages, temperature=0.2, on_token=on_token)
        overhead = usage.tokens + sum(r.tokens for r in subs)
        latency = time.perf_counter() - t0
        new_id = 0
        if learn:
            new_id = self.store.log(task=task, role=COMPLEX, model=synth.name, answer=gen.text, source="swarm",
                                    route_source=route_source, prompt_tokens=gen.prompt_tokens,
                                    completion_tokens=gen.completion_tokens, overhead_tokens=overhead,
                                    latency_s=latency, gen_latency_s=gen.latency_s)
            self.store.set_parent([r.id for r in subs], new_id)
            if route_source in _TEACHER_ROUTES:
                self.router.add(routing_text(task), COMPLEX)
        return Result(new_id, gen.text, COMPLEX, synth.name, "swarm", route_source, gen.tokens + overhead, latency, subresults=subs)

    # ---- ツール実行 ---------------------------------------------------------

    def work_folder(self, folder: str | None) -> Path | None:
        """エージェントが作業するフォルダ: 作業フォルダ、またはその中のフォルダ(会話ごとに選べる)。外は選べない。"""
        workspace = Path(self.cfg.workspace) if self.cfg.workspace else None
        if workspace is None or not workspace.is_dir():
            return None
        if not folder or folder.strip() in (".", ""):
            return workspace
        root = workspace.resolve()
        target = (root / folder.strip()).resolve()
        if target != root and root not in target.parents:
            raise ValueError("作業するフォルダは、作業フォルダの中から選んでください")
        if not target.is_dir():
            raise ValueError(f"フォルダが見つかりません: {folder}")
        return target

    def tool_context(self, approve: Callable[[dict], bool] | None = None, folder: str | None = None) -> ToolContext:
        workspace = self.work_folder(folder)
        if workspace is not None and not workspace.is_dir():
            workspace = None
        return ToolContext(
            workspace=workspace, allow_write=self.cfg.tools_write, allow_run=self.cfg.tools_run,
            allow_pc=self.cfg.tools_pc, allow_net=self.cfg.tools_net, backup_dir=self.cfg.home / "backups",
            confirm=self.cfg.tools_confirm, approve=approve,
            describe_image=self._describe_image if self._vision_model() else None,
            delegate=self._delegate, scratch_dir=self.cfg.home / "scratch", changes={})

    def bench_priors(self) -> dict[str, dict[str, tuple[float, float, int]]]:
        """精度の計測結果(今の組み込みの課題集で測ったものを優先)。{モデル名: {役割: (正解率, 平均秒, 課題数)}}"""
        if not hasattr(self, "_suites"):
            from .bench import load_tasks, suite_id  # 起動を軽くするため、使うときに読む

            tasks = load_tasks()
            self._suites = {suite_id(tasks), suite_id([t for t in tasks if "tests" not in t["check"]])}
        return self.store.bench_priors(self._suites)

    def _bench_scores(self) -> dict[str, dict]:
        """{モデル名: {"all": 全体の正解率, 役割: 正解率}}(計測済みのモデルだけ)。計測が増えるまで使い回す。"""
        version = self.store.bench_version()
        cached = getattr(self, "_scores_cache", None)
        if cached is not None and cached[0] == version:
            return cached[1]
        scores = {}
        for name, table in self.bench_priors().items():
            count = sum(c for _, _, c in table.values())
            if count:
                scores[name] = {"all": sum(acc * c for acc, _, c in table.values()) / count, **{r: v[0] for r, v in table.items()}}
        self._scores_cache = (version, scores)
        return scores

    def _vision_model(self) -> ModelInfo | None:
        """画像を見られるモデルのうち、計測で最も正確なもの。"""
        scores = self._bench_scores()
        candidates = [m for m in self.models() if m.vision]
        return max(candidates, key=lambda m: (scores.get(m.name, {}).get("all", -1.0), m.size_bytes), default=None)

    def _describe_image(self, path: Path, prompt: str) -> str:
        model = self._vision_model()
        if model is None:
            raise ToolError("画像を見られるAIがありません")
        try:
            return self._generate(model, [{"role": "user", "content": prompt}], temperature=0.1, max_tokens=700,
                                  images=[Path(path).read_bytes()]).text
        except BackendError as e:
            raise ToolError(f"画像を読み取れませんでした: {e}") from e

    def _delegate(self, role: str, task: str) -> str:
        """エージェントからの依頼を、専門のワーカー(小型AI)に回す。"""
        role = role.strip().lower()
        try:
            result = self.run(task, role=role if role in ROLES else None, use_cache=False, swarm=False, learn=False)
        except (BackendError, ValueError) as e:
            raise ToolError(f"ワーカーが答えられませんでした: {e}") from e
        return f"[{result.role} 担当 / {result.model}]\n{result.text}"

    def _pick_agent_model(self, role_name: str) -> ModelInfo:
        """エージェントは手順を誤りにくいことが大事なので、計測で正確なモデルを使う(未計測なら大きいモデル)。

        コードの仕事では、同じ成績ならコード向けのモデルを優先する。
        """
        candidates = [m for m in self.models() if m.can_manage] or self.models()
        scores = self._bench_scores()

        def rank(m: ModelInfo) -> tuple:
            mine = scores.get(m.name, {})
            coder = role_name == "code" and "code" in m.name.lower()
            return (mine.get(role_name, mine.get("all", -1.0)), coder, mine.get("all", -1.0), m.size_bytes)

        return max(candidates, key=rank)

    def _run_project(self, task: str, *, usage: _Usage, t0: float, on_token: OnToken | None, on_stage: OnStage | None,
                     learn: bool, approve: Callable[[dict], bool] | None, folder: str | None) -> Result:
        """新しいプロジェクトを作る(計画 → ファイルごとに書く → 確かめる → 直す。実体は project.py)。"""
        ctx = self.tool_context(approve, folder)
        model = self._pick_agent_model("code")
        _emit(on_stage, "worker", role="code", model=model.name, examples=0, agent=True)
        personal = self.personal_prompt() if learn else ""

        def plan_fn(request: str) -> dict:
            gen = self._generate(model, [{"role": "system", "content": project.PLAN_SYSTEM + personal}, {"role": "user", "content": request}],
                                 temperature=0.1, max_tokens=1500, json_schema=project.PLAN_SCHEMA)
            usage.add(gen)
            try:
                return json.loads(gen.text)
            except ValueError as e:
                raise ValueError("プロジェクトの計画を作れませんでした。もう一度試すか、言い方を変えてください") from e

        def write_fn(prompt: str) -> str:
            gen = self._generate(model, [{"role": "system", "content": project.FILE_SYSTEM + personal}, {"role": "user", "content": prompt}],
                                 temperature=0.2, max_tokens=4000)
            usage.add(gen)
            return gen.text

        done = project.create_project(request=task, ctx=ctx, plan_fn=plan_fn, write_fn=write_fn,
                                      on_stage=lambda kind, info: _emit(on_stage, kind, **info))
        text = project.report(done)
        if on_token:
            on_token(text)
        latency = time.perf_counter() - t0
        new_id = 0
        if learn:
            new_id = self.store.log(task=task, role="code", model=model.name, answer=text, source="agent", route_source="project",
                                    overhead_tokens=usage.tokens, latency_s=latency, gen_latency_s=latency)
        return Result(new_id, text, "code", model.name, "agent", "project", usage.tokens, latency, changes=summarize_changes(ctx))

    def _run_agent(self, task: str, role_name: str, *, route_source: str, usage: _Usage, t0: float, model: ModelInfo | None,
                   on_token: OnToken | None, on_stage: OnStage | None, history: list[tuple[str, str]] | None,
                   learn: bool, approve: Callable[[dict], bool] | None = None, images: list[bytes] | None = None,
                   folder: str | None = None) -> Result:
        """AIエージェントとして動かす(実体は agent.py)。"""
        ctx = self.tool_context(approve, folder)
        chosen = model or self._pick_agent_model(role_name)
        _emit(on_stage, "worker", role=role_name, model=chosen.name, examples=0, agent=True)
        goal = task
        if images:  # 画像は先に文章にしてから渡す(エージェント役のモデルが画像を扱えなくてもよいように)
            notes = [self._describe_image_bytes(image) for image in images]
            goal += "\n\n[添付画像の内容]\n" + "\n---\n".join(notes)
        out = run_agent(task=goal, role_name=role_name, model=chosen, ctx=ctx, max_steps=self.cfg.tools_max_steps,
                        generate=self._generate, history=history, on_token=on_token,
                        on_event=lambda kind, **data: _emit(on_stage, kind, **data),
                        personal=time_context(task, memories=list(self.cfg.memories) if learn else None) +(self.personal_prompt() if learn else ""))
        usage.tokens += out["tokens"]
        latency = time.perf_counter() - t0
        new_id = 0
        if learn:
            new_id = self.store.log(task=task, role=role_name, model=chosen.name, answer=out["answer"], source="agent",
                                    route_source=route_source, overhead_tokens=usage.tokens, latency_s=latency,
                                    gen_latency_s=out["gen_latency"])
        return Result(new_id, out["answer"], role_name, chosen.name, "agent", route_source, usage.tokens, latency,
                      tool_calls=out["calls"], changes=summarize_changes(ctx))

    def _describe_image_bytes(self, image: bytes) -> str:
        model = self._vision_model()
        if model is None:
            raise BackendError("画像を扱えるAIがありません(例: ollama pull gemma3:4b)")
        return self._generate(model, [{"role": "user", "content": "Describe this image in detail, including any text in it."}],
                              temperature=0.1, max_tokens=700, images=[image]).text

    # ---- 学習 ---------------------------------------------------------------

    def feedback(self, interaction_id: int, good: bool) -> None:
        """回答の良し悪しを教える。モデル選択・手本記憶・キャッシュに即座に反映される。"""
        row = self.store.set_feedback(interaction_id, 1 if good else -1)
        if row is None:
            raise KeyError(f"#{interaction_id} は存在しません")
        if good and row["source"] == "llm":
            self.memory.add(row["id"], row["role"], row["task"], row["answer"])
        elif not good:
            self.memory.remove(row["id"])

    # ---- 判断(Jev のような System 1) ----------------------------------------------

    def _decision_candidates(self) -> list[ModelInfo]:
        """判断に使えるAI(6GB 以下。コード向けのAIは判断に向かないので除く)。"""
        models = [m for m in self.models() if m.can_manage and m.size_bytes <= 6e9 and "code" not in m.name.lower()]
        return models or [m for m in self.models() if m.can_manage] or self.models()

    def decision_calibration(self) -> dict[str, dict]:
        table = self.store.settings().get("decide_calibration") or {}
        return table if isinstance(table, dict) else {}

    def _decision_model(self, fast: bool = False) -> ModelInfo:
        """判断に使うAI。較正(正解の分かっている判断での計測)をしていれば、計測で最も正確だったもの
        (振り分けの55課題で qwen2.5:1.5b が 49問、gemma3:4b は 33問。大きいほど良いとは限らない)。
        較正していなければ、読み込み済みのものから選ぶ(入れ替えを起こさない)。fast=True は常に読み込み済みを優先する。"""
        models = self._decision_candidates()
        loaded = self._loaded_models()
        ready = [m for m in models if m.name in loaded.get(m.backend, set())]
        measured = self.decision_calibration()
        known = [m for m in models if m.name in measured]
        if known and not (fast and ready and not any(m in ready for m in known)):
            pool = [m for m in known if m in ready] if fast and any(m in ready for m in known) else known
            return max(pool, key=lambda m: (measured[m.name].get("accuracy", 0.0), -measured[m.name].get("ece", 1.0), -m.size_bytes))
        if ready:
            return min(ready, key=lambda m: m.size_bytes)
        return min(models, key=lambda m: m.size_bytes) if models else self.manager_model()

    def decision_temperature(self, model_name: str) -> float:
        entry = self.decision_calibration().get(model_name) or {}
        return float(entry.get("temperature", 1.0))

    def decide(self, question: str, options: list[str] | None = None, context: str = "", model: str | None = None,
               examples: list[tuple[str, int]] | None = None) -> decide.Decision:
        """文章を作らずに、選択肢(省略時は はい/いいえ)から1つを、確率つきで選ぶ。1語だけ生成するので速い。
        examples([(材料, 正解の番号)])を渡すと、例を見せてから判断させる。"""
        question = (question or "").strip()
        if not question:
            raise ValueError("判断する質問を書いてください")
        opts = [str(o).strip()[:200] for o in (options or []) if str(o).strip()][:12] or decide.default_options(question)
        if len(opts) < 2:
            raise ValueError("選択肢は2つ以上にしてください")
        chosen = next((m for m in self.models() if m.name == model), None) if model else self._decision_model()
        if chosen is None:
            raise ValueError(f"未知のモデル: {model}")
        alternatives: list[dict[str, float]] = []
        t0 = time.perf_counter()
        gen = self._generate(chosen, decide.build_messages(question, opts, (context or "")[:8000], examples), temperature=0.0, max_tokens=1,
                             top_logprobs=alternatives)
        dist = decide.option_distribution(alternatives[0], opts) if alternatives else None
        calibrated = dist is not None
        if dist is None:  # 確率の分布を返さないAI: 生成された記号だけから決める
            index = decide.parse_answer(gen.text, opts)
            if index is None:
                raise ValueError("AIが選択肢から選べませんでした")
            dist = [1.0 if i == index else 0.0 for i in range(len(opts))]
        else:
            dist = decide.calibrate(dist, self.decision_temperature(chosen.name))
        index = max(range(len(opts)), key=dist.__getitem__)
        return decide.Decision(opts[index], index, dist[index], dict(zip(opts, dist)), chosen.name, time.perf_counter() - t0, calibrated)

    def calibrate_decisions(self, progress: Callable[[int, int, str], None] | None = None) -> list[dict]:
        """判断の確率を較正する。判断に使えるAIごとに、正解の分かっている判断(文章の意味を読み取る30問と、
        精度の計測の課題の担当の振り分け55問)を解かせ、正答率と温度を学んで保存する。
        以後の判断は、意味の判断で最も正確だったAIを、学んだ温度で使う(判断の得意不得意は課題の種類で変わる)。"""
        from .bench import load_tasks

        names = list(ROLES)
        role_options = [f"{name}: {ROLES[name].description}" for name in names]
        items = [(q, opts or decide.default_options(q), ctx, truth, "semantic") for q, opts, ctx, truth in decide.DECISION_TASKS]
        items += [("この依頼に最も合う担当はどれ?", role_options, routing_text(t["prompt"]), names.index(t["role"]), "routing")
                  for t in load_tasks() if t["role"] in names]
        models = self._decision_candidates()
        table = dict(self.decision_calibration())
        results = []
        for number, model in enumerate(models):
            samples: list[tuple[list[float], int]] = []
            correct = {"semantic": [0, 0], "routing": [0, 0]}
            spent = 0.0
            for i, (question, options, context, truth, kind) in enumerate(items, 1):
                alternatives: list[dict[str, float]] = []
                t0 = time.perf_counter()
                self._generate(model, decide.build_messages(question, options, context), temperature=0.0, max_tokens=1,
                               top_logprobs=alternatives)
                spent += time.perf_counter() - t0
                if progress:
                    progress(number * len(items) + i, len(models) * len(items), f"{model.name}: {context[:30]}")
                dist = decide.option_distribution(alternatives[0], options) if alternatives else None
                if dist is None:
                    continue
                samples.append((dist, truth))
                correct[kind][0] += int(max(range(len(dist)), key=dist.__getitem__) == truth)
                correct[kind][1] += 1
            if not samples:
                continue  # 確率の分布を返さないAI
            # 温度は意味の判断だけで学ぶ(振り分けでは自信満々に誤るAIがあり、混ぜると意味の判断の確率までならしすぎる)
            semantic_samples = samples[:correct["semantic"][1]]
            temperature = decide.fit_temperature(semantic_samples or samples)
            samples = semantic_samples or samples
            semantic, routing = correct["semantic"], correct["routing"]
            entry = {"temperature": round(temperature, 3), "accuracy": round(semantic[0] / max(1, semantic[1]), 4), "n": semantic[1],
                     "routing_accuracy": round(routing[0] / max(1, routing[1]), 4), "routing_n": routing[1],
                     "ece": round(decide.calibration_error(samples, temperature), 4), "ece_raw": round(decide.calibration_error(samples, 1.0), 4),
                     "nll": round(decide.nll(samples, temperature), 4), "nll_raw": round(decide.nll(samples, 1.0), 4),
                     "latency_s": round(spent / len(items), 3), "at": time.time()}
            table[model.name] = entry
            results.append({"model": model.name, **entry})
        if not results:
            raise ValueError("確率の分布を返すAIがないため、較正できません")
        self.store.set_setting("decide_calibration", table)
        return results

    def suggest_memory(self, task: str) -> str | None:
        """記憶の関門(Jev の使い方の例と同じ): 発言に、今後も役立つ利用者自身についての事実が含まれていれば、
        覚える候補の1文を返す(含まれていなければ None)。判断は1語だけの生成で、読み込み済みのAIを使う。"""
        text = task.strip()
        if not text or len(text) > 600 or memory_command(text) or not _FIRST_PERSON.search(text):
            return None
        model = self._decision_model(fast=True)
        # 較正した確率で 0.5 以上(実測: 本人の話は 0.53〜0.88、そうでない発言は 0.15 以下)。提案は利用者が確かめるので取りこぼしを避ける
        gate = self.decide(_MEMORY_GATE, None, text, model=model.name, examples=_GATE_SHOTS)
        if gate.index != 0 or gate.probability < 0.5:
            return None
        messages = [{"role": "system", "content": _MEMORY_REWRITE}]
        for said, fact in _MEMORY_SHOTS:  # 例を会話の形で渡す(相づちの「〜のですね」ではなく、言い切りの1文にさせる)
            messages += [{"role": "user", "content": said}, {"role": "assistant", "content": fact}]
        gen = self._generate(model, messages + [{"role": "user", "content": text[:1000]}], temperature=0.0, max_tokens=60)
        fact = re.sub(r"\s+", " ", gen.text.strip().splitlines()[0] if gen.text.strip() else "").strip("「」\"' ")
        if (not fact or "NONE" in fact.upper() or len(fact) > 120 or fact in self.cfg.memories
                or re.search(r"(ですね|ますね|でしょうか|ですか)[。!]?$", fact)):
            return None
        return fact

    # ---- 利用者に合わせる ------------------------------------------------------

    def personal_prompt(self) -> str:
        return personal_prompt(self.cfg.instructions, list(self.cfg.memories))

    def _memory_reply(self, task: str) -> str | None:
        """「覚えて」「忘れて」「何を覚えてる?」に答える(当てはまらなければ None)。"""
        command = memory_command(task)
        if command is None:
            return None
        kind, fact = command
        with self._personal_lock:
            memories = list(self.cfg.memories)
            if kind == "recall":
                if not memories:
                    return "まだ何も覚えていません。「覚えて: 〜」と頼むと覚えます。"
                return "覚えていること:\n" + "\n".join(f"- {m}" for m in memories)
            if kind == "remember":
                if fact in memories:
                    return f"もう覚えています: {fact}"
                if len(memories) >= MAX_MEMORIES:
                    return f"覚えていることが上限({MAX_MEMORIES}件)に達しています。設定の「覚えていること」から古いものを消してください。"
                self.set_setting("memories", memories + [fact])
                return f"覚えました: {fact}\n(設定の「覚えていること」で確かめたり消したりできます)"
            removed = [m for m in memories if fact in m or m in fact]
            if not removed:
                return f"覚えていることの中に「{fact}」は見つかりませんでした。"
            self.set_setting("memories", [m for m in memories if m not in removed])
            return "忘れました:\n" + "\n".join(f"- {m}" for m in removed)

    def set_setting(self, key: str, value) -> None:
        """設定を変更して保存する(次回起動時も有効)。"""
        kind = TUNABLE.get(key)
        if kind is None or type(value) is not kind:
            raise ValueError(f"変更できない設定、または値の型が違います: {key}")
        if key == "instructions":
            value = value.strip()
            if len(value) > MAX_INSTRUCTIONS:
                raise ValueError(f"AIへの指示は {MAX_INSTRUCTIONS} 文字までです")
        if key == "memories":
            cleaned: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    raise ValueError("覚えていることは文字の並びで指定してください")
                item = re.sub(r"\s+", " ", item).strip()
                if len(item) > MAX_MEMORY_CHARS:
                    raise ValueError(f"覚えていることは1件 {MAX_MEMORY_CHARS} 文字までです")
                if item and item not in cleaned:
                    cleaned.append(item)
            if len(cleaned) > MAX_MEMORIES:
                raise ValueError(f"覚えておけるのは {MAX_MEMORIES} 件までです")
            value = cleaned
        if key in ("instructions", "memories") and value != getattr(self.cfg, key):
            # 指示が変わったら、それより前に作った回答はキャッシュから使わない(時刻ではなく記録の番号で区切る)
            self._personal_epoch = self.store.last_interaction_id()
            self.store.set_setting("personal_epoch", self._personal_epoch)
        if key == "workspace" and value:
            folder = Path(value).expanduser()
            if not folder.is_dir():
                raise ValueError(f"フォルダが見つかりません: {value}")
            value = str(folder.resolve())
        if key == "fewshot_k" and not 0 <= value <= 8:
            raise ValueError("手本の件数は 0〜8 で指定してください")
        if key == "manager_model" and value != "auto" and value not in [m.name for m in self.models()]:
            raise ValueError(f"未知のモデル: {value}")
        setattr(self.cfg, key, value)
        self.store.set_setting(key, value)

    def export(self, path: str | Path) -> int:
        """悪評価を除いた履歴を、自作モデルの学習に使えるチャット形式JSONLで書き出す。"""
        rows = self.store.export_rows()
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                record = {
                    "messages": [
                        {"role": "system", "content": ROLES[r["role"]].system},
                        {"role": "user", "content": r["task"]},
                        {"role": "assistant", "content": r["answer"]},
                    ],
                    "task_role": r["role"],
                    "source_model": r["model"],
                    "quality": "good" if r["feedback"] == 1 else "verified" if r["verified"] == 1 else "unrated",
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(rows)
