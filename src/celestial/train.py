"""自作AIの学習: 小さな基礎モデルに、これまでの良い回答と自習の成果を LoRA で教え込む。

速さのための工夫: 基礎モデルは凍結して LoRA の重みだけ学習、bf16 の自動混合精度、長さの近い例をまとめたバッチ。
学習したモデルは <home>/models/ に保存され、native バックエンド経由で他のモデルと同じように使われる。
"""

from __future__ import annotations

import gc
import json
import math
import random
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .backends.native import META_FILE, read_models
from .textsim import task_hash
from .workers import ROLES

Progress = Callable[[int, int, str], None]
INSTALL_HINT = ("学習用ライブラリが入っていません。次を実行してください:\n"
                "  uv pip install torch --index-url https://download.pytorch.org/whl/cu128\n"
                "  uv pip install transformers peft gguf")


class TrainError(RuntimeError):
    pass


@dataclass
class TrainConfig:
    base: str = "Qwen/Qwen2.5-0.5B-Instruct"
    epochs: int = 4
    lr: float = 1e-4
    batch_size: int = 8       # 1バッチの最大件数
    batch_tokens: int = 1024  # 1バッチの最大トークン数(詰め物込み)。GPU メモリの使用量をこれで抑える
    max_tokens: int = 768  # これより長い例は使わない
    rank: int = 16
    seed: int = 0


def training_examples(store) -> list[dict]:
    """学習データ = 悪評価を除いた実行履歴 + 自習の成果。同じ問題は履歴側を優先する。"""
    examples: dict[str, dict] = {}
    for r in store.study_rows():
        examples[task_hash(r["task"])] = {"role": r["role"], "task": r["task"], "answer": r["answer"]}
    for r in store.export_rows():
        if r["role"] in ROLES:
            examples[task_hash(r["task"])] = {"role": r["role"], "task": r["task"], "answer": r["answer"]}
    return list(examples.values())


def libraries_available() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "peft"))


