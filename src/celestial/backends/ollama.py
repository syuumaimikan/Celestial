from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .base import Backend, BackendError, Generation, Message, ModelInfo, loop_cut, register


@register("ollama")
class OllamaBackend(Backend):
    """Ollama の HTTP API を使うバックエンド(標準ライブラリのみ)。"""

    def __init__(self, name: str, host: str = "http://127.0.0.1:11434", keep_alive: str = "30m", timeout: float = 600,
                 max_answer_tokens: int = 2048, context_tokens: int = 16384, **options):
        super().__init__(name, **options)
        self.host = host.rstrip("/")
        self.keep_alive = keep_alive
        self.timeout = timeout
        # 回答の長さの上限。無制限だと、弱いモデルが同じ文を延々と繰り返して数分止まらないことがある
        self.max_answer_tokens = max_answer_tokens
        # 文脈長。指定しないと Ollama はモデルの最大値(gemma3 なら 131072)で読み込み、GPU メモリを大量に使って
        # モデルの切り替えが遅くなる。全モデルで同じ値を使い、値の違いによる読み込み直しも避ける
        self.context_tokens = context_tokens
        self._vision: dict[str, bool] = {}
        self._files: dict[str, dict | None] = {}

    def _open(self, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.host + path, data=data, headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise BackendError(f"Ollama エラー {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise BackendError(f"Ollama ({self.host}) に接続できません。`ollama serve` が起動しているか確認してください: {e}") from e

    def _tags(self) -> list[dict]:
        with self._open("/api/tags") as r:
            return json.load(r).get("models", [])

    @staticmethod
    def _is_embedding(m: dict) -> bool:
        family = (m.get("details") or {}).get("family") or ""
        return "embed" in m["name"] or "bert" in family

    def _has_vision(self, m: dict) -> bool:
        key = m.get("digest") or m["name"]
        if key not in self._vision:
            try:
                with self._open("/api/show", {"model": m["name"]}) as r:
                    self._vision[key] = "vision" in (json.load(r).get("capabilities") or [])
            except BackendError:
                return False
        return self._vision[key]

    def model_file(self, name: str) -> dict | None:
        """モデル本体の GGUF ファイル(スマホへ配るため)。{path, size, family, parameters, quantization} か None。"""
        if name not in self._files:
            with self._open("/api/show", {"model": name}) as r:
                data = json.load(r)
            found = re.search(r"^FROM (.+)$", data.get("modelfile") or "", re.M)
            path = found.group(1).strip() if found else ""
            info = None
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    if f.read(4) == b"GGUF":
                        details = data.get("details") or {}
                        info = {"path": path, "size": os.path.getsize(path), "family": details.get("family") or "",
                                "parameters": details.get("parameter_size") or "",
                                "quantization": details.get("quantization_level") or ""}
            self._files[name] = info
        return self._files[name]

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(m["name"], self.name, int(m.get("size", 0)), vision=self._has_vision(m))
                for m in self._tags() if not self._is_embedding(m)]

    def embedding_models(self) -> list[str]:
        return [m["name"] for m in self._tags() if self._is_embedding(m)]

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        # 埋め込みは CPU で計算する: 十分速く、回答用モデルと GPU メモリを取り合わない
        with self._open("/api/embed", {"model": model, "input": texts, "options": {"num_gpu": 0}, "keep_alive": self.keep_alive}) as r:
            vectors = json.load(r).get("embeddings") or []
        if len(vectors) != len(texts):
            raise BackendError("埋め込みの件数が一致しません")
        return vectors

    def release(self) -> None:
        """読み込み済みのモデルをすべて降ろす。"""
        try:
            with self._open("/api/ps") as r:
                running = [m["name"] for m in json.load(r).get("models", [])]
            for name in running:
                with self._open("/api/generate", {"model": name, "keep_alive": 0}) as r:
                    r.read()
            deadline = time.monotonic() + 15  # 降ろし終えるまで待つ(直後に GPU を使う処理が控えている)
            while running and time.monotonic() < deadline:
                time.sleep(0.5)
                with self._open("/api/ps") as r:
                    running = json.load(r).get("models", [])
        except BackendError:
            pass

    def loaded_models(self) -> set[str] | None:
        """いま読み込まれているモデル(/api/ps)。依頼のたびに聞かないよう、2秒は使い回す。"""
        now = time.monotonic()
        cached = getattr(self, "_loaded", None)
        if cached is not None and now - cached[0] < 2.0:
            return cached[1]
        try:
            with self._open("/api/ps") as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
        except (BackendError, ValueError):
            return None
        self._loaded = (now, names)
        return names

    def generate(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        images: list[bytes] | None = None,
        logprobs: list[float] | None = None,
        top_logprobs: list[dict[str, float]] | None = None,
    ) -> Generation:
        options: dict = {"temperature": temperature, "num_predict": max_tokens or self.max_answer_tokens,
                         "num_ctx": self.context_tokens}
        if images:
            messages = messages[:-1] + [{**messages[-1], "images": [base64.b64encode(i).decode("ascii") for i in images]}]
        # 常に逐次受け取る: 同じ文の繰り返しに入ったら、その場で打ち切れるようにするため
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options,
            "keep_alive": self.keep_alive,
        }
        if json_schema is not None:
            payload["format"] = json_schema
        if logprobs is not None or top_logprobs is not None:
            payload["logprobs"] = True
        if top_logprobs is not None:  # 各位置の上位の候補とその対数確率(判断の確率に使う)
            payload["top_logprobs"] = 20

        t0 = time.perf_counter()
        parts: list[str] = []
        final: dict = {}
        size = checked = 0
        cut: str | None = None
        # on_token が例外を投げたら(確からしさが低いので打ち切る、など)、with を抜けて接続を閉じる。
        # Ollama は接続が切れると生成をやめる
        with self._open("/api/chat", payload) as r:
            for line in r:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise BackendError(f"Ollama エラー: {chunk['error']}")
                if logprobs is not None:
                    logprobs.extend(float(x.get("logprob", 0.0)) for x in chunk.get("logprobs") or [])
                if top_logprobs is not None:
                    for x in chunk.get("logprobs") or []:
                        alternatives: dict[str, float] = {}
                        for alt in x.get("top_logprobs") or []:
                            token = str(alt.get("token", ""))
                            alternatives[token] = max(alternatives.get(token, -1e9), float(alt.get("logprob", -1e9)))
                        top_logprobs.append(alternatives)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    parts.append(piece)
                    size += len(piece)
                    if on_token is not None:
                        on_token(piece)
                    if json_schema is None and size - checked >= 64:
                        checked = size
                        cut = loop_cut("".join(parts))
                        if cut is not None:
                            break  # 繰り返しに入った: 1回分を残して打ち切る
                if chunk.get("done"):
                    final = chunk
        text = cut if cut is not None else "".join(parts)
        return Generation(
            text=text.strip(),
            prompt_tokens=int(final.get("prompt_eval_count", 0)),
            completion_tokens=int(final.get("eval_count", 0)) or (len(logprobs) if logprobs else len(text) // 2),
            latency_s=time.perf_counter() - t0,
        )
