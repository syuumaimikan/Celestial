"""管理AIの振り分けを真似て学習するルーター。

十分に学習すると管理AI(LLM)を呼ばずに振り分けられるようになり、その分だけ速く・省エネになる。
文字n-gramの多項ナイーブベイズなので学習も推論も一瞬で終わる。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from ..textsim import ngrams


class LearnedRouter:
    def __init__(self, min_examples: int = 12, min_per_role: int = 3):
        self.min_examples = min_examples
        self.min_per_role = min_per_role
        self._counts: dict[str, Counter] = defaultdict(Counter)
        self._totals: Counter = Counter()
        self._docs: Counter = Counter()
        self._vocab: set[str] = set()

    @property
    def examples(self) -> int:
        return sum(self._docs.values())

    def add(self, task: str, role: str) -> None:
        grams = ngrams(task)
        self._counts[role].update(grams)
        self._totals[role] += len(grams)
        self._docs[role] += 1
        self._vocab.update(grams)

    def predict(self, task: str) -> tuple[str | None, float]:
        """(役割, 確信度)。まだ判断材料が足りないときは (None, 0.0)。"""
        n_docs = self.examples
        if n_docs < self.min_examples or len(self._docs) < 2:
            return None, 0.0
        grams = Counter(g for g in ngrams(task) if g in self._vocab)
        n = sum(grams.values())
        if n == 0:
            return None, 0.0
        vocab_size = len(self._vocab)
        # 長文で確信度が過剰にならないよう、実効的なn-gram数に上限を設ける
        scale = min(n, 40) / n
        scores: dict[str, float] = {}
        for role, docs in self._docs.items():
            counts, total = self._counts[role], self._totals[role]
            ll = sum(c * math.log((counts[g] + 0.5) / (total + 0.5 * vocab_size)) for g, c in grams.items())
            scores[role] = math.log(docs / n_docs) + ll * scale
        best = max(scores, key=scores.__getitem__)
        if self._docs[best] < self.min_per_role:
            return None, 0.0
        top = scores[best]
        conf = 1.0 / sum(math.exp(s - top) for s in scores.values())
        return best, conf
