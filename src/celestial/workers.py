"""ワーカー(専門の小型AI)の役割定義。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    description: str  # 管理AIが振り分けに使う説明
    system: str
    temperature: float = 0.2
    tier: str = "small"  # 好ましいモデルの大きさ(初期の事前知識。実績で上書きされる)


_LANG = " Answer in the same language as the user's request."

ROLES: dict[str, Role] = {
    r.name: r
    for r in [
        Role("general", "general questions, facts, advice, anything that fits no other category",
             "You are a concise, accurate assistant. If you are not sure, say so." + _LANG, 0.3, "small"),
        # 説明文は振り分けの精度に効く(qwen2.5:1.5b の管理AIで 55問中 44→49問。抽出・単位換算・時刻計算の誤りが減った)
        Role("code", "only when the user wants source code written, fixed, explained or reviewed (programs, shell commands, regex, SQL). "
                     "Not for pulling items out of a text",
             "You are an expert programmer. Give correct, minimal, runnable code with brief explanation." + _LANG, 0.1, "medium"),
        Role("reason", "computing the answer to arithmetic or math problems (e.g. 'calculate 17×23'), logic puzzles, step-by-step analysis, "
                       "unit conversions, time calculations",
             "You are a careful analyst. Work step by step, check your arithmetic, then state the final answer clearly." + _LANG, 0.1, "medium"),
        Role("summarize", "summarizing or condensing a given text",
             "You summarize text faithfully. Keep only what matters; never add facts that are not in the source." + _LANG, 0.2, "small"),
        Role("translate", "translating text between languages",
             "You are a professional translator. Output only the translation, natural and faithful to the source.", 0.1, "small"),
        Role("write", "creative or business writing: emails, articles, stories, copy, rewriting for tone",
             "You are a skilled writer. Produce polished text that fits the requested purpose and tone." + _LANG, 0.7, "medium"),
        Role("extract", "extracting specific items (email addresses, dates, names, numbers) from a given text, classifying, "
                        "or reformatting given text",
             "You extract and reformat information precisely. Output exactly the requested format with no commentary.", 0.0, "small"),
    ]
}

COMPLEX = "complex"
COMPLEX_DESCRIPTION = "a large multi-part task that needs several different skills above and should be split into subtasks"

_HEURISTICS: list[tuple[str, re.Pattern]] = [
    ("translate", re.compile(r"翻訳|訳して|translate|英語に|日本語に", re.I)),
    ("summarize", re.compile(r"要約|まとめて|summar|tl;?dr", re.I)),
    ("code", re.compile(r"コード|プログラム|関数|バグ|実装|python|javascript|rust|sql|regex|正規表現|```|def |class ", re.I)),
    ("reason", re.compile(r"計算|証明|何通り|確率|足す|足し|引く|掛け|割る|合計|平均|割引|換算|calculate|solve|prove|\d\s*[-+*/×÷]\s*\d", re.I)),
    ("extract", re.compile(r"抽出|抜き出|取り出|分類|json|csv|表にして|extract|classify", re.I)),
    ("write", re.compile(r"メール|文章を書|記事|物語|小説|キャッチコピー|essay|story|email", re.I)),
]

# 担当ごとの「依頼にこれがなければ、その担当ではない」手がかり。学習済みの振り分けや管理AIが自信満々に
# 誤る(メールアドレスの抜き出しを「コード」に、首都を問う質問を「抽出」に、など)のを防ぐ
_ROLE_CUES: dict[str, re.Pattern] = {
    "code": re.compile(r"コード|プログラム|関数|スクリプト|メソッド|クラス|バグ|実装|デバッグ|コマンド|シェル|ターミナル|python|javascript|"
                       r"typescript|java|kotlin|swift|dart|rust|golang|go言語|c\+\+|c#|php|ruby|sql|html|css|regex|正規表現|```|def |class |"
                       r"変数|配列|ライブラリ|api|git|docker|エラー|例外|アルゴリズム|ソート|ループ|再帰|プログラミング|ソースコード|"
                       r"アプリ|サイトを作|ツールを作|拡張機能|fizzbuzz|bot", re.I),
    "translate": re.compile(r"翻訳|訳して|訳す|訳を|translate|英語|英文|日本語に|日本語で|中国語|韓国語|フランス語|ドイツ語|スペイン語|"
                            r"イタリア語|ロシア語|ポルトガル語|in english|in japanese", re.I),
    "extract": re.compile(r"[:：「『\"\n]|次の|以下|抽出|抜き出|取り出|分類|json|csv|表に|整形|extract|classify|format", re.I),
    "summarize": re.compile(r"要約|まとめ|要点|短く|summar|tl;?dr", re.I),
}


def plausible_role(role: str, task: str) -> bool:
    """その担当らしい手がかりが依頼にあるか(手がかりの決まりがない担当は常に True)。"""
    cue = _ROLE_CUES.get(role)
    return cue is None or bool(cue.search(task))


def heuristic_role(task: str) -> str:
    """管理AIが使えないときの最後の砦となるキーワード振り分け。"""
    for name, pattern in _HEURISTICS:
        if pattern.search(task):
            return name
    return "general"
