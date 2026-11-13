"""自習: 教師役のAIに練習問題を作らせて解かせ、自作AIの学習データを短時間で貯める。

普段の利用を待たずに学習データが増えるので、自作AIの学習を早く始められる。
練習問題は担当(役割)を指定して作るため、振り分けの学習にもそのまま使える。
"""

from __future__ import annotations

import json
import random
import re
from typing import Callable

from .backends import BackendError, ModelInfo
from .bench_tasks import BUILTIN
from .textsim import cosine, vectorize
from .workers import ROLES

TOPICS = ["料理", "旅行", "仕事", "学校の勉強", "科学", "健康", "スポーツ", "買い物", "IT", "歴史", "お金", "天気", "音楽",
          "家事", "地理", "動物", "映画", "交通", "言葉", "趣味"]
# 依頼文だけで完結した問題の見本(計測の課題とは別の内容にしてある)
SAMPLES = {
    "general": "光合成とは何かを、小学生にも分かるように説明して",
    "code": "Pythonで、文字列に含まれる母音の数を返す関数 count_vowels(s) を書いて",
    "reason": "1個120円のりんごを5個と、1個80円のみかんを3個買うと合計はいくら?",
    "summarize": "次の文章を1文で要約して: 市は来年度から、駅前の駐輪場を有料化する。放置自転車を減らすのが狙いで、料金は1日100円。定期利用者には月額の割引も用意する。",
    "translate": "次を英語に翻訳して: 来週の金曜日に会議があります",
    "write": "友人の結婚式に出席できないことを伝える、丁寧で短いメッセージを書いて",
    "extract": "次の文から都市名をすべて抜き出して: 私は東京から大阪を経由して福岡へ行きました",
}
# 本文を伴う担当は、問題の書式を指定する(指定しないと「この記事を要約して」のような本文の無い問題が量産される)
FORMATS = {
    "summarize": "Every request must be: an instruction to summarize (with a length limit), then a colon, then a complete "
                 "passage of 3 to 5 sentences written out in full.",
    "translate": "Every request must be: an instruction naming the target language, then a colon, then the exact sentence "
                 "to translate written out. Mix Japanese-to-English and English-to-Japanese.",
    "extract": "Every request must be: an instruction saying exactly what to extract, classify or reformat and in what "
               "output format, then a colon, then the complete input text written out.",
}
_BODY_MIN = {"summarize": 50, "translate": 5, "extract": 12}  # コロンの後ろに必要な本文の長さ
_BODY = re.compile(r"[:：]\s*(\S.*)$", re.S)  # 半角・全角どちらのコロンでもよい
_BROKEN = re.compile(r"task_id|^\s*[{\[]|…|\.\.\.|〇〇|○○|XXX", re.I)
_NEEDS_OUTSIDE = re.compile(r"最新|現在の|リアルタイム|天気予報|株価|為替|添付|この(記事|メール|ファイル|文書|サイト|商品|レシピ|映画)|履歴")
_ASKS_BACK = re.compile(r"(教えて|入力して|提供して|共有して|貼り付けて)(ください|いただけ)|提供されていません|でしょうか[??]")


def well_formed(role_name: str, task: str) -> bool:
    """機械的に分かる不良を弾く: 壊れた出力、外部の情報が要る依頼、本文が書かれていない依頼。"""
    if _BROKEN.search(task):
        return False
    body = _BODY.search(task)
    if role_name in _BODY_MIN:
        return body is not None and len(body.group(1).strip()) >= _BODY_MIN[role_name]
    return not _NEEDS_OUTSIDE.search(task)


def answer_ok(answer: str) -> bool:
    """教師が聞き返している回答は、問題が成立していない印なので使わない。"""
    # 聞き返しは回答の冒頭に現れる。本文中の「ご都合を教えてください」のような文面(メールの下書きなど)は対象にしない
    return bool(answer) and len(answer) <= 3000 and not _ASKS_BACK.search(answer[:120])


Progress = Callable[[int, int, str], None]


def pick_teacher(app) -> ModelInfo:
    """教師役を選ぶ。計測で最も正解率の高いモデル(自作AIは除く)。計測が無ければ最大のモデル。"""
    candidates = [m for m in app.models() if m.can_manage]
    if not candidates:
        raise BackendError("教師役にできるAIがありません。Ollama にモデルを入れてください。")
    scores = {}
    for name, table in app.bench_priors().items():
        counts = sum(c for _, _, c in table.values())
        if counts:
            scores[name] = sum(acc * c for acc, _, c in table.values()) / counts
    return max(candidates, key=lambda m: (scores.get(m.name, -1.0), m.size_bytes))


