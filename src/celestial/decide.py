"""判断(Jev のような System 1): 文章を作らずに、決まった型の答え(選択肢・はい/いいえ)を、確率つきで返す。

ローカルのAIに、選択肢の記号(A, B, C…)を1語だけ答えさせ、その1語目の確率の分布(Ollama の top_logprobs)から
選択肢ごとの確率を出す。生成は1語なので速い(読み込み済みのAIなら 0.1〜0.3 秒)。

較正: AIの確率は、そのままだと自信過剰になりやすい。正解の分かっている判断(精度の計測の課題の担当など)で
温度 T を学び、p_i ∝ exp(log p_i / T) と直す(温度スケーリング)。T > 1 で確率がならされ、T < 1 で尖る。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

LETTERS = "ABCDEFGHIJKL"
_JA = re.compile(r"[぀-ヿ一-鿿]")


@dataclass
class Decision:
    answer: str                 # 選んだ選択肢
    index: int                  # その番号
    probability: float          # その確率(較正したもの)
    distribution: dict[str, float] = field(default_factory=dict)  # 選択肢ごとの確率
    model: str = ""
    latency_s: float = 0.0
    calibrated: bool = False    # 確率が分布から出たものか(False なら、生成された記号だけから決めた)

    def to_dict(self) -> dict:
        return {"answer": self.answer, "index": self.index, "probability": round(self.probability, 4),
                "distribution": {k: round(v, 4) for k, v in self.distribution.items()}, "model": self.model,
                "latency_s": round(self.latency_s, 3), "calibrated": self.calibrated}


# 較正と、判断に使うAIを選ぶための課題(正解つき)。文章の意味を読み取る判断を、種類を散らして集めた。
# (担当の振り分けだけで選ぶと、「本人の話か」の判断を半分も当てられないAIが選ばれた)
_REPLY = "このメールは返信が必要ですか?"
_URGENT, _URGENT_OPTS = "この問い合わせの緊急度は?", ["至急", "普通", "低い"]
_FEEL, _FEEL_OPTS = "この文の感情は?", ["前向き", "否定的", "中立"]
_SELF = "この発言には、今後の会話でも役立つ、利用者自身についての事実(名前・好み・習慣・仕事・家族・住んでいる所・予定など)が含まれていますか?"
_COMPLAINT = "この文章は苦情ですか?"
_TOPIC, _TOPIC_OPTS = "この相談の分野は?", ["仕事", "健康", "お金", "旅行"]
_SPAM = "このメッセージは迷惑メール(詐欺や広告)ですか?"
DECISION_TASKS: list[tuple[str, list[str] | None, str, int]] = [
    (_REPLY, None, "明日の会議の時間を教えてください。", 0),
    (_REPLY, None, "本日のメンテナンスは完了しました。返信は不要です。", 1),
    (_REPLY, None, "見積書を送付しますので、ご確認のうえ可否をお知らせください。", 0),
    (_REPLY, None, "【自動送信】ご注文ありがとうございました。発送は3日以内です。", 1),
    (_URGENT, _URGENT_OPTS, "本番のサーバーが止まっていて、お客様がログインできません。", 0),
    (_URGENT, _URGENT_OPTS, "来月の勉強会の資料を、いつか共有してもらえますか。", 2),
    (_URGENT, _URGENT_OPTS, "請求書の宛名を、今週中に直していただけますか。", 1),
    (_URGENT, _URGENT_OPTS, "支払いの画面でエラーが出て、決済が一件も通りません。", 0),
    (_FEEL, _FEEL_OPTS, "新しいカフェ、思ったより美味しくて最高だった", 0),
    (_FEEL, _FEEL_OPTS, "電車が1時間も遅れて、大事な面接に遅刻した", 1),
    (_FEEL, _FEEL_OPTS, "明日の天気は晴れのち曇りの予報です", 2),
    (_FEEL, _FEEL_OPTS, "頼んだ料理がいつまでも来なくて、もう二度と行かない", 1),
    (_SELF, None, "私はコーヒーはいつもブラックで飲みます。おすすめの豆を教えて", 0),
    (_SELF, None, "私は来月から大阪に引っ越すので、引っ越しの手続きの一覧を作って", 0),
    (_SELF, None, "うちの娘は小学3年生です。算数の問題を作って", 0),
    (_SELF, None, "僕はエンジニアで、主にPythonを書いています。おすすめの本は?", 0),
    (_SELF, None, "光の速さは秒速何km?", 1),
    (_SELF, None, "この文章を要約して: 近年、AIの発展は目覚ましい。", 1),
    (_SELF, None, "私の質問に短く答えて。日本の首都は?", 1),
    (_SELF, None, "Pythonでリストを並べ替える方法は?", 1),
    (_COMPLAINT, None, "注文した商品がまだ届きません。どうなっていますか。", 0),
    (_COMPLAINT, None, "いつも丁寧な対応をありがとうございます。", 1),
    (_COMPLAINT, None, "届いた商品が壊れていました。交換してください。", 0),
    (_COMPLAINT, None, "新商品の発売日を教えてください。", 1),
    (_TOPIC, _TOPIC_OPTS, "上司との関係がうまくいかず、転職を考えています", 0),
    (_TOPIC, _TOPIC_OPTS, "最近よく眠れず、朝起きると頭が痛い", 1),
    (_TOPIC, _TOPIC_OPTS, "老後に向けて、毎月いくら積み立てればいい?", 2),
    (_TOPIC, _TOPIC_OPTS, "秋に京都へ行くなら、どこを回るのがいい?", 3),
    (_SPAM, None, "おめでとうございます!当選しました。賞金を受け取るには口座番号を入力してください", 0),
    (_SPAM, None, "来週の打ち合わせの議事録を共有します。ご確認ください。", 1),
]


def default_options(question: str) -> list[str]:
    return ["はい", "いいえ"] if _JA.search(question) else ["yes", "no"]


def _user_turn(question: str, options: list[str], context: str, ja: bool) -> str:
    lines = "\n".join(f"{LETTERS[i]}) {option}" for i, option in enumerate(options))
    if ja:
        return (f"情報:\n{context.strip()}\n\n" if context.strip() else "") + f"質問: {question.strip()}\n選択肢:\n{lines}\n答え(記号1文字):"
    return (f"Information:\n{context.strip()}\n\n" if context.strip() else "") + f"Question: {question.strip()}\nOptions:\n{lines}\nAnswer (one letter):"


def build_messages(question: str, options: list[str], context: str = "", examples: list[tuple[str, int]] | None = None) -> list[dict]:
    """判断を頼む会話。examples([(材料, 正解の番号)])を渡すと、例を会話の形で先に見せる(小さいAIの判断がそろう)。"""
    ja = bool(_JA.search(question + "".join(options) + context))
    system = ("You make one decision. Read the information and the question, then answer with exactly one letter: "
              "the best option. Output only that letter.")
    messages = [{"role": "system", "content": system}]
    for example, answer in examples or []:
        messages += [{"role": "user", "content": _user_turn(question, options, example, ja)}, {"role": "assistant", "content": LETTERS[answer]}]
    return messages + [{"role": "user", "content": _user_turn(question, options, context, ja)}]


def option_distribution(alternatives: dict[str, float], options: list[str]) -> list[float] | None:
    """1語目の候補 {語: 対数確率} を、選択肢ごとの確率にする。記号(A / (A / A))と、選択肢の言葉の書き出しの両方を数える。"""
    mass = [0.0] * len(options)
    for token, logprob in alternatives.items():
        bare = token.strip().strip("()（）[]【】.:：、, ")
        index = None
        if len(bare) == 1 and bare.upper() in LETTERS[:len(options)]:
            index = LETTERS.index(bare.upper())
        elif len(bare) >= 1:
            starts = [i for i, option in enumerate(options) if option.lower().startswith(bare.lower())]
            if len(starts) == 1:  # 書き出しが1つの選択肢にだけ当てはまるなら、その選択肢として数える
                index = starts[0]
        if index is not None:
            mass[index] += math.exp(logprob)
    total = sum(mass)
    return [m / total for m in mass] if total > 0 else None


def parse_answer(text: str, options: list[str]) -> int | None:
    """確率の分布が取れないとき、生成された文字から選んだ選択肢を読む。"""
    bare = text.strip().strip("()（）[].:： ")
    if bare[:1].upper() in LETTERS[:len(options)] and (len(bare) == 1 or not bare[1:2].isalpha()):
        return LETTERS.index(bare[:1].upper())
    for i, option in enumerate(options):
        if bare.lower().startswith(option.lower()):
            return i
    return None


def calibrate(probs: list[float], temperature: float) -> list[float]:
    if abs(temperature - 1.0) < 1e-9:
        return list(probs)
    logs = [math.log(max(p, 1e-12)) / temperature for p in probs]
    top = max(logs)
    exps = [math.exp(v - top) for v in logs]
    total = sum(exps)
    return [e / total for e in exps]


def nll(samples: list[tuple[list[float], int]], temperature: float) -> float:
    return -sum(math.log(max(calibrate(p, temperature)[y], 1e-12)) for p, y in samples) / max(1, len(samples))


def fit_temperature(samples: list[tuple[list[float], int]]) -> float:
    """正解の分かっている判断 [(確率の並び, 正解の番号)] から、負の対数尤度が最も小さくなる温度(0.25〜約10)を探す。"""
    if not samples:
        return 1.0
    candidates = [0.25 * 1.1 ** k for k in range(40)]
    return min(candidates, key=lambda t: nll(samples, t))


def calibration_error(samples: list[tuple[list[float], int]], temperature: float = 1.0, bins: int = 10) -> float:
    """期待較正誤差(ECE): 「確率 p と言ったものが、実際に p の割合で当たっているか」のずれの平均(0 が理想)。"""
    if not samples:
        return 0.0
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probs, y in samples:
        p = calibrate(probs, temperature)
        best = max(range(len(p)), key=p.__getitem__)
        buckets[min(bins - 1, int(p[best] * bins))].append((p[best], best == y))
    total = len(samples)
    return sum(len(b) / total * abs(sum(c for c, _ in b) / len(b) - sum(ok for _, ok in b) / len(b)) for b in buckets if b)
