"""精度の計測ベンチ。答えの決まったタスク集を自動採点し、結果を履歴に残して推移を追えるようにする。

計測は learn=False で行うため、キャッシュも使わず、学習状態(履歴)も一切変えない。
学習済みの振り分け・モデル選択・手本記憶は読み取りで効くので、「使った結果どれだけ良くなったか」が数字に出る。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .bench_tasks import BUILTIN
from .textsim import normalize

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_FENCE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)```", re.S)


def load_tasks(path: str | Path | None = None) -> list[dict]:
    """タスク集を読む。path は1行1タスクのJSONL(形式は bench_tasks.py と同じ)。省略時は組み込み。"""
    if path is None:
        return list(BUILTIN)
    tasks = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for t in tasks:
        if not {"id", "prompt", "check"} <= t.keys():
            raise ValueError(f"タスクには id / prompt / check が必要です: {t}")
    return tasks


def suite_id(tasks: list[dict]) -> str:
    blob = json.dumps([[t["id"], t["prompt"], t["check"]] for t in tasks], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _check_number(answer: str, expected: float) -> bool:
    lines = [ln for ln in unicodedata.normalize("NFKC", answer).splitlines() if _NUM.search(ln)]
    return bool(lines) and any(abs(x - expected) < 1e-6 for x in _numbers(lines[-1]))


def _check_json(answer: str, expected: dict) -> bool:
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return False
    return isinstance(data, dict) and all(str(data.get(k, "")).strip() == str(v) for k, v in expected.items())


def extract_code(answer: str) -> list[str]:
    """実行を試すコードの候補(全コードブロックの連結 → 最初のブロックだけ → 回答全体)。"""
    blocks = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("", "python", "py", "python3")]
    if not blocks:
        return [answer]
    return ["\n\n".join(blocks)] + ([blocks[0]] if len(blocks) > 1 else [])


def _check_code(answer: str, tests: str, timeout: float = 10) -> bool:
    for code in extract_code(answer):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "candidate.py"
            script.write_text(code + "\n\n" + tests + "\n", encoding="utf-8")
            try:
                proc = subprocess.run([sys.executable, "-I", str(script)], cwd=tmp, timeout=timeout,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode == 0:
                return True
    return False


def check(answer: str, spec: dict, *, run_code: bool = False) -> bool | None:
    """採点する。コード実行が必要なのに許可されていなければ None(対象外)。"""
    if "tests" in spec and not run_code:
        return None
    text = normalize(answer)
    if "number" in spec and not _check_number(answer, float(spec["number"])):
        return False
    if "all" in spec and not all(normalize(w) in text for w in spec["all"]):
        return False
    if "any" in spec and not any(normalize(w) in text for w in spec["any"]):
        return False
    if "none" in spec and any(normalize(w) in text for w in spec["none"]):
        return False
    if "regex" in spec and not re.search(spec["regex"], unicodedata.normalize("NFKC", answer), re.I | re.S):
        return False
    if "json" in spec and not _check_json(answer, spec["json"]):
        return False
    if "max_chars" in spec and len(answer.strip()) > spec["max_chars"]:
        return False
    if "tests" in spec and not _check_code(answer, spec["tests"]):
        return False
    return True


@dataclass
class Outcome:
    id: str
    role: str | None  # 期待される役割
    routed_role: str
    model: str
    ok: bool
    tokens: int
    latency_s: float
    answer: str
    confidence: float | None = None  # 最初に答えたモデルの確からしさ
    escalated: bool = False          # より正確なAIで答え直したか
    grounded: bool = False           # 資料(Wikipedia)で裏付けたか


@dataclass
class Report:
    label: str
    suite: str
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: int = 0

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def correct(self) -> int:
        return sum(o.ok for o in self.outcomes)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def routed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.role]

    @property
    def route_correct(self) -> int:
        return sum(o.role == o.routed_role for o in self.routed)

    @property
    def avg_tokens(self) -> float:
        return sum(o.tokens for o in self.outcomes) / self.n if self.n else 0.0

    @property
    def avg_latency(self) -> float:
        return sum(o.latency_s for o in self.outcomes) / self.n if self.n else 0.0

    def by_role(self) -> dict[str, list]:
        """{役割: [正解数, 件数, 合計秒]}"""
        table: dict[str, list] = {}
        for o in self.outcomes:
            cell = table.setdefault(o.role or "?", [0, 0, 0.0])
            cell[0] += o.ok
            cell[1] += 1
            cell[2] = round(cell[2] + o.latency_s, 3)
        return table

    def to_dict(self) -> dict:
        return {
            "label": self.label, "suite": self.suite, "n": self.n, "correct": self.correct, "accuracy": self.accuracy,
            "route_n": len(self.routed), "route_correct": self.route_correct, "avg_tokens": self.avg_tokens,
            "avg_latency_s": self.avg_latency, "by_role": self.by_role(), "skipped": self.skipped,
            "outcomes": [o.__dict__ for o in self.outcomes],
        }


def run_bench(app, tasks: list[dict], *, model: str | None = None, verify: bool = False, run_code: bool = False,
              label: str | None = None, save: bool = True, on_outcome: Callable[[Outcome, int, int], None] | None = None) -> Report:
    """タスク集を実行して採点する。model を指定すると全ワーカーをそのモデルに固定して比較できる。"""
    todo = [t for t in tasks if run_code or "tests" not in t["check"]]
    report = Report(label or model or "auto", suite_id(todo), skipped=len(tasks) - len(todo))
    for i, task in enumerate(todo, 1):
        result = app.run(task["prompt"], model=model, verify=verify, swarm=False, use_cache=False, learn=False)
        ok = bool(check(result.text, task["check"], run_code=run_code))
        outcome = Outcome(task["id"], task.get("role"), result.role, result.model, ok, result.tokens, result.latency_s, result.text,
                          result.confidence, bool(result.escalated), bool(result.sources))
        report.outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome, i, len(todo))
    if save and report.n:
        app.store.log_bench(label=report.label, suite=report.suite, n=report.n, correct=report.correct,
                            route_n=len(report.routed), route_correct=report.route_correct,
                            avg_tokens=report.avg_tokens, avg_latency_s=report.avg_latency,
                            by_role=json.dumps(report.by_role(), ensure_ascii=False),
                            items=[{"task_id": o.id, "role": o.role, "model": o.model, "ok": o.ok, "confidence": o.confidence,
                                    "latency_s": o.latency_s, "escalated": o.escalated} for o in report.outcomes])
    return report
