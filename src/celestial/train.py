"""自作AIの学習: 基礎モデルに、良回答・自習の成果・選好ペアを LoRA + KL正則化 + DPO で教え込む。

性能と記憶保持の工夫:
- rsLoRA + NEFTune + 長さ均等化損失で、短い定型課題(推論/要約/抽出)と長い課題(コード)を両立
- 基礎モデルを別メモリにコピーせず `disable_adapter()` で KL アンカー正則化(破滅的忘却の防止)
- 勾配チェックポイント & 128トークン分割出力層により 8GB VRAM でも 1.5B〜3B を安定学習
- 好みペア(悪評価・比較データ)があれば SFT の後に DPO 選好学習を実施
"""

from __future__ import annotations

import gc
import json
import math
import random
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .backends import BackendError
from .backends.native import META_FILE, read_models, render_chat
from .study import answer_ok, clean_answer, verify_answer
from .textsim import task_hash
from .workers import ROLES

Progress = Callable[[int, int, str], None]
INSTALL_HINT = ("学習用ライブラリが入っていません。次を実行してください:\n"
                "  uv pip install torch --index-url https://download.pytorch.org/whl/cu128\n"
                "  uv pip install transformers peft gguf")

MODEL_PRESETS = {
    "0.5b": {"base": "Qwen/Qwen2.5-0.5B-Instruct", "label": "超軽量 0.5B (CPU・低VRAM向け)", "vram_gb": 2.0, "tier": "small"},
    "qwen3-0.6b": {"base": "Qwen/Qwen3-0.6B", "label": "次世代軽量 Qwen3 0.6B", "vram_gb": 2.5, "tier": "small"},
    "1.5b": {"base": "Qwen/Qwen2.5-1.5B-Instruct", "label": "バランス 1.5B (推奨・日本語/推論大幅向上)", "vram_gb": 6.0, "tier": "small"},
    "1.5b-coder": {"base": "Qwen/Qwen2.5-Coder-1.5B-Instruct", "label": "コード特化 1.5B", "vram_gb": 6.0, "tier": "small"},
    "math-1.5b": {"base": "Qwen/Qwen2.5-Math-1.5B-Instruct", "label": "数学・数式特化 1.5B", "vram_gb": 6.0, "tier": "small"},
    "qwen3-1.7b": {"base": "Qwen/Qwen3-1.7B", "label": "次世代高性能 Qwen3 1.7B", "vram_gb": 6.0, "tier": "small"},
    "3b": {"base": "Qwen/Qwen2.5-3B-Instruct", "label": "超高性能 3B (8GB VRAM・CPUオフロード対応)", "vram_gb": 7.5, "tier": "medium"},
    "3b-coder": {"base": "Qwen/Qwen2.5-Coder-3B-Instruct", "label": "コード・設計特化 3B", "vram_gb": 7.5, "tier": "medium"},
}

EXTRA_TRAIN_SYSTEMS: dict[str, str] = {
    "agent": (
        "あなたは自律型AIエージェントです。利用可能な道具・制約・手順を確認し、"
        "厳密な JSON 形式または明確な根拠付き回答で実行可能な最善の次手を示してください。"
    ),
    "judge": (
        "あなたは厳格で正確な判定・分類AIです。与えられた条件と根拠だけに基づき、"
        "JSON または指定選択肢で簡潔・客観的に判定してください。"
    ),
}
_TRAIN_ROLES = set(ROLES) | set(EXTRA_TRAIN_SYSTEMS)

