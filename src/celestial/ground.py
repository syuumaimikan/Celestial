"""事実の質問の裏付け: 回答に自信がないときだけ、Wikipedia の公式 API で関連する記述を探して添える。

小さいAIは事実をもっともらしく言い間違える(「日本一長い川は長良川」など)。知らないことは推論では
直せないので、資料を見せて答え直させる。ネットの利用を許可したとき(tools_net)だけ使い、
自信のある回答では問い合わせない(速さと、外へ出す情報を最小にするため)。
Wikipedia の利用回数の制限(HTTP 429)に当たったら、しばらく問い合わせを控える。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse

from .web import WebError, _get

_COOLDOWN_S = 180.0
_blocked_until = 0.0
_lock = threading.Lock()

_FACT_CUE = re.compile(
    r"[?？]|とは|って何|は何|何[がをでのはと]|誰|だれ|いつ|どこ|どれ|どの|いくつ|何年|何人|何本|何個|何回|何度|何メートル|何キロ|"
    r"を挙げて|を教えて|を知りたい|"
    r"\b(what|who|whom|when|where|which|how many|how much|how tall|how long|how old)\b", re.I)
_NOT_FACT = re.compile(
    r"書いて|作って|考えて|計算|翻訳|訳して|要約|まとめ|コード|プログラム|方法|やり方|手順|書き方|使い方|作り方|直し方|選び方|仕方|コツ|"
    r"おすすめ|オススメ|どう思|感想|意見|"
    r"比較|違い|理由|なぜ|どうして|\d\s*[-+*/×÷^]\s*\d|\b(write|create|translate|summar|calculate|code|why|how to)\b", re.I)
_TRAILING = re.compile(r"(について)?(を)?(教えて(ください|下さい)?|挙げて(ください|下さい)?|知りたい(です)?|ですか|でしょうか|ますか|か|は|って|とは)?"
                       r"[\s?？。!！]*$")
_PARTICLE = re.compile(r"(?<=[一-鿿゠-ヿA-Za-z0-9])[のはがをにでとへも](?=[一-鿿゠-ヿA-Za-z0-9])")
_KANA = re.compile(r"[぀-ヿ一-鿿]")


def looks_factual(task: str) -> bool:
    """短い事実の質問か(「日本一長い川は?」など)。作文・計算・意見・手順の依頼は除く。"""
    text = task.strip()
    if not text or len(text) > 80 or "\n" in text:
        return False
    return bool(_FACT_CUE.search(text)) and not _NOT_FACT.search(text)


_SUPERLATIVE = {"大きい": "最大", "広い": "最大", "長い": "最長", "高い": "最高", "小さい": "最小", "狭い": "最小", "深い": "最深",
                "古い": "最古", "新しい": "最新", "多い": "最多", "重い": "最重", "速い": "最速", "早い": "最速", "短い": "最短",
                "低い": "最低", "遠い": "最遠", "暑い": "最高気温", "寒い": "最低気温"}
_SUPERLATIVE_RE = re.compile(r"(一番|いちばん|最も)(" + "|".join(_SUPERLATIVE) + r")")
_PREFIX = re.compile(r"(初代|第\d+代|歴代|最初の|最後の)(?=[一-鿿゠-ヿ])")
_PARTICLE_END = re.compile(r"(?<=[一-鿿゠-ヿA-Za-z0-9])[のはがをにでとへも](?=\s|$)")
_FILLER = re.compile(r"およそ|おおよそ|だいたい|大体|約|ぐらい|くらい|何[本個人年回度台枚匹頭羽件歳]|いくつ|どのくらい|どれくらい|何|誰|だれ|いつ|どこ|どれ|どの")


def search_terms(question: str) -> str:
    """質問文を検索語にする。百科事典の言い回しに寄せると、答えの載った箇所に当たりやすい。

    問いかけの語尾を落とし、「一番大きい」を「最大」のように言い換え、「初代」などを切り離し、助詞で区切る。
    """
    q = question.strip()
    for _ in range(3):
        q = _TRAILING.sub("", q)
    q = _FILLER.sub(" ", q)
    q = _SUPERLATIVE_RE.sub(lambda m: " " + _SUPERLATIVE[m.group(2)] + " ", q)
    q = _PREFIX.sub(lambda m: m.group(1).removesuffix("の") + " ", q)
    q = _PARTICLE.sub(" ", q)
    q = _PARTICLE_END.sub("", q)
    q = re.sub(r"(一番|いちばん)", "最も", q)  # 助詞で区切った後に置き換える(「最も」の「も」を区切らない)
    return re.sub(r"\s+", " ", q).strip() or question.strip()


def _clean(snippet: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet)).strip()


def lookup(question: str, limit: int = 3) -> list[dict]:
    """関連する Wikipedia の項目を [{title, url, text}] で返す。見つからない・使えないときは空。"""
    global _blocked_until
    with _lock:
        if time.monotonic() < _blocked_until:
            return []
    lang = "ja" if _KANA.search(question) else "en"
    natural = question.strip()
    for _ in range(3):
        natural = _TRAILING.sub("", natural)
    # 1回の問い合わせで2通りに探す: 質問そのまま(項目の概要を取る)と、百科事典風の検索語(本文の該当箇所を取る)
    params = {"action": "query", "format": "json", "formatversion": 2, "redirects": 1,
              "list": "search", "srsearch": search_terms(question), "srlimit": limit, "srprop": "snippet",
              "generator": "search", "gsrsearch": natural or question, "gsrlimit": limit,
              "prop": "extracts", "exintro": 1, "explaintext": 1, "exchars": 600, "exlimit": limit}
    try:
        body, _ = _get(f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params), timeout=8)
        data = json.loads(body).get("query") or {}
    except WebError as e:
        if "429" in str(e):  # 利用回数の制限。しばらく控える
            with _lock:
                _blocked_until = time.monotonic() + _COOLDOWN_S
        return []
    except ValueError:
        return []
    snippets = {hit.get("title"): _clean(hit.get("snippet") or "") for hit in data.get("search") or []}
    pages = sorted(data.get("pages") or [], key=lambda p: p.get("index", 99))
    texts: dict[str, str] = {}
    for page in pages[:limit]:
        title = page.get("title") or ""
        texts[title] = re.sub(r"\s+", " ", page.get("extract") or "").strip()
    for title, snippet in snippets.items():
        # 検索語で当たった本文の箇所は、質問への答えが載っていることが多いので、概要より前に置く
        # (後ろに置くと、小さいAIは概要だけを読んで答えを見落とす。「日本一長い川は?」で「信濃川 利根川…」の並びを見落とした)
        if snippet and snippet[:40] not in texts.get(title, ""):
            texts[title] = snippet + (" … " + texts[title] if texts.get(title) else "")
    return [{"title": title, "url": f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
             "text": ranking_note(title, text) + text[:900]} for title, text in texts.items() if title and text][: limit * 2]


_RANKING = re.compile(r"^(?P<key>.+?)順の(?P<subject>.+?)一覧")


def ranking_note(title: str, text: str) -> str:
    """「長さ順の日本の川一覧」のような順位の一覧は、本文の頭が名前の並び(信濃川 利根川 …)だけで、
    どれが1位かが書かれていない。小さいAIはこれを読み取れない(「日本一長い川は?」に、ほかの項目にあった
    「ミシシッピ川」と答えた)ので、1位と2位をはっきり書き添える。"""
    found = _RANKING.match(title)
    if not found:
        return ""
    head = text.split(title, 1)[0].split()
    if len(head) < 2 or any(len(name) > 20 for name in head[:2]):
        return ""
    return f"(この一覧は{found.group('key')}の順。1位は{head[0]}、2位は{head[1]})"


def context_prompt(materials: list[dict], draft: str = "") -> str:
    """回答役のAIに添える参考資料の指示。

    draft(先に書いた答え)を渡すと「資料と食い違うときだけ直す」形にする。検索で関係のない項目しか
    見つからなかったとき(「骨は何本?」で「ネコ」「脚」の項目が出るなど)に、正しかった答えまで
    資料に引きずられて崩れるのを防ぐため。
    """
    lines = "\n".join(f"- {m['title']}: {m['text']}" for m in materials)
    if draft.strip():
        return ("\n\n下に参考資料(Wikipedia の抜粋)と、あなたが先に書いた答えがある。資料に、先の答えと食い違う記述や、"
                "質問への直接の答えがあれば、資料に従って答え直す。資料が質問と関係なければ、先の答えをそのまま答える。"
                "利用者への回答だけを簡潔に書き、資料や先の答えのことには触れない。\n"
                f"先に書いた答え: {draft.strip()[:500]}\n参考資料:\n" + lines)
    return ("\n\n参考資料(Wikipedia の抜粋)。質問に直接答える記述があれば、それに従って答える。"
            "質問と関係のない資料は無視して、自分の知識で答える。資料のことには触れず、簡潔に答える。\n" + lines)
