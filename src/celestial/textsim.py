"""文字n-gramによる軽量なテキスト類似度。分かち書き不要なので日本語でもそのまま使える。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip().lower()


def task_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def ngrams(text: str, sizes: tuple[int, ...] = (2, 3)) -> list[str]:
    t = normalize(text)
    grams = [t[i : i + n] for n in sizes for i in range(len(t) - n + 1)]
    return grams or ([t] if t else [])


def vectorize(text: str) -> dict[str, float]:
    counts = Counter(ngrams(text))
    norm = math.sqrt(sum(c * c for c in counts.values())) or 1.0
    return {g: c / norm for g, c in counts.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(g, 0.0) for g, v in a.items())
