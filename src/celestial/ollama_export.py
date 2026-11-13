"""自作AIを Ollama に取り込む: LoRA を基礎モデルに合成 → GGUF に変換 → `ollama create` で登録・量子化。

Ollama 側で動かすと、配布モデルと同じ高速な推論エンジンを使え、JSON の構造も強制できる。
GGUF への変換は llama.cpp 公式の gguf ライブラリで自前で行う(Ollama 0.34 内蔵の safetensors 変換は
Qwen 系で壊れた出力になることを実機で確認したため)。対応する基礎モデルは Qwen2 / Qwen2.5 系。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .backends.native import META_FILE, read_models

# ChatML 形式のテンプレート(Qwen 系)
_CHATML = '''TEMPLATE """{{- range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
"""
PARAMETER stop <|im_start|>
PARAMETER stop <|im_end|>
PARAMETER stop <|endoftext|>
'''


class ExportError(RuntimeError):
    pass


def ollama_available() -> bool:
    return shutil.which("ollama") is not None


def write_gguf(model, tokenizer, tokenizer_dir: Path, out_path: Path, name: str) -> None:
    """Qwen2 系の transformers モデルを F16 の GGUF として書き出す。"""
    import gguf
    import numpy as np

    config = model.config
    if getattr(config, "model_type", "") != "qwen2":
        raise ExportError(f"基礎モデルの形式 {getattr(config, 'model_type', '?')} は Ollama への書き出しに未対応です(対応: Qwen2 / Qwen2.5 系)")

    writer = gguf.GGUFWriter(str(out_path), "qwen2")
    writer.add_name(name)
    writer.add_context_length(config.max_position_embeddings)
    writer.add_embedding_length(config.hidden_size)
    writer.add_block_count(config.num_hidden_layers)
    writer.add_feed_forward_length(config.intermediate_size)
    writer.add_head_count(config.num_attention_heads)
    writer.add_head_count_kv(config.num_key_value_heads)
    writer.add_layer_norm_rms_eps(config.rms_norm_eps)
    writer.add_rope_freq_base(float(getattr(config, "rope_theta", None) or (getattr(config, "rope_parameters", None) or {}).get("rope_theta", 1000000.0)))
    writer.add_file_type(gguf.LlamaFileType.MOSTLY_F16)

    # 語彙: id 順のトークン列と種別。設定上の語彙数に満たない分は未使用トークンで埋める
    vocab = tokenizer.get_vocab()
    by_id = {i: t for t, i in vocab.items()}
    added = tokenizer.get_added_vocab()
    special = set(tokenizer.all_special_tokens)
    vocab_size = max(config.vocab_size, max(by_id) + 1)
    tokens, types = [], []
    for i in range(vocab_size):
        token = by_id.get(i)
        if token is None:
            tokens.append(f"[PAD{i}]")
            types.append(gguf.TokenType.UNUSED)
        elif token in added:
            tokens.append(token)
            types.append(gguf.TokenType.CONTROL if token in special or token.startswith("<|") else gguf.TokenType.USER_DEFINED)
        else:
            tokens.append(token)
            types.append(gguf.TokenType.NORMAL)
    writer.add_tokenizer_model("gpt2")
    writer.add_tokenizer_pre("qwen2")
    writer.add_token_list(tokens)
    writer.add_token_types(types)
    gguf.SpecialVocab(str(tokenizer_dir), load_merges=True, n_vocab=len(tokens)).add_to_gguf(writer)

    names = gguf.get_tensor_name_map(gguf.MODEL_ARCH.QWEN2, config.num_hidden_layers)
    tied = bool(getattr(config, "tie_word_embeddings", False))
    for key, tensor in model.state_dict().items():
        if key == "lm_head.weight" and tied:
            continue  # 入力の埋め込みと共有されている
        new_name = names.get_name(key, try_suffixes=(".weight", ".bias"))
        if new_name is None:
            raise ExportError(f"変換できない重みがあります: {key}")
        data = tensor.detach().to("cpu").float().numpy()
        writer.add_tensor(new_name, data.astype(np.float16 if data.ndim >= 2 else np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def export_to_ollama(models_dir: Path, name: str, *, quantize: str = "q8_0", timeout: float = 900) -> str:
    """<models_dir>/<name> の自作AIを Ollama に登録し、Ollama 上のモデル名を返す。"""
    meta = next((m for m in read_models(models_dir) if m["name"] == name), None)
    if meta is None:
        raise ExportError(f"自作AI {name} が見つかりません")
    exe = shutil.which("ollama")
    if exe is None:
        raise ExportError("ollama コマンドが見つかりません")
    try:
        import gguf  # noqa: F401
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from transformers.utils import logging as hf_logging
    except ImportError as e:
        raise ExportError("書き出しには学習用ライブラリと gguf が必要です(uv pip install gguf): " + str(e)) from e
    hf_logging.disable_progress_bar()

    with tempfile.TemporaryDirectory(prefix="celestial-export-") as tmp:
        work = Path(tmp)
        tokenizer = AutoTokenizer.from_pretrained(meta["base"])
        model = AutoModelForCausalLM.from_pretrained(meta["base"], dtype=torch.float32)
        if meta.get("adapter", True):
            model = PeftModel.from_pretrained(model, meta["path"]).merge_and_unload()
        tokenizer.save_pretrained(str(work))
        write_gguf(model, tokenizer, work, work / "model.gguf", name)
        del model

        (work / "Modelfile").write_text(f"FROM {(work / 'model.gguf').as_posix()}\n" + _CHATML, encoding="utf-8")
        command = [exe, "create", name, "-f", str(work / "Modelfile")]
        if quantize:
            command += ["-q", quantize]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ExportError("Ollama への取り込みが時間内に終わりませんでした") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise ExportError("Ollama への取り込みに失敗しました: " + " / ".join(detail))

    ollama_name = f"{name}:latest"
    meta_path = Path(meta["path"]) / META_FILE
    saved = json.loads(meta_path.read_text(encoding="utf-8"))
    saved["ollama"] = ollama_name  # 以後は Ollama 側のモデルとして扱い、native の一覧からは外す
    meta_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return ollama_name