def finish_model(app, name: str, *, export: bool = True, run_code: bool = False, on_progress: Progress | None = None) -> dict:
    """学習後の仕上げ: Ollama へ書き出し(可能なら)、精度を計測する。基礎モデルも未計測なら同じ課題で計測する。

    戻り値は {model: 実際に使われるモデル名, report: 計測結果, base_report: 基礎モデルの計測結果または None, export_error}。
    """
    from .bench import load_tasks, run_bench, suite_id
    from .ollama_export import ExportError, export_to_ollama, ollama_available

    def report(done: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(done, total, message)

    models_dir = app.cfg.home / "models"
    tasks = load_tasks()
    suite = suite_id([t for t in tasks if run_code or "tests" not in t["check"]])
    measured = {(r["label"], r["suite"]) for r in app.store.bench_history()}
    result: dict = {"export_error": None, "base_report": None}
    final_names = {}
    for own in ("celestial-base", name):
        final = own
        meta = next((m for m in read_models(models_dir) if m["name"] == own), None)
        if meta is None:
            continue
        if meta.get("ollama"):
            final = meta["ollama"]
        elif export and ollama_available():
            report(0, 0, f"{own} を Ollama に書き出し中")
            for backend in app.backends.values():
                backend.release()
            try:
                final = export_to_ollama(models_dir, own)
            except ExportError as e:
                result["export_error"] = str(e)
        final_names[own] = final
    app.models(refresh=True)
    for own, final in final_names.items():
        if own == "celestial-base" and (final, suite) in measured:
            continue
        bench = run_bench(app, tasks, model=final, run_code=run_code,
                          on_outcome=lambda o, i, total, final=final: report(i, total, f"{final} の精度を計測中"))
        result["base_report" if own == "celestial-base" else "report"] = bench
    result["model"] = final_names.get(name, name)
    return result


def _next_name(models_dir: Path) -> str:
    versions = [int(m["name"].rsplit("-v", 1)[1]) for m in read_models(models_dir)
                if m["name"].startswith("celestial-v") and m["name"].rsplit("-v", 1)[1].isdigit()]
    return f"celestial-v{max(versions, default=0) + 1}"


def train(app, config: TrainConfig | None = None, *, on_progress: Progress | None = None) -> dict:
    """学習して保存し、モデルの情報(celestial_model.json の内容)を返す。

    GPU メモリが足りなければ、一度に処理する量(バッチのトークン数)を半分にして自動でやり直す。
    """
    cfg = config or TrainConfig()
    while True:
        try:
            return _train_once(app, cfg, on_progress)
        except TrainError:
            raise
        except Exception as e:
            if "out of memory" not in str(e).lower():
                raise
        # ここに来るのはメモリ不足のときだけ。例外を抜けてから後片付けする(例外が重みへの参照を握っているため)
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except ImportError:
            pass
        if cfg.batch_tokens <= 512:
            raise TrainError("GPU メモリが足りず学習できませんでした。他のアプリを閉じるか、より小さい基礎モデルを指定してください。")
        cfg = replace(cfg, batch_tokens=cfg.batch_tokens // 2)
        if on_progress:
            on_progress(0, 1, f"GPU メモリが足りないため、バッチを {cfg.batch_tokens} トークンに減らしてやり直します")


def _train_once(app, cfg: TrainConfig, on_progress: Progress | None) -> dict:
    examples = training_examples(app.store)
    if len(examples) < 8:
        raise TrainError(f"学習データが足りません(現在 {len(examples)} 件、最低 8 件)。「自習」で増やせます。")
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        raise TrainError(INSTALL_HINT) from e
    from transformers.utils import logging as hf_logging

    hf_logging.disable_progress_bar()

    def report(step: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(step, total, message)

    report(0, 1, "GPUメモリを空けています")
    for backend in app.backends.values():
        backend.release()

    started = time.perf_counter()
    torch.manual_seed(cfg.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    use_amp = device == "cuda" and torch.cuda.is_bf16_supported()
    report(0, 1, f"基礎モデル {cfg.base} を読み込み中")
    tokenizer = AutoTokenizer.from_pretrained(cfg.base)
    model = AutoModelForCausalLM.from_pretrained(cfg.base, dtype=torch.bfloat16 if use_amp else torch.float32).to(device)
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id

    # 質問部分は損失から外し、回答部分だけを学習させる
    encoded = []
    for ex in examples:
        messages = [{"role": "system", "content": ROLES[ex["role"]].system}, {"role": "user", "content": ex["task"]}]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        full = tokenizer.apply_chat_template(messages + [{"role": "assistant", "content": ex["answer"]}], tokenize=False)
        if not full.startswith(prompt):
            continue
        prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        answer_ids = tokenizer(full[len(prompt):], add_special_tokens=False)["input_ids"]
        if answer_ids and len(prompt_ids) + len(answer_ids) <= cfg.max_tokens:
            encoded.append((prompt_ids + answer_ids, [-100] * len(prompt_ids) + answer_ids))
    if len(encoded) < 8:
        raise TrainError(f"使える学習データが足りません({len(encoded)} 件)。")

    rng = random.Random(cfg.seed)
    rng.shuffle(encoded)
    n_eval = max(2, len(encoded) // 10) if len(encoded) >= 20 else 0
    eval_set, train_set = encoded[:n_eval], encoded[n_eval:]

    def batches(data: list, shuffle: bool) -> list:
        data = sorted(data, key=lambda e: len(e[0]))  # 長さの近い例をまとめて、詰め物を減らす
        chunks, current = [], []
        for item in data:  # 昇順なので、いま足す例がバッチ内で最長になる
            if current and (len(current) >= cfg.batch_size or (len(current) + 1) * len(item[0]) > cfg.batch_tokens):
                chunks.append(current)
                current = []
            current.append(item)
        if current:
            chunks.append(current)
        if shuffle:
            rng.shuffle(chunks)
        return chunks

    def to_tensors(chunk: list):
        width = max(len(ids) for ids, _ in chunk)
        ids = torch.full((len(chunk), width), pad_id, dtype=torch.long)
        labels = torch.full((len(chunk), width), -100, dtype=torch.long)
        mask = torch.zeros((len(chunk), width), dtype=torch.long)
        for i, (row, lab) in enumerate(chunk):
            ids[i, : len(row)] = torch.tensor(row)
            labels[i, : len(lab)] = torch.tensor(lab)
            mask[i, : len(row)] = 1
        return ids.to(device), mask.to(device), labels.to(device)

    core = model  # LoRA はこのオブジェクトの中に直接差し込まれるので、学習中もこの参照で呼べる

    def forward(chunk: list):
        # 語彙(約15万語)への出力は、回答部分の位置だけ計算する。
        # 全位置で計算すると数GBを余計に使い、8GB の GPU からあふれて極端に遅くなる(実測で山が 11GB)。
        ids, mask, labels = to_tensors(chunk)
        with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
            hidden = core.base_model(input_ids=ids, attention_mask=mask).last_hidden_state[:, :-1]
            targets = labels[:, 1:]  # 位置 t の出力は、位置 t+1 のトークンを当てる
            keep = targets != -100
            logits = core.get_output_embeddings()(hidden[keep])
        return torch.nn.functional.cross_entropy(logits.float(), targets[keep])

    def evaluate() -> float | None:
        if not eval_set:
            return None
        model.eval()
        with torch.no_grad():
            losses = [forward(chunk).item() for chunk in batches(eval_set, False)]
        model.train()
        return sum(losses) / len(losses)

    base_eval = evaluate()
    model = get_peft_model(model, LoraConfig(
        task_type="CAUSAL_LM", r=cfg.rank, lora_alpha=cfg.rank * 2, lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]))
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=cfg.lr, weight_decay=0.0)
    total_steps = cfg.epochs * len(batches(train_set, False))
    warmup = max(1, total_steps // 20)
    schedule = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lambda s: (s + 1) / warmup if s < warmup else 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(1, total_steps - warmup))))

    model.train()
    # 少ないデータでは周回を重ねると丸暗記(過学習)に陥るので、周回ごとに検証し、最も良かった時点の重みを採用する
    step, last_loss, eval_loss = 0, 0.0, None
    best_loss, best_state, best_epoch, eval_history = base_eval, None, 0, []
    for epoch in range(cfg.epochs):
        epoch_losses = []
        for chunk in batches(train_set, True):
            loss = forward(chunk)
            if not torch.isfinite(loss):
                raise TrainError("学習が発散しました(損失が数値でなくなりました)。学習率を下げて試してください。")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            schedule.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            if device == "cuda" and step % 4 == 0:
                torch.cuda.empty_cache()  # バッチごとに形が違うので、ため込んだ未使用メモリをこまめに返す(断片化対策)
            epoch_losses.append(loss.item())
            report(step, total_steps, f"学習中 {epoch + 1}/{cfg.epochs} 周目  損失 {loss.item():.3f}")
        last_loss = sum(epoch_losses) / len(epoch_losses)
        eval_loss = evaluate()
        if eval_loss is not None:
            eval_history.append(round(eval_loss, 4))
            if best_loss is None or eval_loss < best_loss:
                best_loss, best_epoch = eval_loss, epoch + 1
                best_state = {n: p.detach().to("cpu", copy=True) for n, p in model.named_parameters() if p.requires_grad}
    if eval_history:
        if best_state is None:
            raise TrainError("学習しても検証データでの成績が基礎モデルより良くなりませんでした。自習でデータを増やしてから試してください。")
        with torch.no_grad():
            for n, p in model.named_parameters():
                if n in best_state:
                    p.copy_(best_state[n])
        eval_loss = best_loss

    models_dir = app.cfg.home / "models"
    name = _next_name(models_dir)
    out_dir = models_dir / name
    out_dir.mkdir(parents=True, exist_ok=True)
    report(total_steps, total_steps, "保存中")
    model.save_pretrained(str(out_dir))
    n_params = sum(p.numel() for p in model.parameters())
    meta = {
        "name": name, "base": cfg.base, "created": time.time(), "examples": len(train_set), "eval_examples": len(eval_set),
        "train_loss": round(last_loss, 4), "eval_loss": None if eval_loss is None else round(eval_loss, 4),
        "base_eval_loss": None if base_eval is None else round(base_eval, 4),
        "eval_history": eval_history, "best_epoch": best_epoch or cfg.epochs,
        "seconds": round(time.perf_counter() - started, 1), "size_bytes": n_params * 2, "config": asdict(cfg),
    }
    (out_dir / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 学習前の基礎モデルも「celestial-base」として登録し、学習の効果を計測で比べられるようにする
    base_dir = models_dir / "celestial-base"
    base_meta_path = base_dir / META_FILE
    current = json.loads(base_meta_path.read_text(encoding="utf-8")) if base_meta_path.is_file() else {}
    if current.get("base") != cfg.base:
        base_dir.mkdir(parents=True, exist_ok=True)
        base_meta_path.write_text(json.dumps({
            "name": "celestial-base", "base": cfg.base, "adapter": False, "created": time.time(),
            "examples": 0, "eval_loss": meta["base_eval_loss"], "base_eval_loss": meta["base_eval_loss"],
            "seconds": 0, "size_bytes": n_params * 2}, ensure_ascii=False, indent=2), encoding="utf-8")

    del model, optimizer
    if device == "cuda":
        torch.cuda.empty_cache()
    app.models(refresh=True)
    return meta
