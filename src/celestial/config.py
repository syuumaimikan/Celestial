from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("CELESTIAL_HOME", Path.home() / ".celestial")))
    backends: dict[str, dict] = field(
        default_factory=lambda: {"ollama": {"type": "ollama", "host": "http://127.0.0.1:11434"},
                                 "native": {"type": "native"}}
    )
    # 明示指定されたモデル。空なら各バックエンドから自動検出する。
    models: list[dict] = field(default_factory=list)
    manager_model: str = "auto"
    auto_title: bool = True            # 最初のやり取りのあと、会話に内容を表す短い題名を AI で付ける
    instructions: str = ""             # AIへの指示(すべての回答で守ってほしいこと)。画面の設定で変える
    memories: list[str] = field(default_factory=list)  # 覚えていること(「覚えて: …」で増える)
    memory_suggest: bool = True        # 発言に利用者についての事実があれば「覚えますか」と提案する(判断の機能で見分ける)

    cache: bool = True
    no_cache_roles: tuple[str, ...] = ("write",)
    learned_routing: bool = True
    route_confidence: float = 0.85
    fewshot_k: int = 2
    fewshot_min_sim: float = 0.35
    semantic_search: bool = True       # 埋め込みモデルがあれば、手本を意味の近さで探す
    embed_model: str = "auto"          # auto = 最初に見つかった埋め込みモデル / off = 使わない
    fewshot_min_sim_semantic: float = 0.65
    use_kb: bool = True                # ローカル知識ベース(RAG)が登録されていれば会話で自動参照する
    explore: float = 0.3
    cascade: bool = True               # 自信のない回答や言語の取り違えは、より正確なAIで答え直す

    workspace: str = ""               # 道具がファイルを扱える作業フォルダ(空なら計算だけ)
    tools_write: bool = False         # 作業フォルダ内に新しいファイルを作ることを許可
    tools_run: bool = False           # AIが書いた Python コードの実行を許可
    tools_pc: bool = False            # PC操作(アプリを開く・入力する・画面を見る)を許可
    tools_net: bool = False           # ネットの利用(Wikipedia・指定ページの読み取り)を許可
    tools_confirm: bool = False       # 影響のある操作の前に、内容を見せて確認する(False = すべて自動。危ない操作だけは止める・確かめる)
    tools_max_steps: int = 30         # エージェントが1つの依頼で使える手数の上限
    # エージェントの権限のモード: read(読み取りのみ) / ask(確認しながら) / edit(編集は自動) / auto(すべて自動) / full(すべて許可)
    agent_mode: str = "auto"
    agent_model: str = "auto"         # エージェントに使うAI(auto = 計測の正確さと速さで選ぶ)
    recent_folders: list[str] = field(default_factory=list)  # 最近エージェントに作業させたフォルダ(選ぶときの候補)

    remote_enabled: bool = True       # Tailscale 経由で、登録したスマホなどからの接続を受け付ける
    remote_control: bool = True       # 登録した端末から、この PC の画面を見たり操作したりできる
    keep_running: bool = False        # 画面のウィンドウを閉じても動き続ける(スマホから使い続けるため)

    swarm_auto: bool = True
    swarm_min_chars: int = 80
    swarm_max_subtasks: int = 4
    swarm_parallel: int = 2


def load_config(path: str | Path | None = None) -> Config:
    """celestial.toml を読む。path 省略時は $CELESTIAL_CONFIG → ./celestial.toml → 既定値。"""
    cfg = Config()
    candidates = [path, os.environ.get("CELESTIAL_CONFIG"), "celestial.toml"]
    file = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
    if file is None:
        return cfg
    data = tomllib.loads(file.read_text(encoding="utf-8"))

    if "home" in data:
        cfg.home = Path(data["home"]).expanduser()
    if "backends" in data:
        cfg.backends = dict(data["backends"])
        cfg.backends.setdefault("native", {"type": "native"})  # 自作AIは常に使えるようにしておく
    cfg.models = data.get("models", [])
    cfg.manager_model = data.get("manager", {}).get("model", cfg.manager_model)
    cfg.auto_title = bool(data.get("manager", {}).get("auto_title", cfg.auto_title))

    ev = data.get("evolve", {})
    for key in ("cache", "learned_routing", "route_confidence", "fewshot_k", "fewshot_min_sim", "explore",
                "semantic_search", "embed_model", "fewshot_min_sim_semantic", "use_kb", "cascade"):
        if key in ev:
            setattr(cfg, key, ev[key])
    if "no_cache_roles" in ev:
        cfg.no_cache_roles = tuple(ev["no_cache_roles"])

    tl = data.get("tools", {})
    for key, attr in (("workspace", "workspace"), ("write", "tools_write"), ("run", "tools_run"), ("max_steps", "tools_max_steps"),
                      ("pc", "tools_pc"), ("net", "tools_net"), ("confirm", "tools_confirm"), ("mode", "agent_mode"),
                      ("agent_model", "agent_model")):
        if key in tl:
            setattr(cfg, attr, tl[key])

    rm = data.get("remote", {})
    for key, attr in (("enabled", "remote_enabled"), ("control", "remote_control"), ("keep_running", "keep_running")):
        if key in rm:
            setattr(cfg, attr, rm[key])

    sw = data.get("swarm", {})
    for key in ("auto", "min_chars", "max_subtasks", "parallel"):
        if key in sw:
            setattr(cfg, f"swarm_{key}", sw[key])
    return cfg