OWN_PROFILES: dict[str, dict] = {
    "nano": {
        "label": "超高速 Nano (0.5B)",
        "desc": "最も軽く高速な汎用モデル。素早い応答・定型抽出・日英翻訳向け",
        "base": "Qwen/Qwen2.5-0.5B-Instruct",
        "default_name": "celestial-nano",
        "roles": None,
        "specialties": ["general", "extract", "translate"],
        "can_manage": False,
        "epochs": 3, "lr": 6e-5, "kl_beta": 0.08, "max_tokens": 1024,
    },
    "qwen3-nano": {
        "label": "次世代 Qwen3-Nano (0.6B)",
        "desc": "Qwen3 系 0.6B。非思考高速モードで軽量ながら高精度な推論・抽出を実現",
        "base": "Qwen/Qwen3-0.6B",
        "default_name": "celestial-q3-nano",
        "roles": None,
        "specialties": ["reason", "general", "extract"],
        "can_manage": False,
        "epochs": 3, "lr": 6e-5, "kl_beta": 0.08, "max_tokens": 1024,
    },
    "fast": {
        "label": "標準オールラウンダー Fast (1.5B)",
        "desc": "速度と知能の最適バランス。普段使い・要約・翻訳・推論を軽快に処理",
        "base": "Qwen/Qwen2.5-1.5B-Instruct",
        "default_name": "celestial-fast",
        "roles": None,
        "specialties": ["general", "translate", "summarize", "extract"],
        "can_manage": False,
        "epochs": 3, "lr": 5e-5, "kl_beta": 0.10, "max_tokens": 1024,
    },
    "qwen3": {
        "label": "次世代バランス Qwen3 (1.7B)",
        "desc": "Qwen3 1.7B 基盤。一般知識・推論・計画・コードを一貫して高精度化",
        "base": "Qwen/Qwen3-1.7B",
        "default_name": "celestial-q3",
        "roles": ["general", "reason", "code", "translate", "summarize", "extract", "write", "agent", "judge"],
        "specialties": ["reason", "general", "extract", "agent"],
        "can_manage": True,
        "epochs": 3, "lr": 5e-5, "kl_beta": 0.10, "max_tokens": 1024,
    },
    "pro": {
        "label": "最上位フラッグシップ Pro (3B)",
        "desc": "全分野最高峰の汎用 3B 自作AI。一般知識・推論・文書・管理役まで統合",
        "base": "Qwen/Qwen2.5-3B-Instruct",
        "default_name": "celestial-pro",
        "roles": ["general", "reason", "code", "translate", "summarize", "extract", "write", "agent", "judge"],
        "specialties": ["general", "reason", "translate", "summarize", "write"],
        "can_manage": True,
        "epochs": 3, "lr": 4e-5, "kl_beta": 0.12, "max_tokens": 1024,
    },
    "coder": {
        "label": "コード・開発特化 Coder (3B)",
        "desc": "テスト検証済みコード・バグ修正・JSON設計に集中特化したエンジニアAI",
        "base": "Qwen/Qwen2.5-Coder-3B-Instruct",
        "default_name": "celestial-coder",
        "roles": ["code", "agent", "reason", "extract"],
        "specialties": ["code", "agent"],
        "can_manage": True,
        "epochs": 3, "lr": 4e-5, "kl_beta": 0.10, "max_tokens": 1280,
    },
    "reason": {
        "label": "数理・論理推論特化 Reason (1.7B)",
        "desc": "算数・代数・論理パズル・単位計算など厳密推論を徹底強化した思考特化型",
        "base": "Qwen/Qwen3-1.7B",
        "default_name": "celestial-reason",
        "roles": ["reason", "extract", "code", "general"],
        "specialties": ["reason"],
        "can_manage": False,
        "epochs": 4, "lr": 5e-5, "kl_beta": 0.08, "max_tokens": 1024,
    },
    "lingual": {
        "label": "言語・要約・抽出特化 Lingual (1.5B)",
        "desc": "日英翻訳・厳密文字数要約・JSON構造化・文章執筆に特化した言語エキスパート",
        "base": "Qwen/Qwen2.5-1.5B-Instruct",
        "default_name": "celestial-lingual",
        "roles": ["translate", "summarize", "extract", "write", "general"],
        "specialties": ["translate", "summarize", "extract", "write"],
        "can_manage": False,
        "epochs": 3, "lr": 5e-5, "kl_beta": 0.10, "max_tokens": 1024,
    },
    "agent": {
        "label": "ツール実行・計画特化 Agent (3B)",
        "desc": "複数ステップ計画・道具選択 JSON・ファイル編集・自律実行に特化(管理役可)",
        "base": "Qwen/Qwen2.5-3B-Instruct",
        "default_name": "celestial-agent",
        "roles": ["agent", "code", "extract", "reason"],
        "specialties": ["agent", "code", "extract"],
        "can_manage": True,
        "epochs": 3, "lr": 4e-5, "kl_beta": 0.10, "max_tokens": 1280,
    },
    "judge": {
        "label": "判定・査読・高速ルーティング Judge (1.5B)",
        "desc": "回答査読・選択肢トリアージ・記憶要否・振り分け専用の超高速判定AI(管理役可)",
        "base": "Qwen/Qwen2.5-1.5B-Instruct",
        "default_name": "celestial-judge",
        "roles": ["judge", "extract", "reason", "general"],
        "specialties": ["judge", "extract"],
        "can_manage": True,
        "epochs": 3, "lr": 5e-5, "kl_beta": 0.10, "max_tokens": 1024,
    },
}


class TrainError(RuntimeError):
    pass


@dataclass
class TrainConfig:
    base: str = "Qwen/Qwen2.5-0.5B-Instruct"
    epochs: int = 3
    lr: float = 5e-5
    batch_size: int = 8       # 1バッチの最大件数
    batch_tokens: int = 1024  # 1バッチの最大トークン数(詰め物込み)。GPU メモリの使用量をこれで抑える
    grad_accum: int = 4       # 勾配累積ステップ数(実効バッチを大きくし学習を安定させる)
    max_tokens: int = 1024    # これより長い例は使わない
    rank: int = 16
    use_rslora: bool = True
    weight_decay: float = 0.01
    neftune_alpha: float = 5.0   # 埋め込みノイズ正則化(過学習抑制・自然さ向上)
    kl_beta: float = 0.10        # 基礎モデルからの過度な逸脱を抑える KL アンカー係数
    pref_beta: float = 0.1       # DPO 選好学習の温度係数 β
    pref_epochs: int = 1         # 比較ペアがある場合の DPO 周回数
    length_balance: bool = True  # 長文に損失が偏らないよう 1/sqrt(L) で重みバランス
    match_ja_hint: bool = True   # 推論時と同じ日本語システム指示を学習時にも添える
    clean_answers: bool = True   # 学習前に教師回答の前置き定型句を掃除する
    gradient_checkpointing: bool | None = None  # None = パラメータ数 0.9B 以上で自動有効
    loraplus_ratio: float = 2.0  # LoRA+: B 行列を A 行列より大きい学習率で更新して収束と推論精度を向上
    offload_optimizer: bool | None = None  # None = 大型モデルを 8〜10GB GPU で学習するとき自動で CPU オフロード
    from_adapter: str | None = None
    roles: list[str] | None = None
    name: str | None = None
    quantize: str = "q8_0"
    seed: int = 0
    profile: str | None = None
    specialties: list[str] | None = None
    can_manage: bool = False
    max_per_role: int = 160
    thinking: bool = False