def _make_tasks(app, teacher: ModelInfo, role_name: str, n: int, rng: random.Random) -> list[str]:
    role = ROLES[role_name]
    schema = {"type": "object", "required": ["tasks"], "properties": {"tasks": {"type": "array", "items": {"type": "string"}}}}
    prompt = (f"Write {n} different, realistic requests that a Japanese user might send to an AI assistant, all in this category:\n"
              f"{role.name}: {role.description}\n\nRules:\n- Write every request in Japanese.\n"
              "- Each request must be complete and self-contained: it must be answerable from the request text plus general "
              "knowledge alone. Never refer to a file, an attachment, 'this article', the user's own data, or current "
              "prices/weather/news. If a text is to be processed, write that full text inside the request.\n"
              f"- Example of a good request: {SAMPLES[role_name]}\n"
              + (f"- {FORMATS[role_name]}\n" if role_name in FORMATS else "") +
              "- Vary the topic and the difficulty. Keep each request short (the instruction part under 100 characters).\n"
              f"- Topics to draw from: {', '.join(rng.sample(TOPICS, 4))}.")
    gen = app.backends[teacher.backend].generate(
        teacher.name, [{"role": "system", "content": "You create practice requests for training an AI assistant. Output JSON."},
                       {"role": "user", "content": prompt}],
        temperature=0.9, max_tokens=2000, json_schema=schema)
    try:
        tasks = json.loads(gen.text).get("tasks", [])
    except (ValueError, AttributeError):
        return []
    return [t.strip() for t in tasks if isinstance(t, str) and 8 <= len(t.strip()) <= 600]


def self_contained(app, teacher: ModelInfo, task: str) -> bool:
    """依頼文と一般知識だけで正しく答えられる問題か。そうでない問題は、教師が作り話で答えてしまうので使わない。"""
    system = ("Decide whether the request can be answered correctly using only the text of the request and general knowledge. "
              "Set ok=false if it needs anything that is not written in the request: private data (accounts, files, the user's "
              "own documents or history), current or real-time data (prices, weather, news, schedules), a text, menu, article "
              "or attachment that is mentioned but not actually included, or essential details the user did not give.")
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    gen = app.backends[teacher.backend].generate(
        teacher.name, [{"role": "system", "content": system}, {"role": "user", "content": "Request:\n" + task}],
        temperature=0.0, max_tokens=20, json_schema=schema)
    try:
        return json.loads(gen.text).get("ok") is True
    except (ValueError, AttributeError):
        return False


def review_study(app, *, teacher: str | None = None, on_progress: Progress | None = None) -> dict:
    """保存済みの自習データを見直し、依頼文だけでは答えられない問題を取り除く。"""
    model = next((m for m in app.models() if m.name == teacher), None) if teacher else pick_teacher(app)
    if model is None:
        raise ValueError(f"未知のモデル: {teacher}")
    rows = app.store.study_rows()
    removed = 0
    for i, row in enumerate(rows, 1):
        if on_progress:
            on_progress(i, len(rows), "自習データを見直し中")
        if not (well_formed(row["role"], row["task"]) and answer_ok(row["answer"]) and self_contained(app, model, row["task"])):
            app.store.delete_study(row["task"])
            removed += 1
    return {"checked": len(rows), "removed": removed, "teacher": model.name}


def run_study(app, *, per_role: int = 15, teacher: str | None = None, roles: list[str] | None = None,
              on_progress: Progress | None = None, seed: int | None = None) -> dict:
    """役割ごとに per_role 件の練習問題を作って解き、保存する。戻り値は {追加件数, 教師, 役割別件数}。"""
    rng = random.Random(seed)
    model = next((m for m in app.models() if m.name == teacher), None) if teacher else pick_teacher(app)
    if model is None:
        raise ValueError(f"未知のモデル: {teacher}")
    role_names = roles or list(ROLES)
    total = per_role * len(role_names)
    # 計測用の課題と似た問題は捨てる(計測の答えを学習させないため)
    bench_vectors = [vectorize(t["prompt"]) for t in BUILTIN]
    seen = [vectorize(r["task"]) for r in app.store.study_rows()]
    added, by_role = 0, {}

    def report(message: str) -> None:
        if on_progress:
            on_progress(added, total, message)

    for role_name in role_names:
        got, attempts = 0, 0
        while got < per_role and attempts < 6:
            attempts += 1
            report(f"{role_name}: 練習問題を作成中")
            for task in _make_tasks(app, model, role_name, min(8, per_role - got + 2), rng):
                if got >= per_role:
                    break
                if not well_formed(role_name, task):
                    continue
                vec = vectorize(task)
                # 同じ言い回し(「次を英語に翻訳して:」など)で始まるだけでは弾かないよう、閾値は高めにする
                if any(cosine(vec, b) >= 0.75 for b in bench_vectors) or any(cosine(vec, s) >= 0.9 for s in seen):
                    continue
                if not self_contained(app, model, task):
                    continue
                report(f"{role_name}: 解答中")
                result = app.run(task, role=role_name, model=model.name, use_cache=False, swarm=False, learn=False)
                answer = result.text.strip()
                if not answer_ok(answer):
                    continue
                if app.store.add_study(task=task, role=role_name, model=model.name, answer=answer):
                    app.router.add(task, role_name)
                    seen.append(vec)
                    got += 1
                    added += 1
        by_role[role_name] = got
    report("完了")
    return {"added": added, "teacher": model.name, "by_role": by_role}
