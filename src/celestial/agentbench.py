"""エージェントの計測: 実際の作業(ファイル・フォルダ・コマンド)を空のフォルダで最後までやらせ、できたものを確かめて採点する。

回答の文章ではなく「結果のファイルが正しいか」で採点するので、できたふりは正解にならない。
モデルごとの成功率と所要時間を記録し、エージェントのAIを自動で選ぶときに使う(manager._pick_agent_model)。
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .tools import python_command, running_processes, stop_named

SUITE = "agent-v1"
LABEL_PREFIX = "agent:"
_PRIMES_50 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]


def _find(ws: Path, name: str) -> Path | None:
    """作業フォルダの中から、その名前のファイルを探す(置き場所の違いは問わない)。"""
    direct = ws / name
    if direct.is_file():
        return direct
    return next((p for p in sorted(ws.rglob(name)) if p.is_file() and ".git" not in p.parts), None)


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)]


def _run(argv: list[str], cwd: Path, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)


# ---- 採点 -------------------------------------------------------------------

def _check_primes(ws: Path, result) -> bool:
    out = _find(ws, "primes.txt")
    return _find(ws, "primes.py") is not None and out is not None and [int(n) for n in _numbers(out.read_text("utf-8", "replace"))] == _PRIMES_50


def _check_notes(ws: Path, result) -> bool:
    note = ws / "notes" / "todo.md"
    if not note.is_file():
        return False
    items = [line for line in note.read_text("utf-8", "replace").splitlines() if re.match(r"\s*([-*+]|\d+[.)]|- \[[ x]\])\s+\S", line)]
    return len(items) >= 3


def _check_total(ws: Path, result) -> bool:
    out = _find(ws, "total.txt")
    return out is not None and 4380.0 in _numbers(out.read_text("utf-8", "replace"))


def _check_fix(ws: Path, result) -> bool:
    test = ws / "test_calc.py"
    if not test.is_file() or test.read_text("utf-8") != _TEST_CALC:  # テストを書き換えて通すのは不正解
        return False
    proc = _run(python_command() + ["-m", "unittest", "test_calc"], ws)
    return proc.returncode == 0


def _check_sort(ws: Path, result) -> bool:
    photos = ws / "photos"
    want = {"2023": {"IMG_2023_01.jpg", "IMG_2023_07.jpg"}, "2024": {"IMG_2024_03.jpg", "IMG_2024_11.jpg"}}
    for year, names in want.items():
        folder = photos / year
        if not folder.is_dir() or {p.name for p in folder.iterdir() if p.is_file()} != names:
            return False
    return not any(p.is_file() and p.suffix.lower() == ".jpg" for p in photos.iterdir())


def _check_average(ws: Path, result) -> bool:
    data_file, script = _find(ws, "users.json"), _find(ws, "average.py")
    if data_file is None or script is None:
        return False
    try:
        data = json.loads(data_file.read_text("utf-8"))
    except ValueError:
        return False
    rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), []) if isinstance(data, dict) else []
    ages = [float(r.get("age", r.get("年齢"))) for r in rows if isinstance(r, dict) and isinstance(r.get("age", r.get("年齢")), (int, float))]
    if len(ages) != 3:
        return False
    proc = _run(python_command() + ["-X", "utf8", str(script)], script.parent)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    average = sum(ages) / 3
    return proc.returncode == 0 and any(abs(n - average) < 0.01 for n in _numbers(out))


def _check_git(ws: Path, result) -> bool:
    if not (ws / ".git").is_dir() or not (ws / "README.md").is_file() or shutil.which("git") is None:
        return False
    log = _run(["git", "log", "--oneline"], ws)
    tracked = _run(["git", "ls-files"], ws)
    return log.returncode == 0 and bool(log.stdout.strip()) and b"README.md" in tracked.stdout


def _check_excel(ws: Path, result) -> bool:
    book = _find(ws, "report.xlsx")
    if book is None:
        return False
    try:
        with zipfile.ZipFile(book) as z:
            text = " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.startswith("xl/") and n.endswith(".xml"))
    except (zipfile.BadZipFile, OSError):
        return False
    values = set(_numbers(re.sub(r"<[^>]+>", " ", text)))
    has_total = 383.0 in values or bool(re.search(r"<f>[^<]*SUM\(", text, re.I))
    return {120.0, 135.0, 128.0} <= values and has_total


def _serves_hello(script: Path, port: int) -> bool:
    """できたサーバーを、ここでもう一度動かして、本当に hello を返すかを確かめる(エージェントの報告には頼らない)。"""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    popen = subprocess.Popen(python_command() + ["-X", "utf8", str(script)], cwd=str(script.parent), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and popen.poll() is None:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://localhost:{port}/", timeout=3) as res:
                    return res.status == 200 and b"hello" in res.read(5000).lower()
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                time.sleep(0.3)
        return False
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(popen.pid)], capture_output=True, timeout=15, creationflags=flags)
        else:
            popen.kill()
        popen.wait(timeout=10)


def free_port(preferred: int) -> int:
    """課題で使うポート。いつもの番号が使われていれば(前に残ったサーバーなど)、空いている番号を返す。"""
    import socket
    from .tools import _port_open
    if not _port_open(preferred):
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _check_server(ws: Path, result, port: int = 8931) -> bool:
    script = _find(ws, "server.py")
    if script is None:
        return False
    calls = getattr(result, "tool_calls", None) or []
    checked = any(c.get("ok") and "HTTP 200" in str(c.get("output", "")) for c in calls
                  if c.get("name") in ("check_url", "run_file", "run_command", "start_process"))  # 動かして確かめたか
    return checked and _serves_hello(script, port)


_TEST_CALC = '''import unittest

from calc import add, mul


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mul(self):
        self.assertEqual(mul(4, 5), 20)


if __name__ == "__main__":
    unittest.main()
'''

TASKS: list[dict] = [
    {"id": "primes", "prompt": "1から50までの素数を primes.txt に1行ずつ書き出す Python スクリプト primes.py を作って実行して",
     "check": _check_primes},
    {"id": "notes", "prompt": "notes フォルダを作って、その中に今日やることを3つ箇条書きにした todo.md を作って", "check": _check_notes},
    {"id": "csv_total", "prompt": "sales.csv の売上の合計を計算して、total.txt に合計の数字を書いて",
     "files": {"sales.csv": "月,売上\n4月,1200\n5月,1350\n6月,1280\n7月,550\n"}, "check": _check_total},
    {"id": "fix_bug", "prompt": "test_calc.py のテストが通るように calc.py のバグを直して。テストのファイルは変えないで",
     "files": {"calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n", "test_calc.py": _TEST_CALC},
     "check": _check_fix},
    {"id": "sort_photos", "prompt": "photos フォルダの中の画像を、ファイル名に入っている年ごとに photos/2023 と photos/2024 のフォルダに分けて",
     "files": {"photos/IMG_2023_01.jpg": "", "photos/IMG_2023_07.jpg": "", "photos/IMG_2024_03.jpg": "", "photos/IMG_2024_11.jpg": ""},
     "check": _check_sort},
    {"id": "json_average", "prompt": "users.json に3人分の名前(name)と年齢(age)を書いて、年齢の平均を表示する average.py を作って実行して",
     "check": _check_average},
    {"id": "git_commit", "prompt": "このフォルダを git リポジトリにして、README.md を作って最初のコミットをして", "check": _check_git,
     "needs": "git"},
    {"id": "excel", "prompt": "次の売上を Excel の report.xlsx にまとめて、最後に合計の行を入れて: 4月 120、5月 135、6月 128",
     "check": _check_excel},
    {"id": "web_server", "prompt": "Python の標準ライブラリだけで「hello」と表示する Web サーバー server.py をポート {port} で作って、起動して動くか確かめて",
     "check": _check_server, "port": 8931},
]


@dataclass
class AgentOutcome:
    id: str
    model: str
    ok: bool
    latency_s: float
    steps: int
    answer: str
    error: str = ""


@dataclass
class AgentReport:
    model: str
    outcomes: list[AgentOutcome] = field(default_factory=list)

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
    def avg_latency(self) -> float:
        return sum(o.latency_s for o in self.outcomes) / self.n if self.n else 0.0


def run_agent_bench(app, *, model: str | None = None, only: list[str] | None = None, save: bool = True,
                    on_outcome: Callable[[AgentOutcome, int, int], None] | None = None) -> AgentReport:
    """課題ごとに空のフォルダを用意してエージェントにやらせ、できたものを採点する。model は固定するAI(省略時は自動選択)。"""
    todo = [t for t in TASKS if (not only or t["id"] in only) and (not t.get("needs") or shutil.which(t["needs"]))]
    chosen = model or app._pick_agent_model("code").name
    report = AgentReport(chosen)
    for i, task in enumerate(todo, 1):
        # 片付けに失敗しても(Windows で、動いているプログラムがフォルダをつかんでいる など)計測は続ける
        with tempfile.TemporaryDirectory(prefix=f"celestial-agentbench-{task['id']}-", ignore_cleanup_errors=True) as tmp:
            ws = Path(tmp)
            for rel, text in (task.get("files") or {}).items():
                target = ws / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            port = free_port(task["port"]) if "port" in task else None  # 前に残ったサーバーなどが使っていれば、空いている番号にする
            prompt = task["prompt"].format(port=port) if port else task["prompt"]
            t0 = time.perf_counter()
            result, error = None, ""
            try:
                # 計測の一時フォルダの中だけで動かす(利用者の設定によらず、画面を撮る・キーを打つ・ネットは使わない)
                result = app.run(prompt, tools=True, model=chosen, folder=str(ws), learn=False, use_cache=False,
                                 swarm=False, approve=lambda request: True, sandbox=True)
            except Exception as e:  # 計測は最後まで続ける(失敗も結果として記録する)
                error = f"{type(e).__name__}: {e}"
            latency = time.perf_counter() - t0
            started = time.time() - latency
            for proc in running_processes():  # 「動かし続けて」と残されたものも、計測では止める(次の課題の邪魔をしない)
                if proc["started"] >= started - 1:
                    stop_named(proc["name"])
            try:
                ok = result is not None and bool(task["check"](ws, result, port) if port else task["check"](ws, result))
            except Exception as e:
                ok, error = False, error or f"{type(e).__name__}: {e}"
            outcome = AgentOutcome(task["id"], chosen, ok, latency, len(getattr(result, "tool_calls", None) or []),
                                   getattr(result, "text", "") or "", error)
        report.outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome, i, len(todo))
    if save and report.n:
        total = round(sum(o.latency_s for o in report.outcomes), 3)
        app.store.log_bench(label=LABEL_PREFIX + chosen, suite=SUITE, n=report.n, correct=report.correct, route_n=0, route_correct=0,
                            avg_tokens=0.0, avg_latency_s=report.avg_latency,
                            by_role=json.dumps({"agent": [report.correct, report.n, total]}),
                            items=[{"task_id": o.id, "role": "agent", "model": o.model, "ok": o.ok, "latency_s": o.latency_s}
                                   for o in report.outcomes])
    return report
