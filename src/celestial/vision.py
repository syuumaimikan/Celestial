"""画像の認識: カメラの映像・PC の画面・写真を、画像を扱えるAIで読み取る(説明・文字・物の数・翻訳・表・問題・コード・質問)。

画面(web の「見る」・スマホ)から、1枚ずつ送られてくる画像を処理する。ライブでは数秒おきに最新の1枚だけを送る。
モードごとに、AIへの指示と出力の形(物の数は JSON で受け取って一覧にする)を決める。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_IMAGE_BYTES = 8_000_000


@dataclass(frozen=True)
class Mode:
    label: str
    instruction: str
    max_tokens: int = 700
    schema: dict | None = None
    needs_question: bool = False


_OBJECTS_SCHEMA = {
    "type": "object", "required": ["objects", "summary"],
    "properties": {
        "objects": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "required": ["name", "count"],
            "properties": {"name": {"type": "string"}, "count": {"type": "integer", "minimum": 0}}}},
        "summary": {"type": "string"},
    },
}

MODES: dict[str, Mode] = {
    "describe": Mode("説明", "Describe what is in the image: the main subjects, the scene, notable details and any visible text. "
                             "Be concrete and accurate. Use a short paragraph and a few bullet points."),
    "ocr": Mode("文字を読む", "Transcribe all text in the image exactly as written, keeping the line breaks and reading order. "
                            "Do not translate or explain. If there is no text, say so in one short sentence.", 1200),
    "objects": Mode("物を数える", "List the distinct kinds of objects you can see and count each kind carefully. "
                                "Use short names. summary: one sentence about the scene.", 500, _OBJECTS_SCHEMA),
    "translate": Mode("翻訳", "Read all text in the image, then translate it. If the text is Japanese, translate it into English; "
                            "otherwise translate it into Japanese. Show the original and the translation line by line.", 1200),
    "table": Mode("表にする", "Extract the table or list data in the image as a Markdown table. Keep the numbers exactly. "
                           "If there is no table, extract the key information as a Markdown table anyway.", 1500),
    "solve": Mode("問題を解く", "Solve the problem or question shown in the image. Show the steps briefly and give the final answer clearly.", 1500),
    "code": Mode("コードを読む", "Extract the program code in the image exactly into one Markdown code block with the language name. "
                              "Then point out any errors you can see and how to fix them.", 2000),
    "ask": Mode("質問", "Answer the user's question about the image accurately and concisely. If the image does not show the "
                       "answer, say what you can see instead.", 900, needs_question=True),
    "assist": Mode("周りの様子", "Describe the surroundings for a person who can not see well: what is ahead, people, obstacles, "
                               "steps, signs and their text, in two or three short sentences. Mention anything dangerous first.", 300),
    "watch": Mode("見張り", "This is a frame from a camera that watches a place. In one or two short sentences, say what is happening "
                           "now: people, animals, vehicles, movements, or anything unusual. If nothing notable, say 「特に変化なし」.", 160),
}


def prompt(mode: str, question: str = "", lang: str = "ja") -> tuple[list[dict], Mode]:
    """モードに合わせた、AIへの指示(メッセージの列)を返す。"""
    spec = MODES.get(mode)
    if spec is None:
        raise ValueError(f"未知のモードです: {mode}(選択肢: {', '.join(MODES)})")
    question = (question or "").strip()
    if spec.needs_question and not question:
        raise ValueError("質問を入力してください")
    language = "Japanese" if lang.startswith("ja") else "English"
    system = (f"You are Celestial's vision assistant. {spec.instruction} "
              f"Write in natural {language}" + (" (except the transcribed or original text)" if mode in ("ocr", "translate", "code") else "")
              + ". Never invent things that are not visible.")
    user = question if question else {"describe": "この画像を説明して", "ocr": "画像の文字を書き出して", "objects": "写っている物を数えて",
                                       "translate": "画像の文字を翻訳して", "table": "表にして", "solve": "この問題を解いて",
                                       "code": "このコードを書き出して", "assist": "周りの様子を教えて", "watch": "今の様子は?"}.get(mode, "")
    if question and mode != "ask":
        user = f"{user}\n(補足: {question})"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], spec


def parse(mode: str, text: str) -> dict:
    """AIの出力を、画面に出す形にする。物を数えるモードは {objects: [{name, count}], text}。"""
    text = (text or "").strip()
    if mode != "objects":
        return {"text": text}
    try:
        data = json.loads(text)
    except ValueError:
        return {"text": text, "objects": []}
    objects = []
    for item in data.get("objects") or []:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            try:
                count = max(0, int(item.get("count", 0)))
            except (TypeError, ValueError):
                continue
            objects.append({"name": str(item["name"]).strip()[:40], "count": count})
    summary = str(data.get("summary") or "").strip()
    lines = [summary] if summary else []
    lines += [f"- {o['name']}: {o['count']}" for o in objects]
    return {"text": "\n".join(lines) or "見つかりませんでした", "objects": objects, "summary": summary}


def decode_image(data: str) -> bytes:
    """画面から届いた画像(base64、data: で始まる形も可)を取り出す。"""
    import base64

    raw = re.sub(r"^data:image/[\w.+-]+;base64,", "", (data or "").strip())
    try:
        image = base64.b64decode(raw, validate=False)
    except ValueError as e:
        raise ValueError("画像を読み取れませんでした") from e
    if not image:
        raise ValueError("画像がありません")
    if len(image) > MAX_IMAGE_BYTES:
        raise ValueError("画像が大きすぎます(8MB まで)")
    if not (image.startswith(b"\x89PNG") or image.startswith(b"\xff\xd8") or image[:4] == b"RIFF" or image[:6] in (b"GIF87a", b"GIF89a")):
        raise ValueError("PNG・JPEG・WebP・GIF の画像を送ってください")
    return image
