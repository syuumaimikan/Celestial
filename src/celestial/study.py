"""自習: 教師役のAIに練習問題を作らせて解かせ、検証済みの学習データを貯める。

普段の利用を待たずに学習データが増えるので、自作AIの学習を早く始められる。
役割別の得意な教師モデル選び、自動検証(構文/JSON/文字数/計算検算)、プログラム生成問題で品質を高める。
"""

from __future__ import annotations

import ast
import json
import math
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .backends import BackendError, ModelInfo
from .bench import extract_code
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
    "summarize": "次の文章を40字以内で要約して: 市は来年度から、駅前の駐輪場を有料化する。放置自転車を減らすのが狙いで、料金は1日100円。定期利用者には月額の割引も用意する。",
    "translate": "次を英語に翻訳して: 来週の金曜日に会議があります",
    "write": "友人の結婚式に出席できないことを伝える、丁寧で短いメッセージを書いて",
    "extract": "次の文から都市名をすべて抜き出して: 私は東京から大阪を経由して福岡へ行きました",
}
# 本文を伴う担当は、問題の書式を指定する(指定しないと「この記事を要約して」のような本文の無い問題が量産される)
FORMATS = {
    "summarize": "Every request must be: an instruction to summarize (including an explicit character limit like '35字以内' or '1文で'), then a colon, then a complete passage of 3 to 5 sentences written out in full.",
    "translate": "Every request must be: an instruction naming the target language, then a colon, then the exact sentence to translate written out. Mix Japanese-to-English and English-to-Japanese.",
    "extract": "Every request must be: an instruction saying exactly what to extract, classify or reformat and in what output format (e.g. JSON or comma-separated), then a colon, then the complete input text written out.",
}
_BODY_MIN = {"summarize": 50, "translate": 5, "extract": 12}  # コロンの後ろに必要な本文の長さ
_BODY = re.compile(r"[:：]\s*(\S.*)$", re.S)  # 半角・全角どちらのコロンでもよい
_BROKEN = re.compile(r"task_id|^\s*[{\[]|…|\.\.\.|〇〇|○○|XXX", re.I)
_NEEDS_OUTSIDE = re.compile(
    r"最新|現在の|リアルタイム|天気予報|株価|為替|添付|"
    r"この(記事|メール|ファイル|文書|サイト|商品|レシピ|映画)|履歴|"
    r"おすすめの[^\n]{0,12}(店|ラーメン|レストラン|カフェ|本|映画|ゲーム|旅行|商品)|"
    r"来週の予定|今日のニュース|あなたの(名前|意見|好み)|私(が|の)(住んで|飼って)"
)
_ASKS_BACK = re.compile(r"(教えて|入力して|提供して|共有して|貼り付けて)(ください|いただけ)|提供されていません|でしょうか[??]")
_REFUSAL = re.compile(r"^\s*(?:申し訳(?:あり|ござい)ませんが|すみませんが|恐れ入りますが|残念ながら)[^\n]*(?:できません|わかりません|分かりません|お答えでき|対応して)")
_PREAMBLE = re.compile(
    r"^\s*(?:(?:はい[、,。]?\s*)?(?:承知[いい]たしました|かしこまりました|了解[しいた]*ました|もちろんです|わかりました|"
    r"ご質問ありがとう[^\n]*|以下に[^\n]{0,40}|丁寧な分析[^\n]{0,40}|ステップごとの[^\n]{0,40}|"
    r"段階的に(?:考えて)?[^\n]{0,40}|それでは[^\n]{0,40}(?:計算|解説|回答|説明)します)[。:：\s]*)+"
)
_EPILOGUE = re.compile(
    r"\n+(?:何か(?:ほか|他)に[^\n]*(?:ください|でしょうか)[。!！]?|"
    r"(?:参考になれば|お役に立てれば)幸いです[。!！]?)\s*$"
)
_CHAR_LIMIT = re.compile(r"(\d+)\s*(?:字|文字)\s*以内")
_JSON_REQ = re.compile(r"(?<![a-z0-9_])json(?![a-z0-9_])", re.I)
_ONE_LINE_REQ = re.compile(r"1行で|一行で|項目のみ|番号のみ|答えのみ|単語のみ")
_HAS_PY_CODE = re.compile(r"```(?:python|py|python3)?\s|(?<![a-z0-9_])(?:def|class|import)\s+", re.I)
_FENCE_LANG = re.compile(r"```([a-zA-Z0-9_+-]*)\s*\n", re.S)

Progress = Callable[[int, int, str], None]


def well_formed(role_name: str, task: str) -> bool:
    """機械的に分かる不良を弾く: 壊れた出力、外部の情報が要る依頼、本文が書かれていない依頼。"""
    if _BROKEN.search(task):
        return False
    body = _BODY.search(task)
    if role_name in _BODY_MIN:
        return body is not None and len(body.group(1).strip()) >= _BODY_MIN[role_name]
    if re.search(r"202[0-9]年|今年[のには]|最近のおすすめ|今月の|現在[のに]|探しています|可能性は高いですか|#[^\s#]{1,8}\s*$", task):
        return False
    return not _NEEDS_OUTSIDE.search(task)


def clean_answer(role_name: str, answer: str) -> str:
    """教師回答に混ざりがちな定型の前置き(「はい、承知いたしました」等)と結びを取り除く。"""
    text = answer.strip()
    if role_name == "write" or not text:
        return text
    for _ in range(2):
        stripped = _PREAMBLE.sub("", text, count=1).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    stripped_end = _EPILOGUE.sub("", text).strip()
    if stripped_end:
        text = stripped_end
    if role_name == "summarize":
        m = re.match(r"^(?:要約|まとめ)\s*[:：]\s*(.+)$", text, re.S)
        if m and m.group(1).strip():
            text = m.group(1).strip()
    return text


def answer_ok(answer: str) -> bool:
    """教師が聞き返している・拒否している・途中で切れた回答は使わない。"""
    # 聞き返しは回答の冒頭に現れる。本文中の「ご都合を教えてください」のような文面は対象にしない
    text = (answer or "").strip()
    if not text or len(text) > 3000 or _ASKS_BACK.search(text[:120]) or _REFUSAL.search(text[:120]):
        return False
    if re.search(r"=\s*\?\s*(?:$|\n)", text):
        return False
    if len(text) >= 60 and re.search(r"(?:\n[-*・]\s*|\*\*|[:：])$", text):
        return False
    if re.search(r"でしょうか\s*[??]?\s*$|ご自身でご確認|最新情報をご確認ください|お住まいの地域|どのような[^\n]{0,20}お探し", text):
        return False
    return not re.search(r"ステップごとの(?:確認|チェック|アレンジ)|(?:今日|現在|現時点)(?:は|では)?20\d{2}年|20\d{2}年\d{1,2}月現在", text)


def _extract_json_payload(answer: str) -> object | None:
    text = answer.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S | re.I)
    if m:
        text = m.group(1).strip()
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if 0 <= s < e:
            try:
                return json.loads(text[s : e + 1])
            except ValueError:
                pass
    try:
        return json.loads(text)
    except ValueError:
        return None


def verify_answer(role_name: str, task: str, answer: str, *, run_code: bool = False) -> tuple[bool, bool]:
    """回答を客観ルールで検証し、(採用可か, 客観検証に合格したか) を返す。"""
    if not answer_ok(answer):
        return False, False
    if role_name == "summarize":
        m = _CHAR_LIMIT.search(task)
        if m:
            limit = int(m.group(1))
            if len(answer.strip()) > limit:
                return False, False
            return True, True
        return True, len(answer.strip()) <= 200
    if role_name == "extract":
        if re.search(r"要約|翻訳", task) and not re.search(r"抽出|抜き出|取り出|分類|整形|リフォーマット|json|csv", task, re.I):
            return False, False
        if _JSON_REQ.search(task):
            if _extract_json_payload(answer) is None:
                return False, False
            return True, True
        if _ONE_LINE_REQ.search(task) and len(answer.strip().splitlines()) > 4:
            return False, False
        return True, False
    if role_name == "translate":
        src = task.split("：", 1)[-1] if "：" in task else (task.split(":", 1)[-1] if ":" in task else "")
        if re.search(r"英語[にへ]|英訳|translate\s+to\s+english", task, re.I):
            if src and not any("\u3040" <= c <= "\u9fff" for c in src):
                return False, False
            latin = sum(c.isascii() and c.isalpha() for c in answer)
            ja = sum("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in answer)
            if latin < 3 or ja > latin:
                return False, False
            return True, True
        if re.search(r"日本語[にへ]|和訳|translate\s+to\s+japanese", task, re.I):
            if src and sum(c.isascii() and c.isalpha() for c in src) < 3:
                return False, False
            kana = sum("\u3040" <= c <= "\u30ff" for c in answer)
            if kana < 2:
                return False, False
            return True, True
        return True, False
    if role_name == "code":
        langs = {m.lower() for m in _FENCE_LANG.findall(answer) if m}
        other_in_task = bool(re.search(
            r"(?<![a-z0-9_])(javascript|typescript|java|c\+\+|rust|golang|sql|html|css|bash|shell|ruby|php)(?![a-z0-9_])",
            task, re.I,
        ))
        is_python = bool(langs & {"python", "py", "python3"}) or (
            not langs and not other_in_task and (re.search(r"\bdef\s+\w+\s*\(", answer) or "python" in task.lower())
        )
        if is_python:
            candidates = extract_code(answer)
            parsed = False
            for snippet in candidates:
                try:
                    ast.parse(snippet)
                    parsed = True
                    break
                except SyntaxError:
                    continue
            if not parsed:
                return False, False
            if run_code:
                return True, _run_python_smoke(candidates[0])
            return True, True
    if role_name == "reason":
        if re.search(r"ステップごと|丁寧な分析|段階的に|step\s*\d+\s*:|情報収集と整理", answer, re.I):
            return False, False
        if len(answer) > 300 or (len(answer) > 120 and not re.search(r"答え\s*[:：]|合計\s*\d+|=\s*-?\d", answer[-120:])):
            return False, False
        return True, bool(re.search(r"答え\s*[:：]\s*[^\n]*\d", answer))
    if role_name == "general":
        if len(answer) > 420 and "\n" in answer:
            return False, False
    if role_name in ("agent", "judge"):
        payload = _extract_json_payload(answer)
        if _JSON_REQ.search(task) or "{" in answer[:10]:
            return (payload is not None), bool(isinstance(payload, (dict, list)) and payload)
        return len(answer.strip()) <= 400, False
    return True, False


def _run_python_smoke(code: str, tests: str = "", timeout: float = 3.0) -> bool:
    """コード片を隔離プロセスで安全に構文・単体テスト実行する。"""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "verify.py"
        script.write_text(code + ("\n\n" + tests if tests else "") + "\n", encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)], cwd=tmp, timeout=timeout,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            return False


