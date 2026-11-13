"""過去の成功例を手本(few-shot)として呼び出す経験記憶。

埋め込みモデルがあれば意味の近さで探す(言い回しが違っても同じ趣旨の例を拾える)。
無ければ文字 n-gram の近さで探す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..textsim import cosine, vectorize
from .embed import Embedder, dot


@dataclass
class Example:
    id: int
    task: str
    answer: str
    vec: dict[str, float]
    emb: list[float] | None = None


class Memory:
    def __init__(self, max_per_role: int = 3000, embedder: Embedder | None = None,
                 semantic: Callable[[], bool] = lambda: True, min_sim_semantic: float = 0.65):
        self.max_per_role = max_per_role
        self.embedder = embedder
        self.semantic = semantic  # 設定で意味検索が有効か
        self.min_sim_semantic = min_sim_semantic
        self.last_mode = "ngram"  # 直近の検索がどちらで行われたか(表示・テスト用)
        self._by_role: dict[str, dict[int, Example]] = {}

    def add(self, id: int, role: str, task: str, answer: str) -> None:
        bucket = self._by_role.setdefault(role, {})
        bucket[id] = Example(id, task, answer, vectorize(task))
        if len(bucket) > self.max_per_role:
            del bucket[next(iter(bucket))]

    def remove(self, id: int) -> None:
        for bucket in self._by_role.values():
            bucket.pop(id, None)

    def search(self, role: str, task: str, k: int, min_sim: float) -> list[Example]:
        examples = list(self._by_role.get(role, {}).values())
        if k <= 0 or not examples:
            return []
        scored = self._semantic_scores(task, examples)
        if scored is None:
            self.last_mode = "ngram"
            vec = vectorize(task)
            scored = [(cosine(vec, ex.vec), ex) for ex in examples]
            threshold = min_sim
        else:
            self.last_mode = "semantic"
            threshold = self.min_sim_semantic
        scored = [(s, ex) for s, ex in scored if s >= threshold]
        scored.sort(key=lambda p: p[0], reverse=True)
        return [ex for _, ex in scored[:k]]

    def _semantic_scores(self, task: str, examples: list[Example]) -> list[tuple[float, Example]] | None:
        if self.embedder is None or not self.semantic():
            return None
        pending = [ex for ex in examples if ex.emb is None]
        vectors = self.embedder.vectors([task] + [ex.task for ex in pending])
        if vectors is None:
            return None
        for ex, emb in zip(pending, vectors[1:]):
            ex.emb = emb
        query = vectors[0]
        return [(dot(query, ex.emb), ex) for ex in examples]