def config_for_profile(profile: str | None = None, **overrides) -> TrainConfig:
    """プロファイル(nano/qwen3-nano/fast/qwen3/pro/coder/reason/lingual/agent/judge)から設定を組み立てる。"""
    kw: dict = {}
    if profile:
        spec = OWN_PROFILES.get(profile)
        if spec is None:
            raise TrainError(f"未知のプロファイル: {profile} (選択肢: {', '.join(OWN_PROFILES)})")
        kw.update({
            "profile": profile,
            "base": spec["base"],
            "name": spec["default_name"],
            "roles": list(spec["roles"]) if spec["roles"] is not None else None,
            "specialties": list(spec["specialties"]),
            "can_manage": bool(spec["can_manage"]),
            "epochs": int(spec["epochs"]),
            "lr": float(spec["lr"]),
            "kl_beta": float(spec["kl_beta"]),
            "max_tokens": int(spec["max_tokens"]),
        })
    for k, v in overrides.items():
        if v is not None:
            kw[k] = v
    return TrainConfig(**kw)


def recommended_base() -> str:
    """利用可能な GPU VRAM に応じて、実用速度と精度のバランスが良い基礎モデル名を返す。"""
    try:
        import torch

        if torch.cuda.is_available():
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
            if vram_gb >= 5.5:
                return MODEL_PRESETS["1.5b"]["base"]
    except ImportError:
        pass
    return MODEL_PRESETS["0.5b"]["base"]


def resolve_base(base_or_preset: str | None) -> str:
    """'auto' やプリセット名('1.5b'等)を HuggingFace モデルID に解決する。"""
    if not base_or_preset or base_or_preset == "auto":
        return recommended_base()
    if base_or_preset in MODEL_PRESETS:
        return MODEL_PRESETS[base_or_preset]["base"]
    return base_or_preset


def _row_get(row, key: str, default=None):
    return row[key] if (isinstance(row, dict) and key in row) or (hasattr(row, "keys") and key in row.keys()) else default


def training_examples(store, *, clean: bool = True, roles: list[str] | None = None) -> list[dict]:
    """学習データ = 悪評価を除いた実行履歴 + 自習の成果。良評価・検算済みには高い重みを付ける。"""
    examples: dict[str, dict] = {}
    role_filter = set(roles) if roles else None
    for r in store.study_rows():
        role = r["role"]
        if role not in _TRAIN_ROLES or (role_filter and role not in role_filter):
            continue
        ans = clean_answer(role, r["answer"]) if clean else r["answer"]
        if not ans or not answer_ok(ans):
            continue
        ok, _ = verify_answer(role, r["task"], ans)
        if not ok:
            continue
        verified = bool(_row_get(r, "verified", 0))
        examples[task_hash(r["task"])] = {
            "role": role, "task": r["task"], "answer": ans, "weight": 1.4 if verified else 0.85,
        }
    for r in store.export_rows():
        role = r["role"]
        if role not in _TRAIN_ROLES or (role_filter and role not in role_filter):
            continue
        ans = clean_answer(role, r["answer"]) if clean else r["answer"]
        if not ans or ans.startswith(("【注意】", "申し訳ありませんが")) or not answer_ok(ans):
            continue
        ok, _ = verify_answer(role, r["task"], ans)
        if not ok:
            continue
        fb = _row_get(r, "feedback", None)
        ver = _row_get(r, "verified", 0)
        weight = 1.5 if fb == 1 else (1.2 if ver == 1 else 0.95)
        examples[task_hash(r["task"])] = {
            "role": role, "task": r["task"], "answer": ans, "weight": weight,
        }
    return list(examples.values())


def balance_examples(examples: list[dict], *, max_per_role: int = 160,
                     specialties: list[str] | None = None, seed: int = 0) -> list[dict]:
    """特定役割への過度な偏りを抑えつつ、得意役割(specialties)と検算済み例を優先保持する。"""
    spec = set(specialties or ())
    rng = random.Random(seed)
    by_role: dict[str, list[dict]] = {}
    for ex in examples:
        item = dict(ex)
        if item["role"] in spec:
            item["weight"] = float(item["weight"]) * 1.35
        by_role.setdefault(item["role"], []).append(item)
    out: list[dict] = []
    for role_name, bucket in sorted(by_role.items()):
        cap = int(max_per_role * 1.5) if (max_per_role > 0 and role_name in spec) else max_per_role
        rng.shuffle(bucket)
        bucket.sort(key=lambda x: -float(x["weight"]))
        out.extend(bucket[:cap] if cap > 0 else bucket)
    return out


def libraries_available() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "peft"))


def _has_japanese(text: str) -> bool:
    return any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in text)


def _sys_prompt(role_name: str, task: str, *, match_ja_hint: bool) -> str:
    sys_text = ROLES[role_name].system if role_name in ROLES else EXTRA_TRAIN_SYSTEMS[role_name]
    if match_ja_hint and _has_japanese(task):
        sys_text += "\n必ず自然な日本語で答えてください。"
    return sys_text


def _next_name(models_dir: Path) -> str:
    versions = [int(m["name"].rsplit("-v", 1)[1]) for m in read_models(models_dir)
                if m["name"].startswith("celestial-v") and m["name"].rsplit("-v", 1)[1].isdigit()]
    return f"celestial-v{max(versions, default=0) + 1}"


def delete_own_model(app, name: str) -> dict:
    """自作AIを削除する(Ollama 側のモデルとローカルフォルダの両方)。"""
    from .ollama_export import remove_from_ollama

    models_dir = app.cfg.home / "models"
    meta = next((m for m in read_models(models_dir) if m["name"] == name), None)
    if meta is None:
        raise TrainError(f"自作AI {name} が見つかりません")
    if meta.get("ollama"):
        remove_from_ollama(meta["ollama"])
    target = Path(meta["path"])
    if target.is_dir():
        shutil.rmtree(target)
    app.models(refresh=True)
    return {"deleted": name, "ollama": meta.get("ollama")}