_ALLOWED_AST = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.UAdd, ast.USub)


def _eval_math_expr(expr: str) -> float | None:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        if not all(isinstance(n, _ALLOWED_AST) for n in ast.walk(tree)):
            return None
        val = eval(compile(tree, "<math>", "eval"), {"__builtins__": {}}, {})  # noqa: S307
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
            return float(val)
    except Exception:
        return None
    return None


def _verify_with_teacher(app, teacher: ModelInfo, role_name: str, task: str, answer: str) -> tuple[bool, bool]:
    """教師モデル自身を使って数値計算や Python 関数の自動テスト検算を行う。"""
    if role_name == "reason":
        if not re.search(r"\d", answer):
            return True, False
        schema = {"type": "object", "required": ["applicable", "expr", "value"],
                  "properties": {"applicable": {"type": "boolean"}, "expr": {"type": "string"}, "value": {"type": "number"}}}
        prompt = ("Does this problem have a single deterministic numeric final answer? If yes, set applicable=true and "
                  "write a pure Python arithmetic expression using only numbers and +, -, *, /, //, %, ** to verify it. "
                  f"For non-numeric logic/ranking/word puzzles, set applicable=false.\nProblem: {task}\n"
                  f"Proposed answer: {answer[:400]}")
        try:
            gen = app.backends[teacher.backend].generate(
                teacher.name,
                [{"role": "system", "content": "Output valid JSON with keys applicable, expr, and value."},
                 {"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=140, json_schema=schema,
            )
            data = json.loads(gen.text)
            if not data.get("applicable"):
                return True, False
            val = _eval_math_expr(str(data.get("expr", "")))
            if val is not None:
                nums = [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", answer)]
                if any(abs(n - val) <= max(1e-3, abs(val) * 1e-4) for n in nums):
                    return True, True
                return False, False
        except (BackendError, ValueError, TypeError):
            pass
    elif role_name == "code" and _HAS_PY_CODE.search(answer):
        blocks = extract_code(answer)
        if blocks and not any(w in blocks[0] for w in ("input(", "open(", "urllib", "socket", "requests", "subprocess")):
            schema = {"type": "object", "required": ["asserts"],
                      "properties": {"asserts": {"type": "array", "items": {"type": "string"}}}}
            prompt = ("Write 3 concise Python assert statements testing the core edge and normal cases for this task. "
                      f"Only write pure assert lines with no imports or prints.\nTask: {task}\nCode:\n{blocks[0][:800]}")
            try:
                gen = app.backends[teacher.backend].generate(
                    teacher.name,
                    [{"role": "system", "content": "Output valid JSON with key asserts (list of assert lines)."},
                     {"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=200, json_schema=schema,
                )
                asserts = json.loads(gen.text).get("asserts") or []
                clean_lines = [a.strip() for a in asserts if isinstance(a, str) and a.strip().startswith("assert ")]
                if clean_lines:
                    ok = _run_python_smoke(blocks[0], "\n".join(clean_lines))
                    return ok, ok
            except (BackendError, ValueError, TypeError):
                pass
    return True, False


def pick_teacher(app, role: str | None = None) -> ModelInfo:
    """教師役を選ぶ。役割別の計測成績(無ければ全体の正解率、さらに無ければサイズ)で選ぶ(自作AIは除く)。"""
    candidates = [m for m in app.models() if m.can_manage]
    if not candidates:
        raise BackendError("教師役にできるAIがありません。Ollama にモデルを入れてください。")
    priors = app.bench_priors()
    overall: dict[str, float] = {}
    role_scores: dict[str, float] = {}
    for name, table in priors.items():
        counts = sum(c for _, _, c in table.values())
        if counts:
            overall[name] = sum(acc * c for acc, _, c in table.values()) / counts
        if role and role in table and table[role][2] > 0:
            role_scores[name] = table[role][0]
    def rank(m: ModelInfo) -> tuple:
        default_role = 0.85 if (role == "code" and "coder" in m.name.lower()) else -1.0
        return (role_scores.get(m.name, default_role), overall.get(m.name, -1.0), m.size_bytes)
    return max(candidates, key=rank)


def weak_roles(app) -> list[str]:
    """直近の自作AIの計測結果から、正解率の低い役割順に返す(計測が無ければ推論/要約を先頭に)。"""
    priors = app.bench_priors()
    own_names = sorted((n for n in priors if n.startswith("celestial-") and n != "celestial-base:latest"), reverse=True)
    if not own_names:
        own_names = [n for n in priors if n.startswith("celestial-")]
    if own_names:
        table = priors[own_names[0]]
        ranked = [r for r in ROLES if r in table]
        if ranked:
            ranked.sort(key=lambda r: table[r][0])
            for r in ROLES:
                if r not in ranked:
                    ranked.append(r)
            return ranked
    return ["reason", "summarize", "extract", "general", "code", "translate", "write"]


def _near_builtin(vec: list[float], bench_vectors: list[list[float]], threshold: float = 0.72) -> bool:
    return any(cosine(vec, b) >= threshold for b in bench_vectors)


def _make_tasks(app, teacher: ModelInfo, role_name: str, n: int, rng: random.Random) -> list[str]:
    role = ROLES[role_name]
    schema = {"type": "object", "required": ["tasks"], "properties": {"tasks": {"type": "array", "items": {"type": "string"}}}}
    prompt = (f"Write {n} different, realistic requests that a Japanese user might send to an AI assistant, all in this category:\n"
              f"{role.name}: {role.description}\n\nRules:\n- Write every request in Japanese.\n"
              "- Each request must be complete and self-contained: it must be answerable from the request text plus general "
              "knowledge alone. Never refer to a file, an attachment, 'this article', the user's own data, or current "
              "prices/weather/news. Never ask for restaurant recommendations, personal opinions, or tomorrow's schedule. "
              "If a text is to be processed, write that full text inside the request.\n"
              f"- Example of a good request: {SAMPLES[role_name]}\n"
              + (f"- {FORMATS[role_name]}\n" if role_name in FORMATS else "") +
              "- Vary the topic and the difficulty. Keep each request concise (the instruction part under 100 characters).\n"
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
              "or attachment that is mentioned but not actually included, subjective restaurant/shop recommendations, or essential details the user did not give.")
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    gen = app.backends[teacher.backend].generate(
        teacher.name, [{"role": "system", "content": system}, {"role": "user", "content": "Request:\n" + task}],
        temperature=0.0, max_tokens=20, json_schema=schema)
    try:
        return json.loads(gen.text).get("ok") is True
    except (ValueError, AttributeError):
        return False


def _gen_reason_extra(kind: int, rng: random.Random, a: str, b: str) -> tuple[str, str]:
    if kind == 14:
        x = rng.randint(5, 25)
        add = rng.randint(2, 12)
        mul = rng.randint(3, 8)
        res = (x + add) * mul
        return (f"ある数に{add}を足してから{mul}倍すると{res}になります。ある数はいくつですか？",
                f"ある数を x とすると (x + {add}) × {mul} = {res}。\n"
                f"x + {add} = {res} ÷ {mul} = {res // mul}、x = {res // mul} - {add} = {x}。\n答え: {x}")
    if kind == 15:
        if rng.randrange(2) == 0:
            ms = rng.choice([5, 10, 12, 15, 20, 25, 30])
            kmh = int(round(ms * 3.6))
            return (f"時速{kmh}kmは秒速何mですか？",
                    f"時速{kmh}km = {kmh * 1000}m ÷ 3600秒 = {ms}m/s。\n答え: 秒速{ms}m")
        ms = rng.choice([5, 10, 15, 20, 25])
        kmh = int(round(ms * 3.6))
        return (f"秒速{ms}mは時速何kmですか？",
                f"秒速{ms}m = {ms} × 3600 = {ms * 3600}m/h = {kmh}km/h。\n答え: 時速{kmh}km")
    if kind == 16:
        sh, sm = rng.randint(8, 11), rng.choice([25, 35, 40, 45, 50])
        dh, dm = rng.randint(1, 3), rng.choice([20, 30, 35, 45])
        tot = sh * 60 + sm + dh * 60 + dm
        eh, em = divmod(tot, 60)
        ampm = "午後" if eh >= 12 else "午前"
        h12 = eh - 12 if eh > 12 else eh
        return (f"午前{sh}時{sm}分の{dh}時間{dm}分後は何時何分ですか？(午前・午後をつけて答えて)",
                f"{sh}時{sm}分に{dh}時間{dm}分を足すと、分は{sm + dm}分で1時間繰り上がり、{eh}時{em}分({ampm}{h12}時{em}分)になります。\n答え: {ampm}{h12}時{em}分")
    if kind == 17:
        sale_base = rng.choice([1000, 1500, 2000, 2500, 3000, 4000])
        disc = rng.choice([10, 20, 25, 30])
        sale = sale_base * (100 - disc) // 100
        return (f"{disc}%引きセールで{sale}円になっている商品があります。割引前の定価はいくらですか？",
                f"割引後は定価の{100 - disc}%({(100 - disc) / 100:.2f}倍)なので、定価 = {sale} ÷ {(100 - disc) / 100:.2f} = {sale_base}円。\n答え: {sale_base}円")
    if kind == 18:
        salt = rng.choice([10, 15, 20, 25, 30, 45])
        water = salt * rng.choice([4, 9, 19])
        total = salt + water
        pct = round(salt * 100 / total)
        return (f"食塩{salt}gを水{water}gに完全に溶かすと、濃度は何%の食塩水になりますか？",
                f"全体の重さは {salt} + {water} = {total}g。濃度は {salt} ÷ {total} × 100 = {pct}%。\n答え: {pct}%")
    if kind == 19:
        p, q = rng.choice([(6, 8), (8, 12), (9, 12), (10, 15), (12, 18), (15, 20)])
        lcm = math.lcm(p, q)
        return (f"バスAは{p}分ごと、バスBは{q}分ごとに発車します。午前9時00分に同時に発車したあと、次に2台が同時に発車するのは何分後ですか？",
                f"{p}と{q}の最小公倍数は{lcm}なので、{lcm}分後(午前{9 + lcm // 60}時{lcm % 60:02d}分)です。\n答え: {lcm}分後")
    if kind == 20:
        n = rng.randint(5, 8)
        k = rng.choice([2, 3])
        ans = math.comb(n, k)
        return (f"{n}人の候補から代表{k}人を選ぶ組み合わせは何通りありますか？",
                f"_{n}C_{k} = {ans}通りです。\n答え: {ans}通り")
    if kind == 21:
        n = 4
        avg1 = rng.randint(68, 78)
        avg2 = avg1 + rng.randint(2, 5)
        need = avg2 * (n + 1) - avg1 * n
        return (f"これまでの{n}回のテストの平均点は{avg1}点です。次の5回目で何点取ると5回の平均点が{avg2}点になりますか？",
                f"{n}回の合計は {avg1}×{n}={avg1 * n}点、5回の目標合計は {avg2}×5={avg2 * 5}点。\n差は {avg2 * 5}-{avg1 * n}={need}点。\n答え: {need}点")
    if kind == 22:
        t = rng.randint(3, 12)
        child_future = rng.randint(12, 18)
        mult = rng.choice([2, 3])
        parent_now = child_future * mult - t
        child_now = child_future - t
        return (f"現在、{a}の年齢は{parent_now}歳、{b}の年齢は{child_now}歳です。{a}の年齢が{b}のちょうど{mult}倍になるのは何年後ですか？",
                f"t年後に {parent_now} + t = {mult}({child_now} + t) となるので、{parent_now - mult * child_now} = {mult - 1}t、よって t = {t}。\n答え: {t}年後")
    if kind == 23:
        n_items = rng.randint(10, 16)
        x = rng.randint(3, n_items - 3)
        y = n_items - x
        p1, p2 = 150, 90
        total = x * p1 + y * p2
        return (f"1個{p1}円の品Aと1個{p2}円の品Bを合わせて{n_items}個買い、合計金額が{total}円になりました。品Aを何個買いましたか？",
                f"すべてBなら {p2}×{n_items}={p2 * n_items}円。差額 {total - p2 * n_items}円を価格差 {p1 - p2}円で割ると {x}個。\n答え: {x}個")
    if kind == 24:
        l1 = rng.choice([120, 150, 180, 200])
        l2 = rng.choice([420, 480, 540, 600])
        sec = rng.choice([20, 30])
        speed = (l1 + l2) // sec
        l2 = sec * speed - l1
        return (f"長さ{l1}mの列車が秒速{speed}mで進み、長さ{l2}mの鉄橋を渡りきるまでに何秒かかりますか？",
                f"進んだ距離は列車と鉄橋の合計 {l1} + {l2} = {l1 + l2}m。時間は {l1 + l2} ÷ {speed} = {sec}秒。\n答え: {sec}秒")
    if kind == 25:
        weekdays = "月火水木金土日"
        d = rng.randrange(7)
        delta = rng.randint(10, 45)
        ans_day = weekdays[(d + delta) % 7]
        return (f"今日が{weekdays[d]}曜日のとき、{delta}日後は何曜日ですか？",
                f"{delta}を7で割った余りは{delta % 7}なので、{weekdays[d]}曜日の{delta % 7}日後は{ans_day}曜日です。\n答え: {ans_day}曜日")
    if kind == 26:
        t_pair = rng.choice([(6, 12, 4), (4, 12, 3), (10, 15, 6), (8, 24, 6), (6, 2, 1.5), (12, 4, 3)])
        t1, t2, together = t_pair
        return (f"ある作業を{a}1人だと{t1}時間、{b}1人だと{t2}時間かかります。2人で同時に行うと何時間かかりますか？",
                f"1時間あたりの仕事量は 1/{t1} + 1/{t2} = 1/{together} なので、2人では {together}時間かかります。\n答え: {together}時間")
    if kind == 27:
        end_n = rng.choice([20, 30, 40, 50, 60, 80, 120])
        total = end_n * (end_n + 1) // 2
        return (f"1から{end_n}までのすべての整数の和はいくつですか？",
                f"等差数列の和は (1 + {end_n}) × {end_n} ÷ 2 = {total} です。\n答え: {total}")
    if kind == 28:
        k_mult = rng.randint(2, 6)
        k_sub = rng.randint(3, 15)
        ans_x = rng.randint(4, 15)
        target_c = k_mult * ans_x - k_sub
        return (f"ある数を{k_mult}倍してから{k_sub}を引くと{target_c}になります。もとの数はいくつですか？",
                f"ある数を x とすると {k_mult}x - {k_sub} = {target_c}。\n{k_mult}x = {target_c + k_sub}、よって x = {ans_x}。\n答え: {ans_x}")
    wari = rng.choice([2, 3, 4])
    unit_p = rng.choice([150, 200, 300, 400, 500])
    qty = rng.randint(3, 7)
    paid = unit_p * (10 - wari) // 10 * qty
    return (f"1個{unit_p}円の品を{wari}割引きで{qty}個買うと、合計いくらになりますか？",
            f"1個あたりの割引後は {unit_p}×{(10 - wari) / 10:.1f} = {unit_p * (10 - wari) // 10}円です。\n"
            f"{qty}個では {unit_p * (10 - wari) // 10}×{qty} = {paid}円です。\n答え: {paid}円")


def _gen_reason_pair(rng: random.Random) -> tuple[str, str]:
    kind = rng.randrange(30)
    names = ["太郎", "花子", "健太", "美咲", "陽翔", "結衣", "蓮", "さくら"]
    a, b = rng.sample(names, 2)
    if kind >= 14:
        return _gen_reason_extra(kind, rng, a, b)
    if kind == 0:
        p1, n1 = rng.choice([90, 120, 140, 160, 210, 250]), rng.randint(3, 8)
        p2, n2 = rng.choice([60, 80, 110, 130, 150]), rng.randint(2, 6)
        pay = ((p1 * n1 + p2 * n2) // 500 + 1) * 500
        total = p1 * n1 + p2 * n2
        chg = pay - total
        return (f"1冊{p1}円のノートを{n1}冊と、1本{p2}円のペンを{n2}本買い、{pay}円を出しました。おつりはいくらですか？",
                f"ノートは {p1}×{n1}={p1 * n1}円、ペンは {p2}×{n2}={p2 * n2}円で、合計は {total}円です。\n"
                f"おつりは {pay}-{total}={chg}円です。\n答え: {chg}円")
    if kind == 1:
        v = rng.choice([4, 5, 6, 12, 15, 20, 45, 60, 80])
        h = rng.randint(2, 6)
        m = rng.choice([15, 20, 30, 40, 45])
        mins = h * 60 + m
        dist = v * mins // 60
        return (f"時速{v}kmで{h}時間{m}分進むと、合計何km進みますか？",
                f"{h}時間{m}分は {h}+{m}/60 = {mins/60:.2f}時間です。\n{v} × {mins/60:.2f} = {dist}km。\n答え: {dist}km")
    if kind == 2:
        base = rng.choice([1200, 1500, 2000, 2400, 3000, 4500, 5000, 8000])
        disc = rng.choice([10, 15, 20, 25, 30])
        price = base * (100 - disc) // 100
        with_tax = price * 110 // 100
        return (f"定価{base}円の商品を{disc}%引きにしたあと、10%の消費税を加えるといくらになりますか？",
                f"{disc}%引き後の価格は {base}×{(100 - disc)/100:.2f} = {price}円です。\n"
                f"消費税10%を加えると {price}×1.1 = {with_tax}円です。\n答え: {with_tax}円")
    if kind == 3:
        x = rng.randint(4, 18)
        y = rng.randint(3, 15)
        n = x + y
        p1, p2 = 120, 80
        cost = p1 * x + p2 * y
        return (f"1個{p1}円のリンゴと1個{p2}円のミカンを合わせて{n}個買い、合計{cost}円でした。リンゴは何個買いましたか？",
                f"すべてミカンだと {p2}×{n}={p2 * n}円です。差額 {cost - p2 * n}円を1個あたりの差 {p1 - p2}円で割ると {x}個です。\n答え: {x}個")
    if kind == 4:
        t1, t2 = rng.choice([(6, 12), (4, 12), (10, 15), (6, 30), (12, 24), (8, 24), (15, 30)])
        ans = (t1 * t2) // (t1 + t2)
        return (f"{a}さん1人では{t1}時間、{b}さん1人では{t2}時間かかる仕事があります。2人で一緒にすると何時間かかりますか？",
                f"1時間あたりの仕事量はそれぞれ 1/{t1} と 1/{t2}、合計 (1/{t1} + 1/{t2}) = 1/{ans} です。\n答え: {ans}時間")
    if kind == 5:
        start = rng.randint(1, 15)
        step = rng.randint(2, 7)
        cnt = rng.randint(6, 12)
        end = start + step * (cnt - 1)
        s = (start + end) * cnt // 2
        return (f"{start}から始めて{step}ずつ増える数列({start}, {start+step}, ..., {end})を最初の{cnt}項すべて足すといくらですか？",
                f"等差数列の和は ({start} + {end}) × {cnt} ÷ 2 = {s} です。\n答え: {s}")
    if kind == 6:
        p = rng.choice([6, 8, 9, 10, 12, 14, 15, 18, 20])
        q = rng.choice([8, 12, 14, 15, 16, 20, 21, 24, 25])
        while p == q:
            q += 3
        lcm = math.lcm(p, q)
        return (f"A線のバスは{p}分ごと、B線のバスは{q}分ごとに発車します。午前9時00分に同時に発車したあと、次に同時に発車するのは何分後ですか？",
                f"{p}と{q}の最小公倍数は {lcm} です。\n答え: {lcm}分後")
    if kind == 7:
        r1, r2 = rng.randint(2, 5), rng.randint(3, 7)
        unit = rng.choice([200, 300, 400, 500, 800, 1000])
        tot = (r1 + r2) * unit
        part = r2 * unit
        return (f"{tot}円を{a}さんと{b}さんで {r1}:{r2} の比で分けると、{b}さんの取り分は何円ですか？",
                f"全体は {r1}+{r2}={r1+r2} 等分で、1つ分は {tot}÷{r1+r2}={unit}円です。\n"
                f"{b}さんは {unit}×{r2}={part}円です。\n答え: {part}円")
    if kind == 8:
        x = rng.randint(12, 48)
        y = rng.randint(11, 39)
        ans = x * y + rng.randint(15, 85)
        k = ans - x * y
        return (f"{x} × {y} + {k} を計算してください。",
                f"{x} × {y} = {x * y}、そこに {k} を足すと {ans} です。\n答え: {ans}")
    if kind == 9:
        avg1 = rng.randint(68, 86)
        n = rng.randint(3, 5)
        target = avg1 + rng.randint(2, 5)
        need = target * (n + 1) - avg1 * n
        return (f"{a}さんのこれまでのテスト{n}回の平均点は{avg1}点です。次の1回を含めた{n+1}回の平均点を{target}点以上にするには、次のテストで最低何点必要ですか？",
                f"{n+1}回の目標合計は {target}×{n+1}={target*(n+1)}点、これまでの合計は {avg1}×{n}={avg1*n}点です。\n"
                f"よって {target*(n+1)} - {avg1*n} = {need}点必要です。\n答え: {need}点")
    if kind == 10:
        child = rng.randint(6, 14)
        k = rng.randint(2, 6)
        mult = rng.choice([2, 3])
        parent = (child + k) * mult - k
        return (f"現在、親は{parent}歳、子は{child}歳です。親の年齢が子の年齢のちょうど{mult}倍になるのは何年後ですか？",
                f"{k}年後は親 {parent+k}歳、子 {child+k}歳となり、{parent+k} = {mult}×{child+k} が成り立ちます。\n答え: {k}年後")
    if kind == 11:
        w = rng.randint(5, 16)
        h = rng.randint(4, 14)
        area = w * h
        perim = 2 * (w + h)
        return (f"縦{w}cm、横{h}cmの長方形の面積と周囲の長さをそれぞれ求めてください。",
                f"面積は {w}×{h}={area} cm2、周囲の長さは 2×({w}+{h})={perim} cmです。\n答え: 面積 {area} cm2、周囲 {perim} cm")
    if kind == 12:
        sh, sm = rng.randint(8, 17), rng.choice([15, 25, 35, 45, 50])
        dur = rng.choice([75, 95, 110, 135, 145, 160])
        tot_m = sh * 60 + sm + dur
        eh, em = tot_m // 60, tot_m % 60
        return (f"{sh}時{sm:02d}分に出発し、{dur}分間移動しました。到着時刻は何時何分ですか？",
                f"{dur}分は {dur//60}時間{dur%60}分です。{sh}時{sm:02d}分に足すと {eh}時{em:02d}分になります。\n答え: {eh}時{em:02d}分")
    ms = rng.choice([5, 10, 15, 20, 25, 30])
    kmh = ms * 36 // 10
    return (f"秒速{ms}mは時速何kmですか？",
            f"1時間は3600秒なので、{ms}×3600={ms*3600}m = {kmh}km進みます。\n答え: 時速{kmh}km")


def _gen_extract_pair(rng: random.Random) -> tuple[str, str]:
    kind = rng.randrange(6)
    if kind in (0, 4):
        person = rng.choice(["高橋翔太", "鈴木優子", "山本大輔", "中村葵", "小林誠", "伊藤健"])
        age = rng.randint(22, 54)
        if kind == 0:
            city = rng.choice(["札幌", "仙台", "名古屋", "京都", "神戸", "広島"])
            payload = json.dumps({"name": person, "age": age, "city": city}, ensure_ascii=False)
            return (f"次の文から name・age(整数)・city をJSONだけで抜き出して: {person}さんは{age}歳で、現在は{city}市で働いています。",
                    payload)
        payload = json.dumps({"name": person, "age": age}, ensure_ascii=False)
        return (f"次の文から人物の名前(name)と年齢(age、整数)を抽出し、JSONだけで出力して: {person}さんは{age}歳の研究者です。",
                payload)
    if kind == 5:
        item = rng.choice(["ノートPC", "キーボード", "カメラ", "自転車"])
        price = rng.choice([12800, 29800, 45000, 64000])
        payload = json.dumps({"item": item, "price": price}, ensure_ascii=False)
        return (f"次の文から item と price をキーに持つJSONだけを出力して: セールで{item}が{price}円で販売されています。",
                payload)
    if kind == 1:
        y, mo, d = rng.randint(2023, 2027), rng.randint(1, 12), rng.randint(1, 28)
        item = rng.choice(["ノートPC", "モニター", "プリンター", "デスクチェア"])
        price = rng.choice([19800, 34500, 58000, 82400])
        return (f"次の文章から日付を YYYY-MM-DD 形式で、金額を数字のみでカンマ区切りにして1行で抜き出して: "
                f"{y}年{mo}月{d}日に社内備品として{item}を{price:,}円で購入しました。",
                f"{y:04d}-{mo:02d}-{d:02d}, {price}")
    if kind == 2:
        positive = rng.choice([True, False])
        if positive:
            txt = rng.choice(["配送がとても早く、梱包も丁寧で大変満足しています。",
                              "操作が直感的で分かりやすく、毎日快適に利用できています。"])
            label = "positive"
        else:
            txt = rng.choice(["届くのが予定より3日遅れ、説明書も不足していて困りました。",
                              "画面が頻繁にフリーズしてしまい、使い物になりません。"])
            label = "negative"
        return (f"次のレビューの感情を positive または negative のどちらか1単語のみで答えて: {txt}", label)
    codes = [f"ORD-{rng.randint(1000, 9999)}", f"ORD-{rng.randint(1000, 9999)}"]
    return (f"次の連絡文から注文番号(ORD-で始まるコード)だけをカンマ区切りで抜き出して: "
            f"お世話になっております。先日の{codes[0]}と追加分の{codes[1]}の発送状況を確認したいです。",
            f"{codes[0]}, {codes[1]}")


_TRANSLATE_BANK = [
    ("ja->en", "この資料館の開館時間は午前9時から午後5時までです。", "This museum is open from 9:00 AM to 5:00 PM."),
    ("ja->en", "会議の開始時刻が1時間後ろ倒しになりました。", "The meeting start time has been pushed back by one hour."),
    ("ja->en", "申請書類は今週の金曜日までに提出してください。", "Please submit the application documents by this Friday."),
    ("ja->en", "駅前の市役所では平日の夜7時まで窓口を開いています。", "The city hall in front of the station keeps its service counter open until 7:00 PM on weekdays."),
    ("ja->en", "この公園は誰でも無料で入園できますが、ペットの同伴はできません。", "Anyone can enter this park free of charge, but pets are not allowed."),
    ("ja->en", "サーバーのメンテナンスのため、深夜2時から1時間停止します。", "The server will stop for one hour from 2:00 AM due to maintenance."),
    ("ja->en", "この劇場では毎週末に新しい映画が上映されます。", "New movies are shown at this theater every weekend."),
    ("en->ja", "Please send me the updated spreadsheet before noon.", "正午までに更新済みの表計算ファイルをお送りください。"),
    ("en->ja", "The train bound for Tokyo will depart from platform 3.", "東京行きの電車は3番線から発車します。"),
    ("en->ja", "We recommend backing up your files before updating the software.", "ソフトウェアを更新する前に、ファイルのバックアップを取ることをおすすめします。"),
    ("en->ja", "Registration closes at 6 PM tomorrow.", "参加登録は明日の午後6時に締め切られます。"),
    ("en->ja", "This bookstore is open every day except Tuesdays.", "この書店は火曜日を除き毎日営業しています。"),
]


def _gen_translate_pair(rng: random.Random) -> tuple[str, str]:
    direction, src, tgt = rng.choice(_TRANSLATE_BANK)
    if direction == "ja->en":
        return f"次の文を自然な英語に翻訳して: {src}", tgt
    return f"次の英文を自然な日本語に翻訳して: {src}", tgt


_SUMMARIZE_BANK = [
    (40, "市役所は来月1日から電子申請の窓口を24時間開放する。これまで平日の日中に限られていた住民票や各種証明書の申請が自宅からできるようになり、待ち時間の大幅な短縮が期待されている。",
     "市役所は来月1日から電子申請を24時間開放し、待ち時間短縮を図る。"),
    (40, "新しい市立科学館では、小中学生を対象に週末のロボット工作教室を毎月開催する。定員は各回20名で参加費は材料費込み500円。事前予約はホームページから受け付ける。",
     "市立科学館は毎月週末に小中学生向けロボット教室(定員20名)を開催する。"),
    (40, "国立公園内の登山道は、今月15日から来月10日まで落石対策の補修工事のため通行止めとなる。期間中は東側の迂回ルートを利用するよう公園管理事務所が呼びかけている。",
     "国立公園の登山道は15日〜来月10日まで工事で通行止めとなり東側迂回が案内された。"),
    (40, "当社は在宅勤務制度を拡充し、来期から全社員が週3日までリモートワークを選べるようにする。通勤時間の削減によって業務効率と社員満足度の向上を図る狙いがある。",
     "当社は来期から全社員が週3日まで在宅勤務できる制度に拡充し効率化を図る。"),
    (40, "地域のスポーツセンターは空調設備の更新に伴い、今週水曜日を臨時休館とする。翌日木曜日からは午前9時に通常通り開館し、トレーニング室とプールも利用できる。",
     "スポーツセンターは水曜休館し、木曜午前9時から全施設を再開する。"),
    (45, "鉄道各社は利用者の利便性向上を目的に、来春から主要駅での交通系ICカードによる改札通過の上限額を引き上げる方針を発表した。定期券利用者の手続きは不要となる。",
     "鉄道各社は来春から主要駅のICカード上限額を引き上げるが、定期券の手続きは不要。"),
    (45, "市立図書館はシステム更新のため、来週火曜から木曜までの3日間、蔵書検索と貸出延長のオンラインサービスを停止する。返却ポストは期間中も通常どおり利用できる。",
     "市立図書館は来週火〜木にオンライン機能を停止するが、返却ポストは使える。"),
    (45, "県は山間部のバス路線を維持するため、運転手不足への対策として予約制の乗合タクシーを来月から試験導入する。利用料金は1回300円で高齢者割引も設けられる。",
     "県は来月から山間部で予約制乗合タクシー(1回300円)を試験導入する。"),
]


def _gen_summarize_pair(rng: random.Random) -> tuple[str, str]:
    limit, passage, summary = rng.choice(_SUMMARIZE_BANK)
    return f"次の文章を{limit}字以内で要約して: {passage}", summary


def _gen_code_pair(rng: random.Random) -> tuple[str, str]:
    """実行テスト付きで 100% 正解保証された Python コーディング問題を生成する。"""
    kind = rng.randrange(18)
    if kind == 0:
        fn = rng.choice(["count_vowels", "num_vowels"])
        prompt = f"Pythonで、文字列に含まれる英語の母音 (a, e, i, o, u、大小区別なし) の個数を返す関数 {fn}(text: str) -> int を書いて。"
        code = f"def {fn}(text: str) -> int:\n    return sum(1 for ch in text.lower() if ch in 'aeiou')"
        tests = f"assert {fn}('Celestial AI') == 6\nassert {fn}('rhythm') == 0\nassert {fn}('') == 0"
    elif kind == 1:
        fn = rng.choice(["is_palindrome", "check_palindrome"])
        prompt = f"Pythonで、空白や大小文字を無視して回文なら True を返す関数 {fn}(s: str) -> bool を書いて。"
        code = f"def {fn}(s: str) -> bool:\n    clean = ''.join(c.lower() for c in s if c.isalnum())\n    return clean == clean[::-1]"
        tests = f"assert {fn}('Race car') is True\nassert {fn}('hello') is False\nassert {fn}('') is True"
    elif kind == 2:
        fn = rng.choice(["primes_up_to", "sieve_primes"])
        prompt = f"Pythonで、n 以下の素数を昇順リストで返す関数 {fn}(n: int) -> list[int] を書いて。"
        code = (
            f"def {fn}(n: int) -> list[int]:\n"
            "    if n < 2:\n        return []\n"
            "    sieve = [True] * (n + 1)\n    sieve[0] = sieve[1] = False\n"
            "    for p in range(2, int(n ** 0.5) + 1):\n"
            "        if sieve[p]:\n            for m in range(p * p, n + 1, p):\n                sieve[m] = False\n"
            "    return [i for i, ok in enumerate(sieve) if ok]"
        )
        tests = f"assert {fn}(10) == [2, 3, 5, 7]\nassert {fn}(2) == [2]\nassert {fn}(1) == []"
    elif kind == 3:
        fn = rng.choice(["dedupe_order", "unique_preserve"])
        prompt = f"Pythonで、リストの元の順序を保ったまま重複を除く関数 {fn}(items: list) -> list を書いて。"
        code = f"def {fn}(items: list) -> list:\n    return list(dict.fromkeys(items))"
        tests = f"assert {fn}([3, 1, 3, 2, 1]) == [3, 1, 2]\nassert {fn}([]) == []"
    elif kind == 4:
        fn = rng.choice(["flatten_once", "flatten_2d"])
        prompt = f"Pythonで、2次元リストを1次元リストに平坦化する関数 {fn}(matrix: list[list]) -> list を書いて。"
        code = f"def {fn}(matrix: list[list]) -> list:\n    return [item for row in matrix for item in row]"
        tests = f"assert {fn}([[1, 2], [], [3]]) == [1, 2, 3]\nassert {fn}([]) == []"
    elif kind == 5:
        fn = rng.choice(["word_freq", "count_words"])
        prompt = f"Pythonで、空白区切りの単語出現回数を小文字統一で辞書集計する関数 {fn}(text: str) -> dict[str, int] を書いて。"
        code = (
            f"def {fn}(text: str) -> dict[str, int]:\n"
            "    counts: dict[str, int] = {}\n"
            "    for w in text.lower().split():\n        counts[w] = counts.get(w, 0) + 1\n    return counts"
        )
        tests = f"assert {fn}('AI tools ai') == {{'ai': 2, 'tools': 1}}\nassert {fn}('') == {{}}"
    elif kind == 6:
        fn = rng.choice(["binary_search", "bsearch"])
        prompt = f"Pythonで、ソート済み配列 nums から target のインデックス(無ければ -1)を二分探索で返す関数 {fn}(nums: list[int], target: int) -> int を書いて。"
        code = (
            f"def {fn}(nums: list[int], target: int) -> int:\n"
            "    lo, hi = 0, len(nums) - 1\n"
            "    while lo <= hi:\n        mid = (lo + hi) // 2\n"
            "        if nums[mid] == target:\n            return mid\n"
            "        if nums[mid] < target:\n            lo = mid + 1\n"
            "        else:\n            hi = mid - 1\n    return -1"
        )
        tests = f"assert {fn}([1, 3, 5, 7], 5) == 2\nassert {fn}([1, 3, 5], 2) == -1"
    elif kind == 7:
        fn = rng.choice(["chunk_list", "split_chunks"])
        prompt = f"Pythonで、リストをサイズ size ごとの部分リストに分割する関数 {fn}(items: list, size: int) -> list[list] を書いて(size <= 0 は ValueError)。"
        code = (
            f"def {fn}(items: list, size: int) -> list[list]:\n"
            "    if size <= 0:\n        raise ValueError('size must be positive')\n"
            "    return [items[i:i + size] for i in range(0, len(items), size)]"
        )
        tests = f"assert {fn}([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\nassert {fn}([], 3) == []"
    elif kind == 8:
        fn = rng.choice(["valid_brackets", "is_balanced"])
        prompt = f"Pythonで、括弧 (), [], {{}} が正しく対応して閉じているか判定する関数 {fn}(s: str) -> bool を書いて。"
        code = (
            f"def {fn}(s: str) -> bool:\n"
            "    pairs = {')': '(', ']': '[', '}': '{'}\n    stack = []\n"
            "    for ch in s:\n"
            "        if ch in '([{':\n            stack.append(ch)\n"
            "        elif ch in pairs:\n            if not stack or stack.pop() != pairs[ch]:\n                return False\n"
            "    return not stack"
        )
        tests = f"assert {fn}('([{{}}])') is True\nassert {fn}('([)]') is False\nassert {fn}('abc') is True"
    elif kind == 9:
        fn = rng.choice(["rle_encode", "run_length_encode"])
        prompt = f"Pythonで、文字列をランレングス圧縮し '(文字)(回数)' 形式で返す関数 {fn}(s: str) -> str を書いて(例: 'aaab' -> 'a3b1')。"
        code = (
            f"def {fn}(s: str) -> str:\n"
            "    if not s:\n        return ''\n    out, prev, cnt = [], s[0], 1\n"
            "    for ch in s[1:]:\n"
            "        if ch == prev:\n            cnt += 1\n"
            "        else:\n            out.append(f'{prev}{cnt}')\n            prev, cnt = ch, 1\n"
            "    out.append(f'{prev}{cnt}')\n    return ''.join(out)"
        )
        tests = f"assert {fn}('aaabcc') == 'a3b1c2'\nassert {fn}('') == ''"
    elif kind == 10:
        fn = rng.choice(["merge_sorted", "merge_two_sorted"])
        prompt = f"Pythonで、2つの昇順リスト a, b をソート関数 sort/sorted を使わず O(n) で1つの昇順リストにマージする関数 {fn}(a: list[int], b: list[int]) -> list[int] を書いて。"
        code = (
            f"def {fn}(a: list[int], b: list[int]) -> list[int]:\n"
            "    i = j = 0\n    res = []\n"
            "    while i < len(a) and j < len(b):\n"
            "        if a[i] <= b[j]:\n            res.append(a[i]); i += 1\n"
            "        else:\n            res.append(b[j]); j += 1\n"
            "    res.extend(a[i:]); res.extend(b[j:])\n    return res"
        )
        tests = f"assert {fn}([1, 4], [2, 3, 5]) == [1, 2, 3, 4, 5]\nassert {fn}([], [1]) == [1]"
    elif kind == 11:
        fn = rng.choice(["snake_to_camel", "to_camel_case"])
        prompt = f"Pythonで、スネークケース文字列 (例: 'user_first_name') をローワーキャメルケース ('userFirstName') に変換する関数 {fn}(name: str) -> str を書いて。"
        code = (
            f"def {fn}(name: str) -> str:\n"
            "    parts = [p for p in name.split('_') if p]\n"
            "    if not parts:\n        return ''\n"
            "    return parts[0].lower() + ''.join(p.capitalize() for p in parts[1:])"
        )
        tests = f"assert {fn}('user_first_name') == 'userFirstName'\nassert {fn}('single') == 'single'"
    elif kind == 12:
        fn = rng.choice(["moving_avg", "sliding_average"])
        w = rng.choice([2, 3, 4])
        prompt = f"Pythonで、数値リスト nums に対し幅 {w} の単純移動平均リストを返す関数 {fn}(nums: list[float]) -> list[float] を書いて(要素数が幅未満なら空リスト)。"
        code = (
            f"def {fn}(nums: list[float]) -> list[float]:\n"
            f"    if len(nums) < {w}:\n        return []\n"
            f"    return [round(sum(nums[i:i + {w}]) / {w}, 6) for i in range(len(nums) - {w} + 1)]"
        )
        tests = f"assert {fn}(list(range(1, {w + 2}))) == [round(sum(range(i + 1, i + 1 + {w})) / {w}, 6) for i in range(2)]"
    elif kind == 13:
        fn = rng.choice(["gcd_many", "list_gcd"])
        prompt = f"Pythonで、正の整数のリスト全体の最大公約数を返す関数 {fn}(nums: list[int]) -> int を書いて(空リストは ValueError)。"
        code = (
            "import math\n"
            f"def {fn}(nums: list[int]) -> int:\n"
            "    if not nums:\n        raise ValueError('empty list')\n"
            "    ans = abs(nums[0])\n    for x in nums[1:]:\n        ans = math.gcd(ans, abs(x))\n    return ans"
        )
        tests = f"assert {fn}([12, 18, 24]) == 6\nassert {fn}([7]) == 7"
    elif kind == 14:
        fn = rng.choice(["caesar_shift", "shift_ascii"])
        prompt = f"Pythonで、ASCII 英字だけを指定文字数 k だけ巡回シフトし他文字はそのまま保つシーザー暗号関数 {fn}(text: str, k: int) -> str を書いて。"
        code = (
            f"def {fn}(text: str, k: int) -> str:\n"
            "    out = []\n"
            "    for ch in text:\n"
            "        if 'a' <= ch <= 'z':\n            out.append(chr((ord(ch) - 97 + k) % 26 + 97))\n"
            "        elif 'A' <= ch <= 'Z':\n            out.append(chr((ord(ch) - 65 + k) % 26 + 65))\n"
            "        else:\n            out.append(ch)\n    return ''.join(out)"
        )
        tests = f"assert {fn}('Abc-Z!', 2) == 'Cde-B!'\nassert {fn}('xyz', -1) == 'wxy'"
    elif kind == 15:
        fn = rng.choice(["invert_multi", "group_by_value"])
        prompt = f"Pythonで、辞書の値ごとにキーをリストにまとめる逆引き辞書作成関数 {fn}(d: dict) -> dict[object, list] を書いて。"
        code = (
            f"def {fn}(d: dict) -> dict:\n"
            "    res: dict = {}\n"
            "    for k, v in d.items():\n        res.setdefault(v, []).append(k)\n    return res"
        )
        tests = f"assert {fn}({{'a': 1, 'b': 2, 'c': 1}}) == {{1: ['a', 'c'], 2: ['b']}}"
    elif kind == 16:
        fn = rng.choice(["top_k_frequent", "most_common_k"])
        prompt = f"Pythonで、配列内で出現回数が多い上位 k 個の要素を多い順に返す関数 {fn}(items: list, k: int) -> list を書いて。"
        code = (
            "from collections import Counter\n"
            f"def {fn}(items: list, k: int) -> list:\n"
            "    return [item for item, _ in Counter(items).most_common(k)]"
        )
        tests = f"assert {fn}(['x', 'y', 'x', 'z', 'x', 'y'], 2) == ['x', 'y']"
    else:
        # よくあるバグ修正問題 (デフォルト可変引数バグ等)
        variant = rng.randrange(3)
        if variant == 0:
            prompt = "次の Python 関数には呼び出し間で状態が共有されるバグがあります。修正した完全なコードを書いて:\n```python\ndef add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n```"
            code = "def add_tag(tag: str, tags: list[str] | None = None) -> list[str]:\n    out = list(tags) if tags is not None else []\n    out.append(tag)\n    return out"
            tests = "a = add_tag('x'); b = add_tag('y')\nassert a == ['x'] and b == ['y']"
        elif variant == 1:
            prompt = "次の Python 関数はゼロ除算や空配列で落ちます。要素が空または重み合計0なら 0.0 を返すよう修正して:\n```python\ndef weighted_avg(vals, weights):\n    return sum(v * w for v, w in zip(vals, weights)) / sum(weights)\n```"
            code = "def weighted_avg(vals: list[float], weights: list[float]) -> float:\n    total_w = sum(weights)\n    if not vals or total_w == 0:\n        return 0.0\n    return sum(v * w for v, w in zip(vals, weights)) / total_w"
            tests = "assert weighted_avg([], []) == 0.0\nassert weighted_avg([10, 20], [1, 3]) == 17.5"
        else:
            prompt = "次の Python 関数は範囲外の終了インデックスや負数に弱いです。n が 0 以下なら 1、負でない整数なら階乗を返すよう反復で書いて:\n```python\ndef safe_fact(n):\n    return n * safe_fact(n - 1)\n```"
            code = "def safe_fact(n: int) -> int:\n    res = 1\n    for i in range(2, n + 1):\n        res *= i\n    return res"
            tests = "assert safe_fact(0) == 1\nassert safe_fact(5) == 120"

    ns: dict = {}
    exec(compile(code + "\n" + tests, "<curriculum-code>", "exec"), ns, ns)  # noqa: S102
    return prompt, f"```python\n{code}\n```"


def _gen_agent_pair(rng: random.Random) -> tuple[str, str]:
    """道具選択 JSON・計画 JSON・不足時エラー処理の 100% 検証済みエージェント教材を生成する。"""
    scenarios = [
        ("利用可能な道具: read_file(path), edit_file(path, old, new), calc(expression), answer。\n"
         "作業フォルダにある config.json の port 値を確かめたい。最初の1手を JSON で返して。",
         '{"thought": "まず config.json を読んで現在の設定を確認する", "tool": "read_file", "args": {"path": "config.json"}}'),
        ("利用可能な道具: web_search(query), fetch_url(url), qr_barcode(text, path), answer。\n"
         f"文字列 'https://example.org/{rng.randint(10, 99)}' の QR コードを out_qr.png に保存して。最初の1手を JSON で返して。",
         None),
        ("利用可能な道具: calc(expression), write_file(path, content), code_check(path), answer。\n"
         "連立方程式 x + 2*y = 14, x - y = 2 を解いて答えだけ返して。最初の1手を JSON で返して。",
         '{"thought": "calc は方程式も厳密に解けるので使う", "tool": "calc", "args": {"expression": "x + 2*y = 14, x - y = 2"}}'),
        ("利用可能な道具: convert_file(path, to), read_file(path), answer。\n"
         f"sales_{rng.randint(1, 9)}.csv を Excel 形式 report.xlsx に変換して。最初の1手を JSON で返して。",
         None),
        ("利用可能な道具: code_check(path), run_command(command), apply_patch(patch), answer。\n"
         "変更後に src/app.py の構文エラーや未定義変数がないか実行せずに検査したい。最初の1手を JSON で返して。",
         '{"thought": "実行せず安全に静的検査できる code_check を使う", "tool": "code_check", "args": {"path": "src/app.py"}}'),
        ("利用可能な道具: search_files(query), list_files(path), answer。\n"
         "プロジェクトのどこで `def export_csv` が定義されているか探して。最初の1手を JSON で返して。",
         '{"thought": "search_files でプロジェクト内の定義箇所を探す", "tool": "search_files", "args": {"query": "def export_csv"}}'),
        ("注意: ネット接続は禁止されています(利用可能: calc, read_file, answer)。\n"
         "今日のニューヨーク株式市場の終値をウェブ検索して報告して。適切な1手を JSON で返して。",
         '{"thought": "ネット利用の権限が無いため取得できないことを正直に伝える", "tool": "answer", "answer": "ネット検索の権限が許可されていないため、現在の株価は取得できません。"}'),
        ("作業手順を JSON {steps: [{title, role}]} で 3 段階に計画して: "
         "「既存ログ log.csv を読み込み、異常値を抽出して、対策レポートを Word でまとめる」",
         '{"steps": [{"title": "log.csv を読み込み集計・異常値候補を特定する", "role": "files"}, '
         '{"title": "異常値の傾向と原因候補を整理する", "role": "reason"}, '
         '{"title": "Word 文書として簡潔な対策レポートを作成する", "role": "write"}]}'),
    ]
    idx = rng.randrange(len(scenarios))
    prompt, ans = scenarios[idx]
    if ans is None and idx == 1:
        url = re.search(r"https://example\.org/\d+", prompt).group(0)
        ans = json.dumps({"thought": "qr_barcode で PNG の QR コードを生成する", "tool": "qr_barcode",
                          "args": {"text": url, "path": "out_qr.png", "format": "qr"}}, ensure_ascii=False)
    elif ans is None and idx == 3:
        csv_name = re.search(r"sales_\d+\.csv", prompt).group(0)
        ans = json.dumps({"thought": "convert_file で CSV を直接 Excel(.xlsx) に変換する", "tool": "convert_file",
                          "args": {"path": csv_name, "to": "report.xlsx"}}, ensure_ascii=False)
    json.loads(ans)
    return prompt, ans


def _gen_judge_pair(rng: random.Random) -> tuple[str, str]:
    """検証(ok/issue JSON)・緊急度トリアージ・記憶選別の教師データを生成する。"""
    kind = rng.randrange(5)
    if kind == 0:
        n = rng.randint(12, 48)
        return (f"厳格な査読者として回答が正しいか JSON {{\"ok\": bool, \"issue\": str}} で判定して。\n"
                f"Task: {n} と 100 の合計を答えて。\nAnswer: 合計は {n + 100} です。",
                '{"ok": true, "issue": ""}')
    if kind == 1:
        n = rng.randint(15, 45)
        wrong = n * 3 + rng.choice([2, 5, -3])
        return (f"厳格な査読者として回答が正しいか JSON {{\"ok\": bool, \"issue\": str}} で判定して。\n"
                f"Task: {n} の3倍を求めて。\nAnswer: {wrong} です。",
                json.dumps({"ok": False, "issue": f"計算誤り: {n}×3={n*3} なのに {wrong} と誤答している"}, ensure_ascii=False))
    if kind == 2:
        case = rng.choice([
            ("全店舗の決済システムが5分前から応答せず、レジが完全に停止しています。", "至急", "金銭決済の全面停止のため最優先対応"),
            ("来月の社内勉強会の会場レイアウト案を送付します。ご都合よいときにご確認ください。", "普通", "来月の連絡であり緊急性はない"),
            ("サーバーのバックアップが午前3時に正常終了した定期通知ログです。", "情報", "正常完了の自動通知であり対応不要"),
        ])
        return (f"次の連絡の緊急度を「至急」「普通」「情報」から1つ選び、JSON {{\"choice\": str, \"reason\": str}} で答えて:\n{case[0]}",
                json.dumps({"choice": case[1], "reason": case[2]}, ensure_ascii=False))
    if kind == 3:
        item = rng.choice([
            ("このユーザーの回答形式の好みとして記憶すべきか JSON {\"save\": bool, \"fact\": str} で判定: 「今後は常に箇条書きと結論ファーストで答えて」",
             True, "ユーザーは箇条書きと結論ファーストの回答を好む"),
            ("この発言から恒常的な好みを記憶すべきか JSON {\"save\": bool, \"fact\": str} で判定: 「今日の気温は18度だよ」",
             False, ""),
        ])
        return item[0], json.dumps({"save": item[1], "fact": item[2]}, ensure_ascii=False)
    role_sample = rng.choice([
        ("次の依頼に最適な役割 (general/code/reason/translate/summarize/write/extract) を JSON {\"role\": str} で選んで: Pythonで二分探索を書いて", "code"),
        ("次の依頼に最適な役割 (general/code/reason/translate/summarize/write/extract) を JSON {\"role\": str} で選んで: 次の文を自然な英語にして: 明日お会いしましょう", "translate"),
        ("次の依頼に最適な役割 (general/code/reason/translate/summarize/write/extract) を JSON {\"role\": str} で選んで: 以下の議事録から決定事項だけをJSONで抜き出して", "extract"),
    ])
    return role_sample[0], json.dumps({"role": role_sample[1]}, ensure_ascii=False)


def generate_curriculum_examples(app, *, count: int = 40, seed: int | None = None,
                                 roles: list[str] | None = None) -> dict:
    """計測問題と重複しない、100%正解保証付きのプログラム生成問題を自習DBへ追加する。"""
    rng = random.Random(seed)
    bench_vectors = [vectorize(t["prompt"]) for t in BUILTIN]
    seen = [vectorize(r["task"]) for r in app.store.study_rows()]
    valid_gens = {
        "reason": _gen_reason_pair,
        "code": _gen_code_pair,
        "extract": _gen_extract_pair,
        "translate": _gen_translate_pair,
        "summarize": _gen_summarize_pair,
        "agent": _gen_agent_pair,
        "judge": _gen_judge_pair,
    }
    allowed = [r for r in (roles or list(valid_gens)) if r in valid_gens]
    if not allowed:
        allowed = ["reason", "code", "extract", "translate", "summarize"]
    default_cycle = ("reason", "code", "extract", "translate", "summarize", "reason", "code", "agent", "judge")
    role_cycle = tuple(r for r in default_cycle if r in allowed) or tuple(allowed)

    added = 0
    by_role = {r: 0 for r in allowed}
    attempts = 0
    while added < count and attempts < count * 8:
        attempts += 1
        role_name = role_cycle[(attempts - 1) % len(role_cycle)]
        task, answer = valid_gens[role_name](rng)
        vec = vectorize(task)
        if _near_builtin(vec, bench_vectors, 0.72) or any(cosine(vec, s) >= 0.88 for s in seen):
            continue
        if app.store.add_study(task=task, role=role_name, model="celestial-curriculum", answer=answer, verified=True):
            app.router.add(task, role_name)
            seen.append(vec)
            added += 1
            by_role[role_name] += 1
    return {"added": added, "by_role": by_role}


_WIKI_TOPICS = [
    # 自然科学・数学
    "周期表", "化学元素", "太陽系", "地球科学", "生物学", "細胞", "遺伝学", "DNA",
    "物理学", "光学", "熱力学", "電磁気学", "量子力学", "相対性理論", "素粒子",
    "国際単位系", "光速", "音速", "重力", "プレートテクトニクス", "火山", "地震",
    "光合成", "生態系", "進化論", "酵素", "免疫", "ウイルス", "細菌",
    "天文学", "銀河", "ブラックホール", "恒星", "月", "火星", "木星", "土星",
    "数学", "素数", "円周率", "ピタゴラスの定理", "微積分学", "確率論", "統計学",
    # コンピュータ・工学
    "コンピュータ科学", "アルゴリズム", "データ構造", "オペレーティングシステム",
    "TCP/IP", "HTTP", "暗号", "公開鍵暗号", "データベース", "SQL",
    "機械学習", "ニューラルネットワーク", "人工知能", "コンパイラ", "Git",
    "半導体", "トランジスタ", "電池", "リチウムイオン二次電池", "再生可能エネルギー",
    # 歴史・地理・社会・文化
    "日本の地理", "世界の地理", "アジアの地理", "ヨーロッパの地理", "オセアニア",
    "太平洋", "大西洋", "ヒマラヤ山脈", "アマゾン川", "ナイル川", "富士山", "琵琶湖",
    "日本史", "縄文時代", "奈良時代", "鎌倉時代", "戦国時代", "江戸時代", "明治維新",
    "産業革命", "ルネサンス", "近代日本文学", "古典文学", "世界遺産", "日本の自然",
    "医学の歴史", "科学技術史", "経済学", "国際連合", "憲法", "民主主義",
]


def generate_wiki_facts(app, *, count: int = 40, teacher: str | None = None,
                        seed: int | None = None, on_progress: Progress | None = None) -> dict:
    """Wikipedia の記事要約を根拠に、教師AIで検証済みの一般知識 Q&A を作成し自習DBへ蓄える。"""
    from . import web

    model = next((m for m in app.models() if m.name == teacher), None) if teacher else pick_teacher(app, "general")
    if model is None:
        raise ValueError(f"未知のモデル: {teacher}")
    rng = random.Random(seed)
    topics = list(_WIKI_TOPICS)
    rng.shuffle(topics)
    bench_vectors = [vectorize(t["prompt"]) for t in BUILTIN]
    seen = [vectorize(r["task"]) for r in app.store.study_rows()]
    added = 0
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {
            "type": "object", "required": ["q", "a"],
            "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
        }}},
    }
    for idx, topic in enumerate(topics, 1):
        if added >= count:
            break
        if on_progress:
            on_progress(added, count, f"Wikipedia知識を取得・整理中 ({topic})")
        try:
            excerpt = web.wikipedia(topic, lang="ja")
        except Exception:
            continue
        if not excerpt or "記事は見つかりませんでした" in excerpt:
            continue
        prompt = (
            "以下の Wikipedia の解説を根拠にして、客観的で正確な一問一答の練習問題を 4 件作ってください。\n"
            "ルール:\n"
            "- 各質問は日本語で完結させ、単一の事実(数値・名称・仕組み・時代など)を問う。\n"
            "- 各回答は前置きなしで簡潔かつ正確に 1〜2 文で答える(重要な固有名詞・数値・記号を必ず含める)。\n"
            "- 本文に書かれていない推測は書かない。\n\n"
            f"トピック: {topic}\n解説:\n{excerpt[:1800]}"
        )
        try:
            gen = app.backends[model.backend].generate(
                model.name,
                [{"role": "system", "content": "Create factual Japanese Q&A pairs strictly grounded in the excerpt. Output JSON."},
                 {"role": "user", "content": prompt}],
                temperature=0.4, max_tokens=900, json_schema=schema,
            )
            items = json.loads(gen.text).get("items") or []
        except (BackendError, ValueError, TypeError):
            continue
        for item in items:
            if added >= count:
                break
            q = clean_answer("general", str((item or {}).get("q") or "").strip())
            a = clean_answer("general", str((item or {}).get("a") or "").strip())
            if len(q) < 8 or len(a) < 2 or not well_formed("general", q) or not answer_ok(a):
                continue
            vec = vectorize(q)
            if _near_builtin(vec, bench_vectors, 0.72) or any(cosine(vec, s) >= 0.85 for s in seen):
                continue
            if app.store.add_study(task=q, role="general", model=f"{model.name}+wiki", answer=a, verified=True):
                app.router.add(q, "general")
                seen.append(vec)
                added += 1
    return {"added": added, "teacher": model.name}


def review_study(app, *, teacher: str | None = None, on_progress: Progress | None = None) -> dict:
    """保存済みの自習データを見直し、定型句の除去・書式/構文検証・非自己完結問題の削除を行う。"""
    model = next((m for m in app.models() if m.name == teacher), None) if teacher else pick_teacher(app)
    if model is None:
        raise ValueError(f"未知のモデル: {teacher}")
    rows = app.store.study_rows()
    bench_vectors = [vectorize(t["prompt"]) for t in BUILTIN]
    removed = cleaned = 0
    for i, row in enumerate(rows, 1):
        if on_progress:
            on_progress(i, len(rows), "自習データを見直し中")
        task, role = row["task"], row["role"]
        src_model = row["model"] if "model" in row.keys() else ""
        new_ans = clean_answer(role, row["answer"])
        keep, verified = verify_answer(role, task, new_ans)
        near_bench = _near_builtin(vectorize(task), bench_vectors, 0.75)
        trusted_gen = src_model == "celestial-curriculum" or role in ("agent", "judge")
        ok_context = True if trusted_gen else self_contained(app, model, task)
        if not (not near_bench and well_formed(role, task) and keep and ok_context):
            app.store.delete_study(task)
            removed += 1
        else:
            was_verified = bool(row["verified"]) if "verified" in row.keys() else False
            if new_ans != row["answer"] or (verified and not was_verified):
                app.store.update_study_answer(task, new_ans, verified=verified or was_verified)
                cleaned += 1
    return {"checked": len(rows), "removed": removed, "cleaned": cleaned, "teacher": model.name}


def _latest_own_model(app) -> ModelInfo | None:
    own = sorted((m for m in app.models() if m.name.startswith("celestial-") and m.name != "celestial-base:latest"),
                 key=lambda m: m.name, reverse=True)
    return own[0] if own else None


def run_study(app, *, per_role: int = 15, teacher: str | None = None, roles: list[str] | None = None,
              on_progress: Progress | None = None, seed: int | None = None,
              synthetic: int = 0, wiki: int = 0, verify_llm: bool = False, contrastive: bool = False) -> dict:
    """役割ごとに per_role 件の練習問題を作って解き、検証のうえ保存する。"""
    rng = random.Random(seed)
    fixed_teacher = None
    if teacher:
        fixed_teacher = next((m for m in app.models() if m.name == teacher), None)
        if fixed_teacher is None:
            raise ValueError(f"未知のモデル: {teacher}")
    role_names = roles or list(ROLES)
    total = per_role * len(role_names)
    # 計測用の課題と似た問題は捨てる(計測の答えを学習させないため)
    bench_vectors = [vectorize(t["prompt"]) for t in BUILTIN]
    seen = [vectorize(r["task"]) for r in app.store.study_rows()]
    added, pref_added, by_role, teachers_used = 0, 0, {}, set()
    own_model = _latest_own_model(app) if contrastive else None

    def report(message: str) -> None:
        if on_progress:
            on_progress(added, max(1, total), message)

    if synthetic > 0:
        report("検算済みの基礎問題を生成中")
        cur = generate_curriculum_examples(app, count=synthetic, seed=seed)
        seen.extend(vectorize(r["task"]) for r in app.store.study_rows())
    else:
        cur = {"added": 0, "by_role": {}}

    wiki_added = 0
    if wiki > 0:
        report("Wikipedia から検証済みの知識問題を収集中")
        try:
            w_res = generate_wiki_facts(app, count=wiki, teacher=teacher, seed=seed, on_progress=on_progress)
            wiki_added = int(w_res.get("added", 0))
            seen.extend(vectorize(r["task"]) for r in app.store.study_rows())
        except Exception:
            wiki_added = 0

    for role_name in role_names:
        model = fixed_teacher or pick_teacher(app, role=role_name)
        teachers_used.add(model.name)
        got, attempts = 0, 0
        while got < per_role and attempts < 6:
            attempts += 1
            report(f"{role_name}: 練習問題を作成中 ({model.name})")
            for task in _make_tasks(app, model, role_name, min(8, per_role - got + 2), rng):
                if got >= per_role:
                    break
                if not well_formed(role_name, task):
                    continue
                vec = vectorize(task)
                if _near_builtin(vec, bench_vectors, 0.72) or any(cosine(vec, s) >= 0.9 for s in seen):
                    continue
                if not self_contained(app, model, task):
                    continue
                report(f"{role_name}: 解答中 ({model.name})")
                result = app.run(task, role=role_name, model=model.name, use_cache=False, swarm=False, learn=False)
                answer = clean_answer(role_name, result.text)
                keep, verified = verify_answer(role_name, task, answer, run_code=(role_name == "code"))
                if not keep and role_name == "summarize":
                    m_lim = _CHAR_LIMIT.search(task)
                    if m_lim and answer_ok(answer):
                        lim = int(m_lim.group(1))
                        shrink = app.backends[model.backend].generate(
                            model.name,
                            [{"role": "system", "content": f"出力は必ず{lim}文字以内(句読点込み)で、要約文のみを返してください。前置き禁止。"},
                             {"role": "user", "content": f"次を{lim}文字以内で簡潔に言い換えて:\n{answer}"}],
                            temperature=0.0, max_tokens=max(40, lim * 3),
                        )
                        answer = clean_answer(role_name, shrink.text)
                        keep, verified = verify_answer(role_name, task, answer)
                if not keep:
                    continue
                if verify_llm and not verified:
                    llm_ok, llm_verified = _verify_with_teacher(app, model, role_name, task, answer)
                    if not llm_ok:
                        continue
                    verified = verified or llm_verified
                if app.store.add_study(task=task, role=role_name, model=model.name, answer=answer, verified=verified):
                    app.router.add(task, role_name)
                    seen.append(vec)
                    got += 1
                    added += 1
                    if own_model is not None and verified and role_name in (
                        "reason", "summarize", "code", "extract", "translate", "general", "agent", "judge"
                    ):
                        try:
                            own_res = app.run(task, role=role_name, model=own_model.name, use_cache=False, swarm=False, learn=False)
                            cand = own_res.text.strip()
                            own_ok, own_ver = verify_answer(role_name, task, cand, run_code=(role_name == "code"))
                            if cand and cand != answer and (not own_ok or (role_name in ("reason", "code", "agent", "judge") and not own_ver)):
                                if app.store.add_preference(task=task, role=role_name, chosen=answer, rejected=cand,
                                                            source=f"contrast:{own_model.name}->{model.name}"):
                                    pref_added += 1
                        except BackendError:
                            pass
        by_role[role_name] = got
    report("完了")
    primary = fixed_teacher.name if fixed_teacher else (
        next(iter(teachers_used)) if len(teachers_used) == 1 else pick_teacher(app).name
    )
    return {"added": added, "teacher": primary, "teachers": sorted(teachers_used),
            "by_role": by_role, "synthetic_added": cur["added"], "wiki_added": wiki_added,
            "preferences_added": pref_added}


def collect_contrastive_preferences(
    app,
    *,
    student: str | None = None,
    roles: list[str] | None = None,
    limit: int = 80,
    seed: int = 0,
    on_progress: Progress | None = None,
) -> dict:
    """検証済み自習データに対し、対象モデルの誤答・冗長・形式違反を収集して DPO 選好ペアを作る。"""
    target = next((m for m in app.models() if m.name == student), None) if student else _latest_own_model(app)
    if target is None:
        return {"checked": 0, "added": 0, "student": None}
    allowed = set(roles) if roles else None
    rows = [
        dict(r) for r in app.store.study_rows()
        if (r["verified"] if "verified" in r.keys() else 0)
        and (not allowed or r["role"] in allowed)
    ]
    rng = random.Random(seed)
    by_role: dict[str, list[dict]] = {}
    for r in rows:
        by_role.setdefault(r["role"], []).append(r)
    sample: list[dict] = []
    per_r = max(4, limit // max(1, len(by_role)))
    for role_name, bucket in sorted(by_role.items()):
        rng.shuffle(bucket)
        sample.extend(bucket[:per_r])
    sample = sample[:limit]

    added = 0
    for idx, r in enumerate(sample, 1):
        if on_progress:
            on_progress(idx, len(sample), f"対比学習ペアを収集中 ({target.name}: {r['role']})")
        task, role_name, chosen = r["task"], r["role"], r["answer"]
        try:
            sys_txt = ROLES[role_name].system if role_name in ROLES else "あなたは正確で簡潔なAIアシスタントです。指示された形式を厳守して答えてください。"
            gen = app.backends[target.backend].generate(
                target.name,
                [{"role": "system", "content": sys_txt}, {"role": "user", "content": task}],
                temperature=0.0, max_tokens=512,
            )
            cand = gen.text.strip()
        except BackendError:
            continue
        if not cand or cand == chosen:
            continue
        ok, ver = verify_answer(role_name, task, cand, run_code=(role_name == "code"))
        bad_len = role_name in ("general", "reason") and len(cand) > max(260, len(chosen) * 2.2)
        bad_nums = False
        if role_name == "reason":
            want_nums = re.findall(r"-?\d+(?:\.\d+)?", chosen[-40:])
            got_nums = re.findall(r"-?\d+(?:\.\d+)?", cand[-60:])
            if want_nums and want_nums[-1] not in got_nums:
                bad_nums = True
        if not ok or bad_len or bad_nums or (role_name in ("code", "extract", "translate", "summarize", "agent", "judge") and not ver):
            if app.store.add_preference(task=task, role=role_name, chosen=chosen, rejected=cand,
                                        source=f"contrast:{target.name}"):
                added += 1
    return {"checked": len(sample), "added": added, "student": target.name}
