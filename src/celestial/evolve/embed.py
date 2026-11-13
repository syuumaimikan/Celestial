"""意味ベースの類似検索のための埋め込み(文章 → ベクトル)。

埋め込みモデルが使えるときだけ働き、無ければ None を返して呼び出し側が文字 n-gram に切り替える。
一度計算したベクトルは SQLite に保存し、同じ文章を二度と計算しない。
"""

from __future__ import annotations

import math
import time

from ..backends import BackendError
from ..textsim import task_hash

if hasattr(math, "sumprod"):
    dot = math.sumprod
else:  # Python 3.11
    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))


class Embedder:
    def __init__(self, backends: dict, store, model: str = "auto"):
        self.backends = backends
        self.store = store
        self.wanted = model
        self._resolved: tuple[object, str] | None = None
        self._retry_at = 0.0  # 失敗したらしばらく試さない(毎回タイムアウトを待たないため)

    def resolve(self) -> tuple[object, str] | None:
        """(バックエンド, モデル名)。使える埋め込みモデルが無ければ None。"""
        if self.wanted == "off":
            return None
        if self._resolved is None and time.monotonic() >= self._retry_at:
            for backend in self.backends.values():
                try:
                    names = backend.embedding_models()
                except BackendError:
                    continue
                pick = next((n for n in names if self.wanted in ("auto", n, n.removesuffix(":latest"))), None)
                if pick:
                    self._resolved = (backend, pick)
                    break
            else:
                self._retry_at = time.monotonic() + 60
        return self._resolved

    @property
    def model(self) -> str | None:
        found = self.resolve()
        return found[1] if found else None

    def vectors(self, texts: list[str]) -> list[list[float]] | None:
        """各文章の単位ベクトル。埋め込みが使えなければ None。"""
        found = self.resolve()
        if found is None:
            return None
        backend, model = found
        hashes = [task_hash(t) for t in texts]
        known = self.store.get_embeddings(hashes, model)
        missing = [(h, t) for h, t in dict(zip(hashes, texts)).items() if h not in known]
        if missing:
            try:
                fresh = backend.embed(model, [t for _, t in missing])
            except BackendError:
                self._resolved, self._retry_at = None, time.monotonic() + 60
                return None
            fresh = [_unit(v) for v in fresh]
            known.update({h: v for (h, _), v in zip(missing, fresh)})
            self.store.put_embeddings([(h, v) for (h, _), v in zip(missing, fresh)], model)
        return [known[h] for h in hashes]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(dot(vector, vector)) or 1.0
    return [x / norm for x in vector]