def finish_model(app, name: str, *, export: bool = True, run_code: bool = False,
                 quantize: str | None = None, on_progress: Progress | None = None) -> dict:
    """学習後の仕上げ: Ollama へ書き出し(可能なら)、精度を計測する。基礎モデルも未計測・更新時なら計測する。"""
    from .bench import load_tasks, run_bench, suite_id
    from .ollama_export import ExportError, export_to_ollama, ollama_available

    def report(done: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(done, total, message)

    models_dir = app.cfg.home / "models"
    tasks = load_tasks()
    suite = suite_id([t for t in tasks if run_code or "tests" not in t["check"]])
    history = app.store.bench_history()
    latest_ts: dict[tuple[str, str], float] = {}
    for r in history:
        key = (r["label"], r["suite"])
        latest_ts[key] = max(latest_ts.get(key, 0.0), float(r["ts"]))
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
                q = quantize or meta.get("quantize") or "q8_0"
                final = export_to_ollama(models_dir, own, quantize=q)
            except ExportError as e:
                result["export_error"] = str(e)
        final_names[own] = final
    app.models(refresh=True)
    for own, final in final_names.items():
        if own == "celestial-base":
            meta = next((m for m in read_models(models_dir) if m["name"] == "celestial-base"), {})
            prev_ts = latest_ts.get((final, suite))
            if prev_ts is not None and prev_ts >= float(meta.get("created", 0.0)):
                continue
        bench = run_bench(app, tasks, model=final, run_code=run_code,
                          on_outcome=lambda o, i, total, final=final: report(i, total, f"{final} の精度を計測中"))
        result["base_report" if own == "celestial-base" else "report"] = bench
    result["model"] = final_names.get(name, name)
    return result


def train(app, config: TrainConfig | None = None, *, on_progress: Progress | None = None) -> dict:
    """学習して保存し、モデルの情報(celestial_model.json の内容)を返す。

    GPU メモリが足りなければ、一度に処理する量(バッチのトークン数)を半分にして自動でやり直す。
    """
    cfg = config or TrainConfig()
    cfg = replace(cfg, base=resolve_base(cfg.base))
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
        if cfg.batch_tokens <= 256:
            raise TrainError("GPU メモリが足りず学習できませんでした。他のアプリを閉じるか、より小さい基礎モデルを指定してください。")
        cfg = replace(cfg, batch_tokens=cfg.batch_tokens // 2)
        if on_progress:
            on_progress(0, 1, f"GPU メモリが足りないため、バッチを {cfg.batch_tokens} トークンに減らしてやり直します")


def _stratified_split(encoded: list[tuple[str, list[int], list[int], float]], rng: random.Random):
    """役割の偏りが無いように評価用データを約10%取り分ける。"""
    if len(encoded) < 20:
        return [], [(r, ids, lab, w) for r, ids, lab, w in encoded]
    by_role: dict[str, list] = {}
    for item in encoded:
        by_role.setdefault(item[0], []).append(item)
    eval_set, train_set = [], []
    for items in by_role.values():
        rng.shuffle(items)
        k = max(1, len(items) // 10) if len(items) >= 4 else 0
        eval_set.extend(items[:k])
        train_set.extend(items[k:])
    if not eval_set:
        rng.shuffle(train_set)
        k = max(2, len(train_set) // 10)
        eval_set, train_set = train_set[:k], train_set[k:]
    return eval_set, train_set


def _build_lora_optimizer(torch_mod, model, *, lr: float, weight_decay: float, loraplus_ratio: float, offload: bool):
    """LoRA+ (lora_B の学習率を拡大) と、必要に応じた CPU fp32 AdamW オフロードを構築する。"""
    pairs = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
    params_a = [p for n, p in pairs if "lora_b" not in n.lower()]
    params_b = [p for n, p in pairs if "lora_b" in n.lower()]
    ratio = max(1.0, float(loraplus_ratio))

    def _groups(items_a, items_b):
        groups = []
        if items_a:
            groups.append({"params": items_a, "lr": lr, "weight_decay": weight_decay})
        if items_b:
            groups.append({"params": items_b, "lr": lr * ratio, "weight_decay": weight_decay})
        return groups

    gpu_params = [p for _, p in pairs]
    if not offload:
        return gpu_params, torch_mod.optim.AdamW(_groups(params_a, params_b)), None

    cpu_masters = [p.detach().to("cpu", dtype=torch_mod.float32).clone().requires_grad_(True) for p in gpu_params]
    cpu_a = [m for (n, _), m in zip(pairs, cpu_masters) if "lora_b" not in n.lower()]
    cpu_b = [m for (n, _), m in zip(pairs, cpu_masters) if "lora_b" in n.lower()]
    opt = torch_mod.optim.AdamW(_groups(cpu_a, cpu_b))

    def _step():
        for gp, cp in zip(gpu_params, cpu_masters):
            if gp.grad is not None:
                cp.grad = gp.grad.detach().to("cpu", dtype=torch_mod.float32)
        opt.step()
        with torch_mod.no_grad():
            for gp, cp in zip(gpu_params, cpu_masters):
                gp.copy_(cp.data.to(device=gp.device, dtype=gp.dtype))
        opt.zero_grad(set_to_none=True)
        for gp in gpu_params:
            gp.grad = None

    return gpu_params, opt, _step


def _load_pref_pairs(store, tokenizer, cfg: TrainConfig) -> list[tuple]:
    """DPO 選好学習用の (chosen_ids, chosen_labels, rejected_ids, rejected_labels) を作る。"""
    raw = list(store.preference_rows()) + list(store.interaction_preferences())
    if cfg.roles:
        allowed = set(cfg.roles)
        raw = [r for r in raw if r["role"] in allowed]
    seen, pairs = set(), []
    for r in raw:
        role = r["role"]
        if role not in _TRAIN_ROLES:
            continue
        chosen = clean_answer(role, r["chosen"]) if cfg.clean_answers else r["chosen"].strip()
        rejected = r["rejected"].strip()
        if not chosen or not rejected or chosen == rejected or not answer_ok(chosen):
            continue
        ok, ver = verify_answer(role, r["task"], chosen)
        if not ok or (role == "reason" and len(chosen) > 180 and not ver):
            continue
        key = (task_hash(r["task"]), chosen, rejected)
        if key in seen:
            continue
        seen.add(key)
        sys_txt = _sys_prompt(role, r["task"], match_ja_hint=cfg.match_ja_hint)
        msgs = [{"role": "system", "content": sys_txt}, {"role": "user", "content": r["task"]}]
        prompt = render_chat(tokenizer, msgs, add_generation_prompt=True, thinking=cfg.thinking)
        full_c = render_chat(tokenizer, msgs + [{"role": "assistant", "content": chosen}], thinking=cfg.thinking)
        full_r = render_chat(tokenizer, msgs + [{"role": "assistant", "content": rejected}], thinking=cfg.thinking)
        if not (full_c.startswith(prompt) and full_r.startswith(prompt)):
            continue
        p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        c_ids = tokenizer(full_c[len(prompt):], add_special_tokens=False)["input_ids"]
        r_ids = tokenizer(full_r[len(prompt):], add_special_tokens=False)["input_ids"]
        if c_ids and r_ids and len(p_ids) + max(len(c_ids), len(r_ids)) <= cfg.max_tokens:
            pairs.append((p_ids + c_ids, [-100] * len(p_ids) + c_ids,
                          p_ids + r_ids, [-100] * len(p_ids) + r_ids))
    return pairs


def _train_once(app, cfg: TrainConfig, on_progress: Progress | None) -> dict:
    raw_examples = training_examples(app.store, clean=cfg.clean_answers, roles=cfg.roles)
    examples = balance_examples(raw_examples, max_per_role=cfg.max_per_role, specialties=cfg.specialties, seed=cfg.seed)
    if len(examples) < 8:
        raise TrainError(f"学習データが足りません(現在 {len(examples)} 件、最低 8 件)。「自習」で増やせます。")
    try:
        import torch
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        raise TrainError(INSTALL_HINT) from e
    from transformers.utils import logging as hf_logging

    hf_logging.disable_progress_bar()

    def report(step: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(step, total, message)

    models_dir = app.cfg.home / "models"
    from_meta = None
    if cfg.from_adapter:
        from_meta = next((m for m in read_models(models_dir) if m["name"] == cfg.from_adapter), None)
        if from_meta is None:
            raise TrainError(f"継続学習元の自作AI {cfg.from_adapter} が見つかりません")
        if cfg.base == TrainConfig.base and from_meta.get("base"):
            cfg = replace(cfg, base=from_meta["base"])

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
    n_base_params = sum(p.numel() for p in model.parameters())
    vram_gb = (torch.cuda.get_device_properties(0).total_memory / 1e9) if device == "cuda" else 0.0
    offload = cfg.offload_optimizer if cfg.offload_optimizer is not None else (
        device == "cuda" and n_base_params >= 2_200_000_000 and vram_gb < 11.0
    )
    eff_batch_tokens = min(cfg.batch_tokens, 512) if (device == "cuda" and n_base_params >= 2_200_000_000 and vram_gb < 10.5) else cfg.batch_tokens
    eff_accum = cfg.grad_accum * 2 if (eff_batch_tokens < cfg.batch_tokens and cfg.grad_accum <= 4) else cfg.grad_accum
    lm_step_sz = 64 if (device == "cuda" and n_base_params >= 2_200_000_000 and vram_gb < 11.0) else 128

    # 質問部分は損失から外し、回答部分だけを学習させる(推論時と同じ日本語ヒントを合わせる)
    encoded = []
    for ex in examples:
        messages = [
            {"role": "system", "content": _sys_prompt(ex["role"], ex["task"], match_ja_hint=cfg.match_ja_hint)},
            {"role": "user", "content": ex["task"]},
        ]
        prompt = render_chat(tokenizer, messages, add_generation_prompt=True, thinking=cfg.thinking)
        full = render_chat(tokenizer, messages + [{"role": "assistant", "content": ex["answer"]}], thinking=cfg.thinking)
        if not full.startswith(prompt):
            continue
        prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        answer_ids = tokenizer(full[len(prompt):], add_special_tokens=False)["input_ids"]
        if answer_ids and len(prompt_ids) + len(answer_ids) <= cfg.max_tokens:
            encoded.append((ex["role"], prompt_ids + answer_ids, [-100] * len(prompt_ids) + answer_ids, float(ex["weight"])))
    if len(encoded) < 8:
        raise TrainError(f"使える学習データが足りません({len(encoded)} 件)。")

    rng = random.Random(cfg.seed)
    eval_set, train_set = _stratified_split(encoded, rng)

    def batches(data: list, shuffle: bool) -> list:
        data = sorted(data, key=lambda e: len(e[1]))  # 長さの近い例をまとめて詰め物を減らす
        chunks, current = [], []
        for item in data:
            if current and (len(current) >= cfg.batch_size or (len(current) + 1) * len(item[1]) > eff_batch_tokens):
                chunks.append(current)
                current = []
            current.append(item)
        if current:
            chunks.append(current)
        if shuffle:
            rng.shuffle(chunks)
        return chunks

    def to_tensors(chunk: list, *, weighted: bool):
        width = max(len(item[1]) for item in chunk)
        ids = torch.full((len(chunk), width), pad_id, dtype=torch.long)
        labels = torch.full((len(chunk), width), -100, dtype=torch.long)
        mask = torch.zeros((len(chunk), width), dtype=torch.long)
        weights = torch.ones((len(chunk), width), dtype=torch.float32)
        for i, (_role, row, lab, w) in enumerate(chunk):
            ids[i, : len(row)] = torch.tensor(row)
            labels[i, : len(lab)] = torch.tensor(lab)
            mask[i, : len(row)] = 1
            if weighted:
                ans_len = max(1, sum(1 for x in lab[1:] if x != -100))
                scale = w / (math.sqrt(ans_len) if cfg.length_balance else 1.0)
                weights[i, : len(lab)] = scale
        return ids.to(device), mask.to(device), labels.to(device), weights.to(device)

    core = model  # LoRA が差し込まれても元の基礎構造(base_model / lm_head)へ直接触れられる
    neftune_active = [False]

    def _slice_loss(h_stu, h_ref, tgt, w, kl_beta: float):
        with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
            logits_s = core.get_output_embeddings()(h_stu).float()
        loss_vec = torch.nn.functional.cross_entropy(logits_s, tgt, reduction="none") * w
        if h_ref is not None and kl_beta > 0:
            with torch.no_grad(), torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
                ref_lp = torch.log_softmax(core.get_output_embeddings()(h_ref).float(), dim=-1)
            stu_lp = torch.log_softmax(logits_s, dim=-1)
            kl_vec = torch.nn.functional.kl_div(stu_lp, ref_lp, log_target=True, reduction="none").sum(dim=-1)
            loss_vec = loss_vec + kl_beta * kl_vec * w
        return loss_vec.sum()

    def forward(chunk: list, *, training: bool, use_kl: bool = False):
        ids, mask, labels, w_mat = to_tensors(chunk, weighted=training)
        targets = labels[:, 1:]
        keep = targets != -100
        flat_tgt = targets[keep]
        flat_w = w_mat[:, 1:][keep] if training else torch.ones_like(flat_tgt, dtype=torch.float32)
        if training:
            flat_w = flat_w / flat_w.mean().clamp(min=1e-6)

        ref_h = None
        if use_kl and cfg.kl_beta > 0:
            was_train = model.training
            prev_nef = neftune_active[0]
            neftune_active[0] = False
            model.eval()
            with torch.no_grad(), model.disable_adapter(), torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
                ref_h = core.base_model(input_ids=ids, attention_mask=mask).last_hidden_state[:, :-1][keep]
            neftune_active[0] = prev_nef
            if was_train:
                model.train()

        with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
            stu_h = core.base_model(input_ids=ids, attention_mask=mask).last_hidden_state[:, :-1][keep]

        # 語彙約15万語への射影は128トークン単位で計算+チェックポイントし、VRAM ピークを抑える
        total = None
        step_sz = lm_step_sz
        kl_val = cfg.kl_beta if (ref_h is not None) else 0.0
        for s in range(0, flat_tgt.numel(), step_sz):
            e = min(flat_tgt.numel(), s + step_sz)
            hs = stu_h[s:e]
            hr = ref_h[s:e] if ref_h is not None else None
            ts = flat_tgt[s:e]
            ws = flat_w[s:e]
            if training and torch.is_grad_enabled() and hs.requires_grad:
                if hr is not None:
                    part = torch.utils.checkpoint.checkpoint(
                        lambda a, b, c, d: _slice_loss(a, b, c, d, kl_val),
                        hs, hr, ts, ws, use_reentrant=False,
                    )
                else:
                    part = torch.utils.checkpoint.checkpoint(
                        lambda a, c, d: _slice_loss(a, None, c, d, 0.0),
                        hs, ts, ws, use_reentrant=False,
                    )
            else:
                part = _slice_loss(hs, hr, ts, ws, kl_val)
            total = part if total is None else total + part
        return total / flat_w.sum().clamp(min=1e-6)

    def evaluate() -> float | None:
        if not eval_set:
            return None
        prev_nef = neftune_active[0]
        neftune_active[0] = False
        model.eval()
        with torch.no_grad():
            losses = [forward(chunk, training=False, use_kl=False).item() for chunk in batches(eval_set, False)]
        model.train()
        neftune_active[0] = prev_nef
        return sum(losses) / len(losses)

    base_eval = evaluate()

    use_gc = cfg.gradient_checkpointing if cfg.gradient_checkpointing is not None else (n_base_params >= 900_000_000)
    if use_gc:
        core.config.use_cache = False
        core.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})

    if from_meta is not None and from_meta.get("adapter", True):
        model = PeftModel.from_pretrained(core, from_meta["path"], is_trainable=True)
    else:
        model = get_peft_model(core, LoraConfig(
            task_type="CAUSAL_LM", r=cfg.rank, lora_alpha=cfg.rank * 2, lora_dropout=0.05,
            use_rslora=cfg.use_rslora,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        ))
    if use_gc:
        model.enable_input_require_grads()

    def _neftune_hook(_mod, _inp, out):
        if neftune_active[0] and model.training and cfg.neftune_alpha > 0:
            dims = torch.tensor(out.size(1) * out.size(2), device=out.device, dtype=out.dtype)
            mag = cfg.neftune_alpha / torch.sqrt(dims.clamp(min=1.0))
            return out + torch.empty_like(out).uniform_(-1.0, 1.0) * mag
        return out

    hook_handle = model.get_input_embeddings().register_forward_hook(_neftune_hook)

    try:
        params, optimizer, sft_step = _build_lora_optimizer(
            torch, model, lr=cfg.lr, weight_decay=cfg.weight_decay,
            loraplus_ratio=cfg.loraplus_ratio, offload=offload,
        )
        accum = max(1, eff_accum)
        n_macros_per_epoch = math.ceil(len(batches(train_set, False)) / accum)
        total_steps = max(1, cfg.epochs * n_macros_per_epoch)
        warmup = max(1, total_steps // 15)
        schedule = torch.optim.lr_scheduler.LambdaLR(
            optimizer,
            lambda s: (s + 1) / warmup if s < warmup else 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(1, total_steps - warmup))),
        )

        model.train()
        neftune_active[0] = True
        step, last_loss, eval_loss = 0, 0.0, None
        best_loss, best_state, best_epoch, eval_history = base_eval, None, 0, []
        for epoch in range(cfg.epochs):
            chunks = batches(train_set, True)
            macros = [chunks[i : i + accum] for i in range(0, len(chunks), accum)]
            epoch_losses = []
            for macro in macros:
                macro_loss = 0.0
                for chunk in macro:
                    loss = forward(chunk, training=True, use_kl=(cfg.kl_beta > 0))
                    if not torch.isfinite(loss):
                        raise TrainError("学習が発散しました(損失が数値でなくなりました)。学習率を下げて試してください。")
                    macro_loss += loss.item()
                    (loss / len(macro)).backward()
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                if sft_step is not None:
                    sft_step()
                else:
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                schedule.step()
                step += 1
                if device == "cuda" and step % 4 == 0:
                    torch.cuda.empty_cache()
                avg_m = macro_loss / len(macro)
                epoch_losses.append(avg_m)
                report(step, total_steps, f"学習中 {epoch + 1}/{cfg.epochs} 周目  損失 {avg_m:.3f}")
            last_loss = sum(epoch_losses) / max(1, len(epoch_losses))
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

        # 比較ペアがあれば DPO 選好学習を追加実施(基礎モデルとのログ確率比で良回答を強化、悪回答を抑制)
        neftune_active[0] = False
        pref_pairs = _load_pref_pairs(app.store, tokenizer, cfg) if (cfg.pref_epochs > 0 and cfg.pref_beta > 0) else []
        dpo_used = 0
        if pref_pairs:
            optimizer = None
            sft_step = None
            gc.collect()
            if device == "cuda":
                torch.cuda.empty_cache()
            pre_dpo_state = {n: p.detach().to("cpu", copy=True) for n, p in model.named_parameters() if p.requires_grad}
            pre_dpo_eval = eval_loss
            dpo_params, dpo_opt, dpo_step = _build_lora_optimizer(
                torch, model, lr=min(cfg.lr * 0.15, 1.5e-5),
                weight_decay=cfg.weight_decay, loraplus_ratio=1.0, offload=offload,
            )

            def _seq_logp(seq_ids: list[int], seq_lab: list[int], *, use_adapter: bool):
                t_ids = torch.tensor([seq_ids], dtype=torch.long, device=device)
                t_mask = torch.ones_like(t_ids)
                t_lab = torch.tensor([seq_lab], dtype=torch.long, device=device)
                tgt = t_lab[:, 1:]
                kp = tgt != -100
                ctx = torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp)
                if use_adapter:
                    with ctx:
                        h = core.base_model(input_ids=t_ids, attention_mask=t_mask).last_hidden_state[:, :-1][kp]
                        logits = core.get_output_embeddings()(h).float()
                else:
                    was_tr = model.training
                    model.eval()
                    with torch.no_grad(), model.disable_adapter(), ctx:
                        h = core.base_model(input_ids=t_ids, attention_mask=t_mask).last_hidden_state[:, :-1][kp]
                        logits = core.get_output_embeddings()(h).float()
                    if was_tr:
                        model.train()
                lp = torch.log_softmax(logits, dim=-1).gather(1, tgt[kp].unsqueeze(1)).squeeze(1)
                return lp.sum(), max(1, int(tgt[kp].numel()))

            model.train()
            dpo_steps = cfg.pref_epochs * len(pref_pairs)
            for ep in range(cfg.pref_epochs):
                rng.shuffle(pref_pairs)
                for pi, (c_ids, c_lab, r_ids, r_lab) in enumerate(pref_pairs, 1):
                    ref_c, len_c = _seq_logp(c_ids, c_lab, use_adapter=False)
                    ref_r, _ = _seq_logp(r_ids, r_lab, use_adapter=False)
                    pol_c, _ = _seq_logp(c_ids, c_lab, use_adapter=True)
                    pol_r, _ = _seq_logp(r_ids, r_lab, use_adapter=True)
                    diff = cfg.pref_beta * ((pol_c - ref_c) - (pol_r - ref_r))
                    dpo_loss = -torch.nn.functional.logsigmoid(diff) + 0.1 * (-pol_c / len_c)
                    if torch.isfinite(dpo_loss):
                        (dpo_loss / accum).backward()
                        dpo_used += 1
                    if pi % accum == 0 or pi == len(pref_pairs):
                        torch.nn.utils.clip_grad_norm_(dpo_params, 1.0)
                        if dpo_step is not None:
                            dpo_step()
                        else:
                            dpo_opt.step()
                            dpo_opt.zero_grad(set_to_none=True)
                    report(ep * len(pref_pairs) + pi, dpo_steps, f"選好学習(DPO)中 {pi}/{len(pref_pairs)}")
            post_dpo_eval = evaluate()
            if post_dpo_eval is not None and pre_dpo_eval is not None and post_dpo_eval > pre_dpo_eval + 0.015:
                with torch.no_grad():
                    for n, p in model.named_parameters():
                        if n in pre_dpo_state:
                            p.copy_(pre_dpo_state[n])
            elif post_dpo_eval is not None:
                eval_loss = min(eval_loss or post_dpo_eval, post_dpo_eval)
            del dpo_opt
    finally:
        hook_handle.remove()

    name = cfg.name or _next_name(models_dir)
    out_dir = models_dir / name
    out_dir.mkdir(parents=True, exist_ok=True)
    report(1, 1, "保存中")
    model.save_pretrained(str(out_dir))
    n_params = sum(p.numel() for p in model.parameters())
    tier = "medium" if n_base_params >= 2_500_000_000 else "small"
    meta = {
        "name": name, "base": cfg.base, "tier": tier, "quantize": cfg.quantize,
        "profile": cfg.profile or "", "specialties": list(cfg.specialties or []),
        "can_manage": bool(cfg.can_manage), "thinking": bool(cfg.thinking),
        "created": time.time(), "examples": len(train_set), "eval_examples": len(eval_set),
        "preference_steps": dpo_used, "train_loss": round(last_loss, 4),
        "eval_loss": None if eval_loss is None else round(eval_loss, 4),
        "base_eval_loss": None if base_eval is None else round(base_eval, 4),
        "eval_history": eval_history, "best_epoch": best_epoch or cfg.epochs,
        "seconds": round(time.perf_counter() - started, 1), "size_bytes": n_params * 2,
        "config": asdict(cfg),
    }
    (out_dir / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 学習前の基礎モデルも「celestial-base」として登録し、学習の効果を計測で比べられるようにする
    base_dir = models_dir / "celestial-base"
    base_meta_path = base_dir / META_FILE
    current = json.loads(base_meta_path.read_text(encoding="utf-8")) if base_meta_path.is_file() else {}
    if current.get("base") != cfg.base or ("ollama" in current and not current.get("quantize")):
        base_dir.mkdir(parents=True, exist_ok=True)
        base_meta_path.write_text(json.dumps({
            "name": "celestial-base", "base": cfg.base, "tier": tier, "quantize": cfg.quantize,
            "adapter": False, "created": time.time(), "examples": 0,
            "eval_loss": meta["base_eval_loss"], "base_eval_loss": meta["base_eval_loss"],
            "seconds": 0, "size_bytes": n_params * 2,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    del model, optimizer
    if device == "cuda":
        torch.cuda.empty_cache()
    app.models(refresh=True)
    return meta


def evolve_own_ai(
    app,
    *,
    config: TrainConfig | None = None,
    profile: str | None = None,
    study_per_role: int = 8,
    synthetic: int = 40,
    wiki: int = 24,
    review: bool = True,
    run_code: bool = True,
    on_progress: Progress | None = None,
) -> dict:
    """診断 → 自習データの見直し → 苦手分野中心の自習/検算問題生成 → 学習 → 書き出し・計測を一気通貫で行う。"""
    from .study import collect_contrastive_preferences, generate_curriculum_examples, review_study, run_study, weak_roles

    def report(done: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(done, total, message)

    cfg = config or (config_for_profile(profile) if profile else TrainConfig(base=recommended_base()))
    cfg = replace(cfg, base=resolve_base(cfg.base))

    review_res = None
    if review and app.store.study_rows():
        try:
            review_res = review_study(app, on_progress=on_progress)
        except BackendError:
            review_res = {"checked": 0, "removed": 0, "cleaned": 0, "skipped": True}

    study_res = None
    focus_roles = [r for r in (cfg.roles or weak_roles(app)[:4]) if r in ROLES] or weak_roles(app)[:4]
    cur_roles = cfg.roles
    if study_per_role > 0:
        try:
            study_res = run_study(
                app, per_role=study_per_role, roles=focus_roles,
                synthetic=synthetic, wiki=wiki, verify_llm=True, contrastive=True, on_progress=on_progress,
            )
        except BackendError:
            if synthetic > 0:
                cur = generate_curriculum_examples(app, count=synthetic, seed=cfg.seed, roles=cur_roles)
                study_res = {"added": 0, "synthetic_added": cur["added"], "by_role": cur["by_role"], "skipped_llm": True}
    elif synthetic > 0:
        report(0, 1, "検算済みの基礎問題を追加中")
        cur = generate_curriculum_examples(app, count=synthetic, seed=cfg.seed, roles=cur_roles)
        study_res = {"added": 0, "synthetic_added": cur["added"], "by_role": cur["by_role"]}

    try:
        collect_contrastive_preferences(app, roles=cfg.roles, limit=48, seed=cfg.seed, on_progress=on_progress)
    except BackendError:
        pass

    meta = train(app, cfg, on_progress=on_progress)
    done = finish_model(app, meta["name"], run_code=run_code, quantize=cfg.quantize, on_progress=on_progress)
    base = done["base_report"]
    return {
        **meta,
        "model": done["model"],
        "export_error": done["export_error"],
        "bench_correct": done["report"].correct,
        "bench_n": done["report"].n,
        "base_correct": base.correct if base else None,
        "base_n": base.n if base else None,
        "focus_roles": focus_roles,
        "review": review_res,
        "study": study_res,
    }
