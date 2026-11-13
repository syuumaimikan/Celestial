from __future__ import annotations

from typing import Callable

from .base import Backend, Generation, Message, ModelInfo, register

Responder = Callable[[str, list[Message], dict | None], str]


@register("mock")
class MockBackend(Backend):
    """テスト用、かつ自作バックエンドを書くときの最小の見本。"""

    def __init__(self, name: str = "mock", responder: Responder | None = None, models: list[ModelInfo] | None = None,
                 embedder: Callable[[str], list[float]] | None = None,
                 logprob: Callable[[str, str], float] | None = None, **options):
        super().__init__(name, **options)
        self.responder = responder or (lambda model, messages, schema: f"[{model}] {messages[-1]['content']}")
        self._models = models or [ModelInfo("mock-small", name, int(1e9))]
        self.calls: list[tuple[str, list[Message], dict | None]] = []
        self.embedder = embedder
        self.embed_calls = 0
        self.image_calls: list = []
        self.logprob = logprob  # (モデル名, 回答) → 各トークンの対数確率(確からしさの試験用)
        self.loaded: set[str] | None = None  # 読み込み済みとして扱うモデル(モデル選びの試験用)
        # (モデル名, 会話) → 1語目の候補ごとの対数確率 {"A": -0.1, "B": -2.4}(判断の試験用。None なら返さない)
        self.top: Callable[[str, list[Message]], dict[str, float]] | None = None

    def loaded_models(self) -> set[str] | None:
        return self.loaded

    def embedding_models(self) -> list[str]:
        return ["mock-embed"] if self.embedder else []

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [self.embedder(t) for t in texts]

    def list_models(self) -> list[ModelInfo]:
        return list(self._models)

    def generate(self, model, messages, *, temperature=0.2, max_tokens=None, json_schema=None, on_token=None,
                 images=None, logprobs=None, top_logprobs=None) -> Generation:
        self.calls.append((model, messages, json_schema))
        self.image_calls.append(images)
        if top_logprobs is not None and self.top is not None:
            first = self.top(model, messages)
            top_logprobs.append(first)
            text = max(first, key=first.get)
            return Generation(text, sum(len(m["content"]) for m in messages) // 3, 1, 0.01)
        text = self.responder(model, messages, json_schema)
        if logprobs is not None and self.logprob is not None:
            logprobs.extend([self.logprob(model, text)] * max(1, len(text) // 3))
        if on_token:
            on_token(text)
        return Generation(text, sum(len(m["content"]) for m in messages) // 3, len(text) // 3, 0.01)
