"""自作AI用バックエンド: Celestial 自身が学習したモデルを、このプロセス内で直接動かす。

モデルは <home>/models/<名前>/ に置かれ、celestial_model.json(基礎モデル名など)と LoRA アダプタからなる。
torch などの重いライブラリは、実際に生成するときまで読み込まない(未導入でも一覧取得は動く)。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .base import Backend, BackendError, Generation, ModelInfo, register

META_FILE = "celestial_model.json"


def read_models(models_dir: Path) -> list[dict]:
    metas = []
    if models_dir.is_dir():
        for meta_path in sorted(models_dir.glob(f"*/{META_FILE}")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["path"] = str(meta_path.parent)
                metas.append(meta)
            except (OSError, ValueError):
                continue
    return metas


@register("native")
class NativeBackend(Backend):
    def __init__(self, name: str, models_dir: str | None = None, max_new_tokens: int = 1024, **options):
        super().__init__(name, **options)
        self.models_dir = Path(models_dir) if models_dir else Path.home() / ".celestial" / "models"
        self.max_new_tokens = max_new_tokens
        self._lock = threading.Lock()
        self._loaded: tuple[str, object, object] | None = None  # (名前, tokenizer, model)

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(m["name"], self.name, int(m.get("size_bytes", 0)), "small", can_manage=False)
                for m in read_models(self.models_dir)]

    def release(self) -> None:
        with self._lock:
            self._loaded = None
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

    def _load(self, name: str):
        if self._loaded and self._loaded[0] == name:
            return self._loaded[1], self._loaded[2]
        meta = next((m for m in read_models(self.models_dir) if m["name"] == name), None)
        if meta is None:
            raise BackendError(f"自作AI {name} が見つかりません")
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:
            raise BackendError("自作AIを動かすには学習用ライブラリが必要です(README の「自作AI」を参照): " + str(e)) from e
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()
        self._loaded = None
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
        tokenizer = AutoTokenizer.from_pretrained(meta["base"])
        model = AutoModelForCausalLM.from_pretrained(meta["base"], dtype=dtype)
        if meta.get("adapter", True):
            model = PeftModel.from_pretrained(model, meta["path"]).merge_and_unload()  # 合成して推論を速くする
        model.to(device).eval()
        self._loaded = (name, tokenizer, model)
        return tokenizer, model

    def generate(self, model, messages, *, temperature=0.2, max_tokens=None, json_schema=None, on_token=None,
                 images=None, logprobs=None, top_logprobs=None) -> Generation:  # top_logprobs は未対応(空のまま)
        if images:
            raise BackendError("このモデルは画像を扱えません")
        with self._lock:
            tokenizer, net = self._load(model)
            import torch
            from transformers import TextStreamer

            if json_schema is not None:  # 構造の強制はできないので、指示として添える
                messages = messages + [{"role": "system", "content": "Reply with JSON only, matching this schema: " + json.dumps(json_schema)}]
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer(text, return_tensors="pt", add_special_tokens=False).to(net.device)

            streamer = None
            if on_token is not None:
                class _Streamer(TextStreamer):
                    def on_finalized_text(self, piece: str, stream_end: bool = False):
                        if piece:
                            on_token(piece)

                streamer = _Streamer(tokenizer, skip_prompt=True, skip_special_tokens=True)

            sampling = {"do_sample": True, "temperature": temperature, "top_p": 0.9} if temperature > 0.05 else {"do_sample": False}
            t0 = time.perf_counter()
            with torch.inference_mode():
                out = net.generate(**inputs, max_new_tokens=max_tokens or self.max_new_tokens, repetition_penalty=1.05,
                                   pad_token_id=tokenizer.pad_token_id, streamer=streamer, **sampling)
            n_prompt = inputs["input_ids"].shape[1]
            answer = tokenizer.decode(out[0, n_prompt:], skip_special_tokens=True).strip()
            return Generation(answer, n_prompt, int(out.shape[1] - n_prompt), time.perf_counter() - t0)
