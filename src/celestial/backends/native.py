"""自作AI用バックエンド: Celestial 自身が学習したモデルを、このプロセス内で直接動かす。

モデルは <home>/models/<名前>/ に置かれ、celestial_model.json(基礎モデル名など)と LoRA アダプタからなる。
torch などの重いライブラリは、実際に生成するときまで読み込まない(未導入でも一覧取得は動く)。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .base import Backend, BackendError, Generation, ModelInfo, loop_cut, register

META_FILE = "celestial_model.json"


def _expects_array(schema: dict | None) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "array"


def _matches_schema(data, schema: dict | None) -> bool:
    if not isinstance(schema, dict):
        return True
    if "anyOf" in schema:
        return any(_matches_schema(data, sub) for sub in schema["anyOf"])
    if "enum" in schema and data not in schema["enum"]:
        return False
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(data, dict):
            return False
        for req in schema.get("required") or []:
            if req not in data:
                return False
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in data and not _matches_schema(data[k], sub):
                return False
        return True
    if expected_type == "array":
        if not isinstance(data, list):
            return False
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return all(_matches_schema(x, item_schema) for x in data)
        return True
    if expected_type == "string":
        return isinstance(data, str)
    if expected_type == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected_type == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if expected_type == "boolean":
        return isinstance(data, bool)
    return True


def _extract_json(text: str, expect_array: bool = False) -> str | None:
    """生成された文字列から最初に見つかる妥当な JSON ({...} または [...]) を取り出して整形する。"""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    open_ch, close_ch = ("[", "]") if expect_array else ("{", "}")
    candidates = [s]
    idx = s.find(open_ch)
    if idx >= 0:
        candidates.append(s[idx:])
    else:
        alt_idx = s.find("{" if expect_array else "[")
        if alt_idx >= 0:
            candidates.append(s[alt_idx:])
    for cand in candidates:
        # 末尾の閉じ括弧不足・余計な末尾カンマを軽く修復してパースを試す
        trimmed = cand.strip()
        for attempt in (trimmed, re.sub(r",\s*([}\]])", r"\1", trimmed)):
            try:
                parsed = json.loads(attempt)
                return json.dumps(parsed, ensure_ascii=False)
            except ValueError:
                pass
        # 括弧が閉じ切っていない場合、バランス分を補って修復する
        depth_obj, depth_arr, in_str, esc, end_pos = 0, 0, False, False, None
        for i, ch in enumerate(trimmed):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth_obj += 1
            elif ch == "}":
                depth_obj -= 1
            elif ch == "[":
                depth_arr += 1
            elif ch == "]":
                depth_arr -= 1
            if i > 0 and depth_obj == 0 and depth_arr == 0 and (trimmed[0] in "{["):
                end_pos = i + 1
                break
        if end_pos is not None:
            sub = re.sub(r",\s*([}\]])", r"\1", trimmed[:end_pos])
            try:
                parsed = json.loads(sub)
                return json.dumps(parsed, ensure_ascii=False)
            except ValueError:
                pass
        elif trimmed.startswith(("{", "[")) and (depth_obj > 0 or depth_arr > 0):
            patched = trimmed + ('"' if in_str else "")
            patched = re.sub(r":\s*$", ": null", patched)
            patched = re.sub(r'(?:^|,)(\s*"[^"]*")\s*$', lambda m: "" if "," in m.group(0) else "", patched)
            patched = patched + ("]" * max(0, depth_arr)) + ("}" * max(0, depth_obj))
            patched = re.sub(r",\s*([}\]])", r"\1", patched)
            try:
                parsed = json.loads(patched)
                return json.dumps(parsed, ensure_ascii=False)
            except ValueError:
                pass
    return None


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


def render_chat(tokenizer, messages: list[dict], *, add_generation_prompt: bool = False, thinking: bool = False) -> str:
    """Qwen2 / Qwen3 を統一的に扱うチャットテンプレート適用(Qwen3 では不要な思考出力を抑止可能)。"""
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=add_generation_prompt, enable_thinking=thinking,
        )
    except TypeError:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=add_generation_prompt)


@register("native")
class NativeBackend(Backend):
    def __init__(self, name: str, models_dir: str | None = None, max_new_tokens: int = 1024, **options):
        super().__init__(name, **options)
        self.models_dir = Path(models_dir) if models_dir else Path.home() / ".celestial" / "models"
        self.max_new_tokens = max_new_tokens
        self._lock = threading.Lock()
        self._loaded: tuple[str, object, object] | None = None  # (名前, tokenizer, model)
        self._json_masks: dict[tuple[int, str], list[int]] = {}

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                m["name"], self.name, int(m.get("size_bytes", 0)), m.get("tier", "small"),
                can_manage=bool(m.get("can_manage", False)),
                specialties=tuple(m.get("specialties") or ()),
                profile=str(m.get("profile") or ""),
            )
            for m in read_models(self.models_dir)
        ]

    def loaded_models(self) -> set[str] | None:
        return {self._loaded[0]} if self._loaded else set()

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

    def _json_start_ids(self, tokenizer, expect_char: str) -> list[int]:
        """最初のトークンが JSON 開始文字({ または [)になるトークン ID 群をキャッシュして返す。"""
        key = (id(tokenizer), expect_char)
        cached = self._json_masks.get(key)
        if cached is not None:
            return cached
        allowed: list[int] = []
        vocab = tokenizer.get_vocab() if hasattr(tokenizer, "get_vocab") else {str(i): i for i in range(len(tokenizer))}
        cand_ids = [tid for tok, tid in vocab.items() if expect_char in tok] or list(range(len(tokenizer)))
        for tid in cand_ids:
            try:
                s = tokenizer.decode([tid], skip_special_tokens=True)
            except Exception:
                continue
            if s.lstrip().startswith(expect_char):
                allowed.append(tid)
        self._json_masks[key] = allowed
        return allowed

    def generate(self, model, messages, *, temperature=0.2, max_tokens=None, json_schema=None, on_token=None,
                 images=None, logprobs=None, top_logprobs=None) -> Generation:
        if images:
            raise BackendError("このモデルは画像を扱えません")
        with self._lock:
            tokenizer, net = self._load(model)
            import torch
            from transformers import LogitsProcessor, StoppingCriteria, TextStreamer

            expect_array = _expects_array(json_schema)
            open_char = "[" if expect_array else "{"
            base_messages = list(messages)
            if json_schema is not None:
                hint = ("Reply with strict valid JSON only (no markdown fences, no commentary), matching this JSON schema: "
                        + json.dumps(json_schema, ensure_ascii=False))
                if base_messages and base_messages[0].get("role") == "system":
                    base_messages = [{**base_messages[0], "content": base_messages[0]["content"] + "\n\n" + hint}, *base_messages[1:]]
                else:
                    base_messages = [{"role": "system", "content": hint}, *base_messages]

            allowed_start = self._json_start_ids(tokenizer, open_char) if json_schema is not None else []
            need_probs = logprobs is not None or top_logprobs is not None

            def _run_once(msgs, temp_val: float, max_toks: int | None, emit_tokens: bool) -> Generation:
                text = render_chat(tokenizer, msgs, add_generation_prompt=True)
                inputs = tokenizer(text, return_tensors="pt", add_special_tokens=False).to(net.device)
                state: dict = {
                    "last_lp": None, "cut": None, "parts": [], "size": 0, "checked": 0, "step": 0,
                    "json_buf": [], "depth_obj": 0, "depth_arr": 0, "in_str": False, "esc": False, "seen_open": False,
                }

                class _LogprobAndJsonProcessor(LogitsProcessor):
                    def __call__(self, input_ids, scores):
                        if json_schema is not None and state["step"] == 0 and allowed_start:
                            masked = torch.full_like(scores, -1e9)
                            masked[:, allowed_start] = scores[:, allowed_start]
                            scores = masked
                        state["step"] += 1
                        if need_probs:
                            state["last_lp"] = torch.log_softmax(scores[0], dim=-1)
                        return scores

                class _StepStop(StoppingCriteria):
                    def __call__(self, input_ids, scores, **kwargs):
                        tok_id = int(input_ids[0, -1].item())
                        lp = state["last_lp"]
                        if lp is not None:
                            if logprobs is not None:
                                logprobs.append(float(lp[tok_id].item()))
                            if top_logprobs is not None:
                                vals, tids = torch.topk(lp, k=min(20, lp.numel()))
                                alts: dict[str, float] = {}
                                for val, tid in zip(vals.tolist(), tids.tolist()):
                                    tok_str = tokenizer.decode([tid], skip_special_tokens=False)
                                    alts[tok_str] = max(alts.get(tok_str, -1e9), float(val))
                                top_logprobs.append(alts)
                            state["last_lp"] = None

                        if json_schema is not None and state["cut"] is None:
                            piece = tokenizer.decode([tok_id], skip_special_tokens=True)
                            if piece:
                                buf_len = sum(len(x) for x in state["json_buf"])
                                state["json_buf"].append(piece)
                                for offset, ch in enumerate(piece):
                                    if state["in_str"]:
                                        if state["esc"]:
                                            state["esc"] = False
                                        elif ch == "\\":
                                            state["esc"] = True
                                        elif ch == '"':
                                            state["in_str"] = False
                                        continue
                                    if ch == '"':
                                        state["in_str"] = True
                                    elif ch == "{":
                                        state["seen_open"] = True
                                        state["depth_obj"] += 1
                                    elif ch == "}":
                                        state["depth_obj"] -= 1
                                    elif ch == "[":
                                        state["seen_open"] = True
                                        state["depth_arr"] += 1
                                    elif ch == "]":
                                        state["depth_arr"] -= 1
                                    if state["seen_open"] and state["depth_obj"] == 0 and state["depth_arr"] == 0 and ch in ("}", "]"):
                                        full = "".join(state["json_buf"])[: buf_len + offset + 1]
                                        state["cut"] = full
                                        break
                        stop = state["cut"] is not None
                        return torch.full((input_ids.shape[0],), stop, dtype=torch.bool, device=input_ids.device)

                class _Streamer(TextStreamer):
                    def on_finalized_text(self, piece: str, stream_end: bool = False):
                        if not piece:
                            return
                        state["parts"].append(piece)
                        state["size"] += len(piece)
                        if emit_tokens and on_token is not None:
                            on_token(piece)
                        if json_schema is None and state["cut"] is None and state["size"] - state["checked"] >= 64:
                            state["checked"] = state["size"]
                            cut = loop_cut("".join(state["parts"]))
                            if cut is not None:
                                state["cut"] = cut

                streamer = _Streamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
                pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
                sampling = {"do_sample": True, "temperature": temp_val, "top_p": 0.9} if temp_val > 0.05 else {"do_sample": False}
                t0 = time.perf_counter()
                with torch.inference_mode():
                    out = net.generate(
                        **inputs, max_new_tokens=max_toks or self.max_new_tokens, repetition_penalty=1.05,
                        pad_token_id=pad_id, streamer=streamer, logits_processor=[_LogprobAndJsonProcessor()],
                        stopping_criteria=[_StepStop()], return_dict_in_generate=True, **sampling,
                    )
                n_prompt = inputs["input_ids"].shape[1]
                seq = out.sequences[0]
                raw = state["cut"] if state["cut"] is not None else tokenizer.decode(seq[n_prompt:], skip_special_tokens=True)
                return Generation(raw.strip(), n_prompt, int(seq.shape[0] - n_prompt), time.perf_counter() - t0)

            gen = _run_once(base_messages, temperature, max_tokens, emit_tokens=(json_schema is None))
            if json_schema is None:
                return gen

            extracted = _extract_json(gen.text, expect_array=expect_array)
            parsed = json.loads(extracted) if extracted is not None else None
            if extracted is not None and _matches_schema(parsed, json_schema):
                if on_token is not None:
                    on_token(extracted)
                return Generation(extracted, gen.prompt_tokens, gen.completion_tokens, gen.latency_s)

            # 1回目がスキーマに合わなければ、失敗内容を添えて温度0で1回だけ自己修復する
            repair_msgs = base_messages + [
                {"role": "assistant", "content": gen.text[:800]},
                {"role": "user", "content": "Your output was invalid or did not match the JSON schema. Output ONLY valid JSON matching the schema."},
            ]
            rep = _run_once(repair_msgs, 0.0, max_tokens, emit_tokens=False)
            fixed = _extract_json(rep.text, expect_array=expect_array) or extracted or ("[]" if expect_array else "{}")
            if on_token is not None:
                on_token(fixed)
            return Generation(fixed, gen.prompt_tokens + rep.prompt_tokens, gen.completion_tokens + rep.completion_tokens, gen.latency_s + rep.latency_s)
