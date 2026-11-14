"""Celestial のサーバー。PC の画面と、スマホなど登録した端末からの接続を受け付ける。標準ライブラリのみ。

受け口は2つ:
- 127.0.0.1(このPC自身): 画面用。Host / Origin を確かめて、他のサイトからの呼び出しを拒否する。
- Tailscale のアドレス: 登録した端末用。トークン(Authorization: Bearer ...)が必須。Tailscale が
  つながっている間だけ開く(同じ Wi-Fi の他人や、インターネットからは届かない)。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, office, pc
from .backends import BackendError
from .backends.native import read_models
from .bench import load_tasks, run_bench
from .chats import ChatStore
from .flows import CONDITION_OPS, FLOW_TEMPLATES, STEP_TYPES, TRIGGER_TYPES, FlowEngine, FlowStore, build_flow
from .manager import AGENT_MODES, TUNABLE, Celestial, Result
from . import tools as toolbox
from .qr import qr_svg
from .remote import Devices, is_tailnet, tailscale_status
from .runs import EventBus, RunManager
from .study import review_study, run_study, weak_roles
from .tools import ToolError
from . import vision
from .train import (INSTALL_HINT, MODEL_PRESETS, OWN_PROFILES, TrainConfig, TrainError, config_for_profile,
                    delete_own_model, evolve_own_ai, finish_model, libraries_available, recommended_base,
                    resolve_base, train, training_examples)
from .workers import ROLES

WEB_DIR = Path(__file__).parent / "web"
PROJECT_DIR = Path(__file__).resolve().parents[2]
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}
_ROLE_LABELS = {"general": "一般", "code": "コード", "reason": "計算・推論", "summarize": "要約",
                "translate": "翻訳", "write": "文章作成", "extract": "抽出・整形", "complex": "複合"}
# スマホなど別の端末からは変えられない設定(PC の安全に関わるもの)
SENSITIVE = {"workspace", "tools_write", "tools_run", "tools_pc", "tools_net", "tools_confirm", "remote_enabled", "remote_control",
             "agent_mode", "agent_model", "tools_max_steps", "recent_folders"}
MAX_UPLOAD = 2 * 1024**3


def _result_dict(r: Result) -> dict:
    return {"id": r.id, "text": r.text, "role": r.role, "model": r.model, "source": r.source,
            "route_source": r.route_source, "tokens": r.tokens, "latency_s": r.latency_s,
            "examples_used": r.examples_used,
            "tool_calls": [{k: c.get(k) for k in ("name", "label", "detail", "ok", "thought", "step")} for c in r.tool_calls], "plan": r.plan,
            "subresults": [{"role": s.role, "model": s.model, "tokens": s.tokens, "latency_s": s.latency_s} for s in r.subresults],
            "confidence": None if r.confidence is None else round(r.confidence, 4), "escalated": r.escalated, "sources": r.sources,
            "changes": r.changes}


def _row_tokens(row) -> int:
    return row["prompt_tokens"] + row["completion_tokens"] + row["overhead_tokens"]


def collect_stats(app: Celestial) -> dict:
    rows = app.store.top_level()

    def summary(chunk) -> dict:
        n = len(chunk)
        if not n:
            return {"n": 0, "avg_tokens": 0, "avg_latency_s": 0, "cache_rate": 0, "learned_rate": 0}
        routed = [r for r in chunk if r["route_source"] in ("learned", "manager", "heuristic")]
        return {
            "n": n,
            "avg_tokens": sum(_row_tokens(r) for r in chunk) / n,
            "avg_latency_s": sum(r["latency_s"] for r in chunk) / n,
            "cache_rate": sum(r["source"] == "cache" for r in chunk) / n,
            "learned_rate": sum(r["route_source"] == "learned" for r in routed) / len(routed) if routed else 0,
        }

    half = len(rows) // 2
    rated = Counter(r["feedback"] for r in rows)
    saved_tokens, saved_seconds = app.store.savings()
    return {
        "all": summary(rows),
        "first_half": summary(rows[:half]) if len(rows) >= 10 else None,
        "second_half": summary(rows[half:]) if len(rows) >= 10 else None,
        "feedback": {"good": rated[1], "bad": rated[-1], "none": rated[None]},
        "routes": dict(Counter(r["route_source"] for r in rows)),
        "series": [{"id": r["id"], "tokens": _row_tokens(r), "latency_s": r["latency_s"], "source": r["source"]}
                   for r in rows[-300:]],
        "role_model": [dict(r) for r in app.store.role_model_table()],
        "router_examples": app.router.examples,
        "router_needed": app.router.min_examples,
        "memory_size": sum(len(b) for b in app.memory._by_role.values()),
        "saved_tokens": saved_tokens,
        "saved_seconds": saved_seconds,
    }


class BenchJob:
    """計測は時間がかかるので裏で1件ずつ走らせ、画面からは進み具合を問い合わせる。"""

    def __init__(self, app: Celestial):
        self.app = app
        self.lock = threading.Lock()
        self.state: dict = {"running": False, "done": 0, "total": 0, "label": "", "outcomes": [], "report": None, "error": None}

    def start(self, model: str | None, run_code: bool, verify: bool, kind: str = "answers") -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "done": 0, "total": 0, "label": model or "auto", "kind": kind, "outcomes": [],
                          "report": None, "error": None}
        threading.Thread(target=self._work, args=(model, run_code, verify, kind), daemon=True).start()
        return True

    def _work(self, model, run_code, verify, kind="answers") -> None:
        def progress(outcome, i, total):
            with self.lock:
                self.state["done"], self.state["total"] = i, total
                self.state["outcomes"].append({k: v for k, v in outcome.__dict__.items() if k != "answer"})

        try:
            if kind == "agent":
                from .agentbench import run_agent_bench

                done = run_agent_bench(self.app, model=model, on_outcome=progress)
                summary = {"label": "agent:" + done.model, "n": done.n, "correct": done.correct, "accuracy": done.accuracy,
                           "route_n": 0, "route_correct": 0, "avg_tokens": 0, "avg_latency_s": done.avg_latency,
                           "by_role": {"agent": [done.correct, done.n]}, "skipped": 0}
            else:
                report = run_bench(self.app, load_tasks(), model=model, run_code=run_code, verify=verify, on_outcome=progress)
                summary = report.to_dict()
                del summary["outcomes"]
            with self.lock:
                self.state["report"] = summary
        except Exception as e:  # 裏スレッドなので、何が起きても画面に伝える
            with self.lock:
                self.state["error"] = str(e)
        finally:
            with self.lock:
                self.state["running"] = False

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))


class WorkJob:
    """自習と学習を裏で1件ずつ走らせる。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state: dict = {"running": False, "kind": "", "done": 0, "total": 0, "message": "", "result": None, "error": None}

    def start(self, kind: str, work) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "kind": kind, "done": 0, "total": 0, "message": "準備中", "result": None, "error": None}

        def progress(done: int, total: int, message: str) -> None:
            with self.lock:
                self.state.update(done=done, total=total, message=message)

        def run() -> None:
            try:
                result = work(progress)
                with self.lock:
                    self.state["result"] = result
            except Exception as e:  # 裏スレッドなので、何が起きても画面に伝える
                with self.lock:
                    self.state["error"] = str(e)
            finally:
                with self.lock:
                    self.state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state, ensure_ascii=False))


# ---- サーバー全体の状態 --------------------------------------------------------

class Api:
    def __init__(self, app: Celestial, port: int):
        self.app, self.port = app, port
        self.chats = ChatStore(app.store)
        self.bus = EventBus()
        self.chats.listeners.append(lambda rev: self.bus.publish("sync", rev=rev))
        self.runs = RunManager(app, self.chats, self.bus, _result_dict)
        self.devices = Devices(app.store)
        self.bench = BenchJob(app)
        self.work = WorkJob()
        self.remote = RemoteListeners(self)
        self.flows = FlowEngine(FlowStore(app.cfg.home / "flows.db"), app, publish=self.bus.publish)
        self.started = time.time()

    def files_root(self) -> Path:
        """ファイルのやり取りに使うフォルダ: 作業フォルダ(設定されていれば)か、Celestial の共有フォルダ。"""
        root = self.app.workspace_root() or self.app.cfg.home / "files"
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def apk_path(self) -> Path | None:
        for path in (self.app.cfg.home / "app" / "celestial.apk",
                     PROJECT_DIR / "app" / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"):
            if path.is_file():
                return path
        return None

    def apk_info(self) -> dict:
        """PC に置いてあるスマホのアプリの版(スマホのアプリが「新しい版があるか」を確かめ、その場で更新するため)。"""
        path = self.apk_path()
        if path is None:
            return {"available": False}
        stat = path.stat()
        meta: dict = {}
        side = path.with_suffix(".json")  # celestial publish-app が書き添える版
        if side.is_file():
            try:
                meta = json.loads(side.read_text("utf-8"))
            except ValueError:
                meta = {}
        if not meta.get("build"):  # 書き添えが無ければ、作ったあとに版を変えていない限り pubspec.yaml の版
            pubspec = PROJECT_DIR / "app" / "pubspec.yaml"
            if pubspec.is_file() and pubspec.stat().st_mtime <= stat.st_mtime:
                found = re.search(r"^version:\s*([\w.\-]+)\+(\d+)", pubspec.read_text("utf-8"), re.M)
                if found:
                    meta = {"version": found.group(1), "build": int(found.group(2))}
        return {"available": True, "version": str(meta.get("version") or ""), "build": int(meta.get("build") or 0),
                "size": stat.st_size, "updated": stat.st_mtime, "url": "/app/celestial.apk"}

    def phone_models(self) -> list[dict]:
        """スマホに入れられるモデル(Ollama の GGUF ファイル)。"""
        backend = self.app.backends.get("ollama")
        if backend is None or not hasattr(backend, "model_file"):
            return []
        scores = self.app._bench_scores()
        out = []
        for m in self.app.models():
            if m.backend != "ollama":
                continue
            try:
                info = backend.model_file(m.name)
            except BackendError:
                continue
            if info is None or info["size"] > 4.5e9:
                continue
            out.append({"name": m.name, "file_size": info["size"], "family": info["family"], "parameters": info["parameters"],
                        "quantization": info["quantization"], "accuracy": scores.get(m.name, {}).get("all"),
                        "own": m.name.startswith("celestial-"), "vision": m.vision})
        best = max((o for o in out if o["file_size"] < 2.2e9 and o["accuracy"] is not None), key=lambda o: o["accuracy"], default=None)
        for o in out:
            o["recommended"] = o is best
        return sorted(out, key=lambda o: (not o["recommended"], o["file_size"]))


class RemoteListeners:
    """Tailscale のアドレスで待ち受けるサーバーを、接続状況に合わせて開け閉めする。"""

    def __init__(self, api: Api):
        self.api = api
        self.servers: dict[str, ThreadingHTTPServer] = {}
        self.error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def refresh(self) -> None:
        cfg = self.api.app.cfg
        status = tailscale_status()
        wanted = set(status["ips"]) if cfg.remote_enabled else set()
        with self._lock:
            for ip in [ip for ip in self.servers if ip not in wanted]:
                server = self.servers.pop(ip)
                threading.Thread(target=server.shutdown, daemon=True).start()
            for ip in sorted(wanted - set(self.servers)):
                try:
                    server = make_http_server(self.api, ip, self.api.port, remote=True)
                except OSError as e:
                    self.error = f"{ip}:{self.api.port} で待ち受けできませんでした: {e}"
                    continue
                self.servers[ip] = server
                self.error = ""
                threading.Thread(target=server.serve_forever, daemon=True, name=f"remote-{ip}").start()

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # 監視が止まると接続できなくなるので、何が起きても続ける
                self.error = str(e)
            self._stop.wait(20)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for server in self.servers.values():
                threading.Thread(target=server.shutdown, daemon=True).start()
            self.servers.clear()

    def addresses(self) -> list[str]:
        """いま待ち受けている Tailscale のアドレス(tailscale を呼ばないので速い)。"""
        with self._lock:
            return [f"{ip}:{self.api.port}" for ip in self.servers]

    def status(self) -> dict:
        ts = tailscale_status()
        with self._lock:
            listening = [f"{ip}:{self.api.port}" for ip in self.servers]
        urls = [f"http://{a}" for a in listening]
        if ts["dns"] and listening:
            urls.append(f"http://{ts['dns']}:{self.api.port}")
        return {"enabled": self.api.app.cfg.remote_enabled, "tailscale": ts, "listening": listening, "urls": urls, "error": self.error}


# ---- ルーティング ------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, object, str]] = []


def route(method: str, pattern: str, access: str = "device"):
    """access: public(登録なしで可)/ device(このPC か登録済み端末)/ local(このPC だけ)"""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, access))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "Celestial"
    # 接続を使い回す(画面の連続取得や Tailscale 越しの通信で、毎回のつなぎ直しを省く)。
    # 長さの分からない応答(逐次送信・通知の配信)だけは、送り終えたら接続を閉じる。
    protocol_version = "HTTP/1.1"
    timeout = 120
    api: Api

    def log_message(self, format, *args):  # コンソールを静かに保つ
        pass

    # ---- 共通 ----

    @property
    def remote(self) -> bool:
        return getattr(self.server, "remote", False)

    def _local_ok(self) -> bool:
        """他サイトからの呼び出し(DNSリバインディングやCSRF)を拒否する。

        中継(Tailscale Funnel / serve などのリバースプロキシ)を通った依頼も拒否する。この受け口は鍵なしで
        PC を操作できるため、誤ってインターネットへ公開する設定をしても、外からは使えないようにする。
        """
        if any(self.headers.get(name) for name in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-Funnel-Request",
                                                   "Tailscale-User-Login", "Via")):
            return False
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed_hosts}

    def _token(self, query: dict) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (query.get("token") or [None])[0]

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _has_body(self) -> bool:
        return "chunked" in (self.headers.get("Transfer-Encoding") or "").lower() or int(self.headers.get("Content-Length") or 0) > 0

    def raw_body(self, limit: int = 64 * 1024**2) -> bytes:
        """本文を読む。Content-Length でも、分割送信(chunked)でもよい。"""
        self._consumed = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts, total = [], 0
            while True:
                size_line = self.rfile.readline(1024).split(b";")[0].strip()
                size = int(size_line or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass  # 末尾のヘッダーを読み捨てる
                    break
                total += size
                if total > limit:
                    raise HttpError(413, "データが大きすぎます")
                parts.append(self.rfile.read(size))
                self.rfile.readline(16)  # 各かたまりの後ろの改行
            return b"".join(parts)
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise HttpError(413, "データが大きすぎます")
        return self.rfile.read(length) if length > 0 else b""

    def body(self) -> dict:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("JSONで送ってください")
        data = json.loads(self.raw_body() or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSONオブジェクトで送ってください")
        return data

    def stream_start(self, content_type: str) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def write_line(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        self.wfile.flush()

    # ---- 振り分け ----

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        self.who, self.device = "pc", None
        self._consumed = False
        if not self.remote and not self._local_ok():
            return self.send_json({"error": "forbidden"}, 403)
        try:
            for m, pattern, fn, access in ROUTES:
                if m != method:
                    continue
                match = pattern.match(url.path)
                if not match:
                    continue
                if self.remote:
                    if access == "local":
                        raise HttpError(403, "この操作は PC の画面からだけ行えます")
                    if access == "device":
                        device = self.api.devices.authenticate(self._token(query), self.client_address[0])
                        if device is None:
                            raise HttpError(401, "この端末は登録されていないか、登録が取り消されています")
                        self.device, self.who = device, device["id"]
                fn(self, match, query)
                if not self._consumed and self._has_body():
                    self.close_connection = True  # 読まなかった本文が次の要求に混ざらないようにする
                return
            if method == "GET" and not self.remote:
                return self._static(url.path)
            raise HttpError(404, "not found")
        except HttpError as e:
            self._error(e.status, str(e))
        except PermissionError as e:
            self._error(403, str(e))
        except KeyError as e:
            self._error(404, str(e.args[0]) if e.args else "見つかりません")
        except (ValueError, TypeError, ToolError, pc.PCError) as e:
            self._error(400, str(e.args[0]) if e.args else str(e))
        except BackendError as e:
            self._error(502, str(e))
        except (ConnectionError, BrokenPipeError):
            pass  # 相手が切断した
        except OSError as e:
            self._error(500, f"操作できませんでした: {e}")

    def _error(self, status: int, message: str) -> None:
        self.close_connection = True  # 読み残した本文が次の要求に混ざらないよう、失敗したら接続を閉じる
        try:
            self.send_json({"error": message}, status)
        except OSError:
            pass

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in file.parents or not file.is_file():
            raise HttpError(404, "not found")
        self.send_bytes(file.read_bytes(), _TYPES.get(file.suffix, "application/octet-stream"))

    def send_file(self, path: Path, content_type: str = "application/octet-stream", download_name: str | None = None) -> None:
        """Range(途中からの再開)に対応したファイル送信。"""
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        spec = self.headers.get("Range") or ""
        if spec.startswith("bytes=") and size:
            first, _, last = spec[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start, end = int(first), int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end, status = min(end, size - 1), 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}")
        self.end_headers()
        if not size:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int((query.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def _require_remote_control(h: Handler) -> None:
    if not pc.SUPPORTED:
        raise HttpError(501, "この PC ではリモート操作を使えません")
    if h.remote and not h.api.app.cfg.remote_control:
        raise HttpError(403, "PC の設定で、スマホからの操作が許可されていません")


# ---- 基本 ------------------------------------------------------------------

@route("GET", r"/api/hello", "public")
def hello(h: Handler, m, q):
    device = h.api.devices.authenticate(h._token(q), h.client_address[0]) if h.remote else None
    h.send_json({"app": "Celestial", "version": __version__, "name": socket.gethostname(), "remote": h.remote,
                 "authenticated": (device is not None) if h.remote else True,
                 "device": {"id": device["id"], "name": device["name"]} if device else None,
                 "features": ["sync", "events", "runs", "agent", "remote_control", "files", "phone_models", "push"]})


@route("GET", r"/api/state")
def state(h: Handler, m, q):
    app = h.api.app
    try:
        models = sorted(app.models(refresh=True), key=lambda x: x.size_bytes)
        manager, error = app.manager_model().name, None
    except BackendError as e:
        models, manager, error = [], None, str(e)
    own_names = set()
    for meta in read_models(app.cfg.home / "models"):
        own_names.update({meta["name"], meta.get("ollama")})
    h.send_json({
        "models": [{"name": x.name, "backend": x.backend, "size_gb": x.size_bytes / 1e9, "tier": x.tier, "vision": x.vision,
                    "own": x.name in own_names or x.name.startswith("celestial-")} for x in models],
        "manager": manager,
        "error": error,
        "roles": [{"name": name, "label": _ROLE_LABELS[name]} for name in ROLES],
        "role_labels": _ROLE_LABELS,
        "settings": {**{key: getattr(app.cfg, key) for key in TUNABLE}, "agent_mode": app.agent_mode()},
        "sensitive": sorted(SENSITIVE),
        "embed_model": app.embedder.model,
        "vision_model": next((x.name for x in models if x.vision), None),
        "pc_supported": pc.SUPPORTED,
        "backup_dir": str(app.cfg.home / "backups"),
        "files_root": str(h.api.files_root()),
        "workspace_root": str(app.workspace_root() or ""),
        "default_workspace": str(app.default_workspace()),
        "agent_modes": [{"id": k, "label": v[0], "description": v[1]} for k, v in AGENT_MODES.items()],
        "processes": toolbox.running_processes(),
        "apk": h.api.apk_path() is not None,
        "apk_info": h.api.apk_info(),
        "home": str(app.cfg.home),
        "name": socket.gethostname(),
        "version": __version__,
        "rev": h.api.chats.rev(),
        "events": h.api.bus.last,
        "who": h.who,
    })


@route("GET", r"/api/stats")
def stats(h: Handler, m, q):
    h.send_json(collect_stats(h.api.app))


@route("GET", r"/api/history")
def history(h: Handler, m, q):
    limit = min(200, _int(q, "limit", 50))
    h.send_json([
        {"id": r["id"], "ts": r["ts"], "task": r["task"], "answer": r["answer"], "role": r["role"], "model": r["model"],
         "source": r["source"], "route_source": r["route_source"], "tokens": _row_tokens(r),
         "latency_s": r["latency_s"], "feedback": r["feedback"]}
        for r in h.api.app.store.recent(limit)])


@route("POST", r"/api/settings")
def settings(h: Handler, m, q):
    data = h.body()
    if h.remote and SENSITIVE & set(data):
        raise HttpError(403, "安全に関わる設定は PC の画面からだけ変えられます")
    for key, value in data.items():
        h.api.app.set_setting(key, value)
    h.api.bus.publish("settings")
    h.send_json({"ok": True})


@route("POST", r"/api/feedback")
def feedback(h: Handler, m, q):
    data = h.body()
    interaction_id = data.get("id")
    message_id = data.get("message_id")
    good = bool(data["good"])
    if message_id:
        message = h.api.chats.message(str(message_id))
        if message is None:
            raise KeyError("メッセージが見つかりません")
        interaction_id = interaction_id or message["meta"].get("interaction_id")
        h.api.chats.update_message(message["id"], meta={"feedback": 1 if good else -1})
    if interaction_id:
        h.api.app.feedback(int(interaction_id), good)
    elif not message_id:
        raise ValueError("id か message_id が必要です")
    h.send_json({"ok": True})


# ---- 判断(Jev のような System 1) ----------------------------------------------------

@route("POST", r"/api/decide")
def decide_api(h: Handler, m, q):
    """文章を作らずに、選択肢(省略時は はい/いいえ)から1つを確率つきで選ぶ。{question, options?, context?, model?}"""
    data = h.body()
    options = data.get("options")
    if isinstance(options, str):
        options = [o for o in re.split(r"[\n,、]", options) if o.strip()]
    try:
        result = h.api.app.decide(str(data.get("question") or ""), options if isinstance(options, list) else None,
                                  str(data.get("context") or ""), str(data.get("model") or "") or None)
    except BackendError as e:
        raise ValueError(f"AIに接続できません: {e}") from e
    h.send_json(result.to_dict())


@route("GET", r"/api/decide/calibration")
def decide_calibration(h: Handler, m, q):
    app = h.api.app
    try:
        current = app._decision_model().name
    except BackendError:
        current = None
    h.send_json({"models": app.decision_calibration(), "current": current, "job": h.api.work.snapshot()})


@route("POST", r"/api/decide/calibrate", "local")
def decide_calibrate(h: Handler, m, q):
    """判断の較正を裏で始める(精度の計測の課題で、判断に使えるAIごとに正答率と温度を学ぶ)。"""
    api = h.api
    if api.bench.snapshot()["running"]:
        raise HttpError(409, "計測の実行中です。終わってから始めてください")
    if not api.work.start("calibrate", lambda progress: api.app.calibrate_decisions(progress)):
        raise HttpError(409, "自習・学習・較正のどれかがすでに実行中です")
    h.send_json({"ok": True})


# ---- 自作AI・計測 --------------------------------------------------------------

@route("GET", r"/api/own")
def own(h: Handler, m, q):
    app = h.api.app
    keys = ("name", "base", "tier", "quantize", "profile", "specialties", "can_manage",
            "created", "examples", "eval_loss", "base_eval_loss", "preference_steps", "seconds", "ollama", "adapter")
    bench_map: dict[str, dict] = {}
    for r in app.store.bench_history():
        bench_map[r["label"]] = {"correct": int(r["correct"]), "n": int(r["n"]), "suite": r["suite"]}
    models = []
    for x in read_models(app.cfg.home / "models"):
        item = {k: x.get(k) for k in keys}
        label = x.get("ollama") or x.get("name")
        if label in bench_map:
            item["bench"] = bench_map[label]
        models.append(item)
    h.send_json({
        "available": libraries_available(),
        "install_hint": INSTALL_HINT,
        "examples": len(training_examples(app.store)),
        "study": len(app.store.study_rows()),
        "role_counts": app.store.study_counts_by_role(),
        "preferences": len(app.store.preference_rows()) + len(app.store.interaction_preferences()),
        "weak_roles": weak_roles(app),
        "presets": MODEL_PRESETS,
        "profiles": OWN_PROFILES,
        "recommended_base": recommended_base(),
        "models": models,
        "job": h.api.work.snapshot(),
    })


def _train_cfg_from_body(data: dict, *, default_auto: bool = False) -> TrainConfig:
    profile = str(data.get("profile") or "").strip() or None
    kw: dict = {}
    if data.get("base") or data.get("preset") or (default_auto and not profile):
        kw["base"] = resolve_base(data.get("base") or data.get("preset") or "auto")
    for k, fn in (("epochs", int), ("batch_tokens", int), ("grad_accum", int), ("rank", int)):
        if data.get(k) is not None:
            kw[k] = fn(data[k])
    for k in ("lr", "kl_beta", "neftune_alpha", "pref_beta", "loraplus_ratio"):
        if data.get(k) is not None:
            kw[k] = float(data[k])
    if data.get("offload_optimizer") is not None:
        kw["offload_optimizer"] = bool(data["offload_optimizer"])
    if data.get("can_manage") is not None:
        kw["can_manage"] = bool(data["can_manage"])
    for k in ("from_adapter", "name", "quantize"):
        if data.get(k):
            kw[k] = str(data[k])
    if data.get("roles"):
        kw["roles"] = list(data["roles"])
    if data.get("specialties"):
        kw["specialties"] = list(data["specialties"])
    try:
        return config_for_profile(profile, **kw)
    except TrainError as e:
        raise HttpError(400, str(e)) from e


@route("POST", r"/api/(study|train)")
def start_work(h: Handler, m, q):
    api, kind, data = h.api, m.group(1), h.body()
    app = api.app
    if api.bench.snapshot()["running"]:
        raise HttpError(409, "計測の実行中です。終わってから始めてください")
    if kind == "study":
        per_role = max(1, min(50, int(data.get("per_role", 15))))
        roles = [data["role"]] if data.get("role") else (weak_roles(app)[:4] if data.get("focus_weak") else None)
        synthetic = max(0, min(200, int(data.get("synthetic", 0))))
        wiki = max(0, min(120, int(data.get("wiki", 0))))
        work = lambda progress: run_study(  # noqa: E731
            app, per_role=per_role, roles=roles, synthetic=synthetic, wiki=wiki,
            verify_llm=bool(data.get("verify_llm")), contrastive=bool(data.get("contrastive")),
            on_progress=progress,
        )
    else:
        cfg = _train_cfg_from_body(data)
        run_code = bool(data.get("run_code", False))

        def work(progress):
            meta = train(app, cfg, on_progress=progress)
            done = finish_model(app, meta["name"], run_code=run_code, quantize=cfg.quantize, on_progress=progress)
            base = done["base_report"]
            return {**meta, "model": done["model"], "export_error": done["export_error"],
                    "bench_correct": done["report"].correct, "bench_n": done["report"].n,
                    "base_correct": base.correct if base else None, "base_n": base.n if base else None}
    if not api.work.start(kind, work):
        raise HttpError(409, "自習または学習がすでに実行中です")
    h.send_json({"ok": True})


@route("POST", r"/api/study/review")
def start_study_review(h: Handler, m, q):
    api = h.api
    if api.bench.snapshot()["running"]:
        raise HttpError(409, "計測の実行中です。終わってから始めてください")
    if not api.work.start("review", lambda progress: review_study(api.app, on_progress=progress)):
        raise HttpError(409, "自習・学習・較正のどれかがすでに実行中です")
    h.send_json({"ok": True})


@route("POST", r"/api/evolve")
def start_evolve(h: Handler, m, q):
    api, data = h.api, h.body()
    if api.bench.snapshot()["running"]:
        raise HttpError(409, "計測の実行中です。終わってから始めてください")
    cfg = _train_cfg_from_body(data, default_auto=True)
    study_per_role = max(0, min(30, int(data.get("study_per_role", 8))))
    synthetic = max(0, min(200, int(data.get("synthetic", 40))))
    wiki = max(0, min(120, int(data.get("wiki", 24))))
    run_code = bool(data.get("run_code", True))
    work = lambda progress: evolve_own_ai(  # noqa: E731
        api.app, config=cfg, study_per_role=study_per_role, synthetic=synthetic, wiki=wiki,
        run_code=run_code, on_progress=progress,
    )
    if not api.work.start("evolve", work):
        raise HttpError(409, "自習・学習・較正のどれかがすでに実行中です")
    h.send_json({"ok": True})


@route("POST", r"/api/own/delete", "local")
def own_delete(h: Handler, m, q):
    api, data = h.api, h.body()
    if api.work.snapshot()["running"] or api.bench.snapshot()["running"]:
        raise HttpError(409, "処理の実行中は削除できません")
    name = str(data.get("name") or "").strip()
    if not name:
        raise HttpError(400, "削除するモデル名を指定してください")
    try:
        res = delete_own_model(api.app, name)
    except (TrainError, ValueError) as e:
        raise HttpError(404, str(e)) from e
    h.send_json({"ok": True, **res})


@route("GET", r"/api/bench")
def bench_state(h: Handler, m, q):
    h.send_json({"job": h.api.bench.snapshot(), "history": [dict(r) for r in h.api.app.store.bench_history()]})


@route("POST", r"/api/bench")
def bench_start(h: Handler, m, q):
    data = h.body()
    if h.api.work.snapshot()["running"]:
        raise HttpError(409, "自習または学習の実行中です。終わってから始めてください")
    kind = "agent" if data.get("kind") == "agent" else "answers"
    if kind == "agent" and h.remote:
        raise PermissionError("エージェントの計測は、この PC の画面から始めてください(PC の上で実際にファイルを作ってコマンドを動かします)")
    if not h.api.bench.start(data.get("model") or None, bool(data.get("run_code")), bool(data.get("verify")), kind):
        raise HttpError(409, "計測はすでに実行中です")
    h.send_json({"ok": True})


@route("POST", r"/api/export", "local")
def export(h: Handler, m, q):
    path = h.api.app.cfg.home / f"training-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    h.send_json({"ok": True, "count": h.api.app.export(path), "path": str(path)})


# ---- 会話と同期 ---------------------------------------------------------------

@route("GET", r"/api/conversations")
def conversations(h: Handler, m, q):
    query = (q.get("q") or [""])[0].strip() or None
    h.send_json({"conversations": h.api.chats.list_conversations(query), "rev": h.api.chats.rev()})


@route("POST", r"/api/conversations")
def conversation_create(h: Handler, m, q):
    data = h.body()
    h.send_json(h.api.chats.create_conversation(str(data.get("title", "")), origin=h.who, id=data.get("id")))


@route("GET", r"/api/conversations/([A-Za-z0-9_-]+)")
def conversation_get(h: Handler, m, q):
    conv = h.api.chats.conversation(m.group(1))
    if conv is None:
        raise KeyError("会話が見つかりません")
    h.send_json({"conversation": conv, "messages": h.api.chats.messages(conv["id"]),
                 "runs": [r for r in h.api.runs.active() if r["conversation_id"] == conv["id"]]})


@route("POST", r"/api/conversations/([A-Za-z0-9_-]+)")
def conversation_update(h: Handler, m, q):
    data = h.body()
    h.send_json(h.api.chats.update_conversation(
        m.group(1), title=data.get("title"), pinned=data.get("pinned"), deleted=data.get("deleted")))


@route("GET", r"/api/conversations/([A-Za-z0-9_-]+)/export")
def conversation_export(h: Handler, m, q):
    cid = m.group(1)
    fmt = ((q.get("format") or ["md"])[0] or "md").lower()
    text = h.api.chats.export_markdown(cid)
    conv = h.api.chats.conversation(cid) or {}
    name = office.file_name((conv.get("title") or "").strip() or "会話")
    if fmt == "docx":
        with tempfile.TemporaryDirectory() as tmp:
            path = office.export_answer("docx", text, (conv.get("title") or "").strip() or "会話", Path(tmp) / "chat.docx")
            data = path.read_bytes()
        h.send_bytes(data, office.KINDS["docx"][0],
                     headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(f'{name}.docx')}"})
        return
    h.send_bytes(text.encode("utf-8"), "text/markdown; charset=utf-8",
                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(f'{name}.md')}"})


@route("GET", r"/api/messages/([A-Za-z0-9_-]+)/export")
def message_export(h: Handler, m, q):
    """回答を Word・Excel・PowerPoint のファイルにして渡す(?format=docx / xlsx / pptx)。"""
    kind = (q.get("format") or ["docx"])[0]
    if kind not in office.KINDS:
        raise ValueError("format は docx / xlsx / pptx のどれかです")
    message = h.api.chats.message(m.group(1))
    if message is None or message["deleted"] or not message["content"].strip():
        raise KeyError("メッセージが見つかりません")
    conversation = h.api.chats.conversation(message["conversation_id"]) or {}
    title = (conversation.get("title") or "").strip() or "Celestial の回答"
    with tempfile.TemporaryDirectory() as tmp:
        path = office.export_answer(kind, message["content"], title, Path(tmp) / f"answer.{kind}")
        data = path.read_bytes()
    name = office.file_name(title)
    h.send_bytes(data, office.KINDS[kind][0],
                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(f'{name}.{kind}')}"})


@route("POST", r"/api/messages/([A-Za-z0-9_-]+)/memory")
def message_memory(h: Handler, m, q):
    """回答に付いた「覚えますか」の候補に答える。{accept: true} なら覚える。どちらでも候補は消す。"""
    message = h.api.chats.message(m.group(1))
    if message is None:
        raise KeyError("メッセージが見つかりません")
    fact = (message.get("meta") or {}).get("memory_suggestion")
    if not fact:
        raise ValueError("覚える候補がありません")
    app = h.api.app
    if h.body().get("accept") and fact not in app.cfg.memories:
        app.set_setting("memories", list(app.cfg.memories) + [fact])
        h.api.bus.publish("settings")
    h.api.chats.update_message(message["id"], meta={"memory_suggestion": None})
    h.send_json({"ok": True, "memories": app.cfg.memories})


@route("POST", r"/api/messages/([A-Za-z0-9_-]+)")
def message_update(h: Handler, m, q):
    data = h.body()
    h.send_json(h.api.chats.update_message(m.group(1), content=data.get("content"), deleted=data.get("deleted")))


@route("GET", r"/api/sync")
def sync_pull(h: Handler, m, q):
    h.send_json(h.api.chats.changes(_int(q, "since", 0), _int(q, "limit", 500)))


@route("POST", r"/api/sync")
def sync_push(h: Handler, m, q):
    h.send_json(h.api.chats.apply(h.body(), origin=h.who))


@route("GET", r"/api/events")
def events(h: Handler, m, q):
    """Server-Sent Events。同期・実行・確認・端末宛ての送信などの出来事を流し続ける。"""
    bus = h.api.bus
    after = _int(q, "after", bus.last)
    h.stream_start("text/event-stream; charset=utf-8")
    hello_event = {"type": "hello", "rev": h.api.chats.rev(), "seq": bus.last, "who": h.who}
    h.wfile.write(f"data: {json.dumps(hello_event, ensure_ascii=False)}\n\n".encode())
    h.wfile.flush()
    while True:
        last, items = bus.wait(after, 20, h.who)
        if items:
            for item in items:
                payload = json.dumps({k: v for k, v in item.items() if k != "target"}, ensure_ascii=False)
                h.wfile.write(f"id: {last}\ndata: {payload}\n\n".encode())
        else:
            h.wfile.write(b": ping\n\n")
        h.wfile.flush()
        after = last


# ---- 実行 ------------------------------------------------------------------

def _stream_run(h: Handler, run, after: int = 0) -> None:
    h.stream_start("application/x-ndjson; charset=utf-8")
    while True:
        items, done = run.wait(after, 15)
        for item in items:
            h.write_line(item)
        after += len(items)
        if done and not items:
            break
        if not items:
            h.write_line({"type": "ping"})


@route("POST", r"/api/run")
def run_start(h: Handler, m, q):
    data = h.body()
    try:
        images = [base64.b64decode(item, validate=True) for item in (data.get("images") or [])[:4]]
    except (binascii.Error, ValueError, TypeError) as e:
        raise ValueError("画像を読み取れませんでした") from e
    if sum(len(i) for i in images) > 16_000_000:
        raise ValueError("画像が大きすぎます(合計16MBまで)")
    task = str(data.get("task", ""))
    history = data.get("history")
    options = {"role": data.get("role"), "model": data.get("model"), "verify": data.get("verify"),
               "use_cache": data.get("use_cache", True), "agent": bool(data.get("agent") or data.get("tools")),
               "swarm": {"on": True, "off": False}.get(data.get("swarm")), "folder": str(data.get("folder") or "")[:300] or None}
    if options["folder"]:
        try:
            chosen = h.api.app.work_folder(options["folder"])  # 選べないフォルダは、始める前に断る
        except ValueError:
            if options["agent"]:
                raise
            chosen = options["folder"] = None  # 普通の会話は、選んだフォルダが無くなっていても続ける
        if chosen is not None and options["agent"]:
            h.api.app.remember_folder(chosen)
    context = data.get("context") or "auto"
    if history and not data.get("conversation_id"):  # 古い画面との互換: 会話の流れを直接受け取る
        context = "on"
    run = h.api.runs.start(task=task, who=h.who, conversation_id=data.get("conversation_id"),
                           user_message_id=data.get("message_id"), images=images, context=context, options=options)
    if data.get("stream", True) is False:
        return h.send_json(run.summary())
    _stream_run(h, run)


@route("POST", r"/api/messages/([A-Za-z0-9_-]+)/regenerate")
def message_regenerate(h: Handler, m, q):
    data = h.body()
    run = h.api.runs.regenerate(message_id=m.group(1), who=h.who)
    if data.get("stream", True) is False:
        return h.send_json(run.summary())
    _stream_run(h, run)


@route("GET", r"/api/runs")
def runs_active(h: Handler, m, q):
    h.send_json({"runs": h.api.runs.active()})


@route("GET", r"/api/runs/([0-9a-f]+)/events")
def run_events(h: Handler, m, q):
    run = h.api.runs.get(m.group(1))
    if run is None:
        raise KeyError("実行が見つかりません")
    _stream_run(h, run, max(0, _int(q, "after", 0)))


@route("POST", r"/api/runs/([0-9a-f]+)/cancel")
def run_cancel(h: Handler, m, q):
    h.send_json({"ok": h.api.runs.cancel(m.group(1))})


@route("POST", r"/api/approve")
def approve(h: Handler, m, q):
    data = h.body()
    if not h.api.runs.approve(str(data.get("id")), bool(data.get("allow")), bool(data.get("all"))):
        raise KeyError("この確認はすでに終わっています")
    h.send_json({"ok": True})


# ---- 端末の登録 ---------------------------------------------------------------

@route("POST", r"/api/pairing/start", "local")
def pairing_start(h: Handler, m, q):
    api = h.api
    api.remote.refresh()
    status = api.remote.status()
    code = api.devices.start_pairing()
    ip = status["tailscale"]["ips"][0] if status["tailscale"]["ips"] else None
    link = f"http://{ip}:{api.port}/pair?c={code['code']}" if ip else None
    h.send_json({**code, "link": link, "qr_svg": qr_svg(link) if link else None, "remote": status})


@route("POST", r"/api/pair", "public")
def pair(h: Handler, m, q):
    data = h.body()
    result = h.api.devices.pair(str(data.get("code", "")), str(data.get("name", "")), str(data.get("platform", "")),
                                h.client_address[0])
    h.api.bus.publish("devices")
    status = h.api.remote.status()
    h.send_json({**result, "pc_name": socket.gethostname(), "urls": status["urls"]})


@route("GET", r"/api/devices", "local")
def devices(h: Handler, m, q):
    h.send_json({"devices": h.api.devices.list(), "remote": h.api.remote.status()})


@route("POST", r"/api/devices/([0-9a-f]+)/revoke", "local")
def device_revoke(h: Handler, m, q):
    ok = h.api.devices.revoke(m.group(1))
    h.api.bus.publish("devices")
    h.send_json({"ok": ok})


@route("POST", r"/api/devices/([0-9a-f]+)/rename", "local")
def device_rename(h: Handler, m, q):
    h.send_json({"ok": h.api.devices.rename(m.group(1), str(h.body().get("name", "")))})


@route("GET", r"/api/remote/status", "local")
def remote_status(h: Handler, m, q):
    h.send_json(h.api.remote.status())


@route("POST", r"/api/send")
def send_to(h: Handler, m, q):
    """端末どうしで、文字・URL・クリップボードを送る(PC → スマホ、スマホ → PC)。"""
    data = h.body()
    kind = str(data.get("kind", "text"))
    text = str(data.get("text", ""))[:100_000]
    if kind not in ("text", "url", "clipboard", "notify"):
        raise ValueError("kind は text / url / clipboard / notify のいずれかです")
    if h.remote:  # スマホ → PC
        if kind == "clipboard":
            _require_remote_control(h)
            pc.clipboard_write(text)
        sender = h.device["name"] if h.device else "端末"
        h.api.bus.publish("push", target="pc", kind=kind, text=text, title=str(data.get("title", "")), sender=sender)
    else:  # PC → スマホ(target を省くと全端末)
        target = data.get("target") or None
        h.api.bus.publish("push", target=target, kind=kind, text=text, title=str(data.get("title", "")), sender=socket.gethostname())
    h.send_json({"ok": True})


# ---- リモート操作 -------------------------------------------------------------

@route("GET", r"/api/remote/screen")
def remote_screen(h: Handler, m, q):
    _require_remote_control(h)
    width = max(160, min(3840, _int(q, "w", 1280)))
    fmt = "jpeg" if (q.get("f") or ["png"])[0] == "jpeg" else "png"
    data, w, height, screen_w, screen_h = pc.capture(width, fmt, max(10, min(95, _int(q, "q", 70))))
    kind = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    etag = '"' + hashlib.md5(data).hexdigest() + '"'
    headers = {"ETag": etag, "X-Screen-Width": str(screen_w), "X-Screen-Height": str(screen_h),
               "X-Image-Width": str(w), "X-Image-Height": str(height)}
    if h.headers.get("If-None-Match") == etag:
        h.send_response(304)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return
    h.send_bytes(data, kind, headers=headers)


@route("POST", r"/api/remote/input")
def remote_input(h: Handler, m, q):
    _require_remote_control(h)
    data = h.body()
    action = str(data.get("action", ""))
    screen_w, screen_h = pc.screen_size()

    def at(key: str, size: int) -> int:
        return int(min(max(float(data[key]), 0.0), 0.9999) * size)

    if action in ("click", "double", "right"):
        message = pc.click(at("x", screen_w), at("y", screen_h), {"click": "left"}.get(action, action))
    elif action == "move":
        message = pc.move(at("x", screen_w), at("y", screen_h))
    elif action == "drag":
        message = pc.drag(at("x", screen_w), at("y", screen_h), at("x2", screen_w), at("y2", screen_h))
    elif action == "scroll":
        if "x" in data and "y" in data:
            pc.move(at("x", screen_w), at("y", screen_h))
        message = pc.scroll(int(float(data.get("amount", -3))))
    elif action == "text":
        message = pc.type_text(str(data.get("text", "")))
    elif action == "keys":
        message = pc.press_keys(str(data.get("keys", "")))
    else:
        raise ValueError("action が不正です")
    h.send_json({"ok": True, "message": message})


@route("POST", r"/api/remote/action")
def remote_action(h: Handler, m, q):
    _require_remote_control(h)
    data = h.body()
    action = str(data.get("action", ""))
    if action == "lock":
        message = pc.lock()
    elif action in ("volume_up", "volume_down", "mute"):
        message = pc.volume({"volume_up": "up", "volume_down": "down", "mute": "mute"}[action])
    elif action in ("play_pause", "next", "previous", "stop"):
        message = pc.media(action)
    elif action == "open":
        message = pc.open_target(str(data.get("target", "")))
    elif action in ("focus", "close"):
        title = str(data.get("title", ""))
        message = pc.focus_window(title) if action == "focus" else pc.close_window(title)
    else:
        raise ValueError("action が不正です")
    h.send_json({"ok": True, "message": message})


@route("GET", r"/api/remote/info")
def remote_info(h: Handler, m, q):
    _require_remote_control(h)
    width, height = pc.screen_size()
    h.send_json({"system": pc.system_info(), "windows": [t for _, t in pc.list_windows()],
                 "foreground": pc.foreground_title(), "screen": [width, height]})


@route("GET", r"/api/remote/clipboard")
def clipboard_get(h: Handler, m, q):
    _require_remote_control(h)
    h.send_json({"text": pc.clipboard_read()})


@route("POST", r"/api/remote/clipboard")
def clipboard_set(h: Handler, m, q):
    _require_remote_control(h)
    h.send_json({"ok": True, "message": pc.clipboard_write(str(h.body().get("text", "")))})


# ---- ファイル -----------------------------------------------------------------

def _file_target(h: Handler, query: dict, key: str = "path") -> tuple[Path, Path, str]:
    root = h.api.files_root()
    rel = (query.get(key) or [""])[0].replace("\\", "/").strip("/")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("共有フォルダの外にはアクセスできません")
    return root, target, rel


@route("GET", r"/api/folders")
def folders(h: Handler, m, q):
    """エージェントが作業するフォルダの候補(作業フォルダと、その中のフォルダ。2階層まで)。"""
    root = h.api.app.work_folder(None)
    if root is None:
        return h.send_json({"root": None, "folders": []})
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".idea", ".vscode", ".dart_tool"}
    found = []
    for first in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in skip and not p.name.startswith(".")):
        found.append(first.name)
        try:
            found += [f"{first.name}/{p.name}" for p in sorted(first.iterdir()) if p.is_dir() and p.name not in skip and not p.name.startswith(".")][:30]
        except OSError:
            pass
        if len(found) >= 200:
            break
    h.send_json({"root": root.name, "folders": found})


def _browse_roots(h: Handler) -> list[dict]:
    """フォルダを選ぶときの近道: 作業フォルダ・最近のフォルダ・ホーム・デスクトップ・ドキュメント・ダウンロード・ドライブ。"""
    app = h.api.app
    home = Path.home()
    picks: list[dict] = []

    def add(label: str, path: Path | str | None, kind: str) -> None:
        if not path:
            return
        path = Path(path)
        if path.is_dir() and all(Path(x["path"]) != path for x in picks):
            picks.append({"label": label, "path": str(path), "kind": kind})

    add("作業フォルダ", app.workspace_root(), "workspace")
    for folder in app.cfg.recent_folders[:6]:
        add(Path(folder).name or folder, folder, "recent")
    add("ホーム", home, "home")
    for name, label in (("Desktop", "デスクトップ"), ("Documents", "ドキュメント"), ("Downloads", "ダウンロード"),
                        ("OneDrive", "OneDrive"), ("Projects", "Projects")):
        add(label, home / name, "place")
    if os.name == "nt" and not h.remote:
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                add(f"{letter}: ドライブ", drive, "drive")
    return picks


def _browsable(h: Handler, path: Path) -> bool:
    """スマホなど別の端末からは、ホームフォルダと作業フォルダの中だけを見せる(PC 全体のフォルダ構成は見せない)。"""
    if not h.remote:
        return True
    roots = [Path.home().resolve()] + [r.resolve() for r in [h.api.app.workspace_root()] if r is not None]
    return any(path == r or r in path.parents for r in roots)


@route("GET", r"/api/browse")
def browse(h: Handler, m, q):
    """エージェントに作業させるフォルダを選ぶための一覧(フォルダだけ)。"""
    app = h.api.app
    raw = (q.get("path") or [""])[0].strip()
    if not raw:
        h.send_json({"path": "", "parent": None, "folders": [], "roots": _browse_roots(h), "mode": app.agent_mode()})
        return
    target = Path(os.path.expandvars(raw)).expanduser().resolve()
    if not target.is_dir():
        raise KeyError(f"フォルダが見つかりません: {raw}")
    if not _browsable(h, target):
        raise PermissionError("スマホからは、ホームフォルダと作業フォルダの中だけを選べます")
    folders = []
    try:
        for p in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            try:
                if p.is_dir() and not p.name.startswith(("$", ".")) and p.name not in ("System Volume Information", "__pycache__", "node_modules"):
                    folders.append({"name": p.name, "path": str(p)})
            except OSError:
                continue
            if len(folders) >= 400:
                break
    except OSError as e:
        raise PermissionError(f"このフォルダは開けません: {e}") from e
    parent = target.parent if target.parent != target else None
    h.send_json({"path": str(target), "parent": str(parent) if parent and _browsable(h, parent) else None, "folders": folders,
                 "roots": _browse_roots(h), "system": app.is_system_folder(target), "mode": app.agent_mode(),
                 "usable": not app.is_system_folder(target) or app.agent_mode() == "full"})


@route("POST", r"/api/browse/mkdir", "local")
def browse_mkdir(h: Handler, m, q):
    """フォルダを選ぶ画面から、新しいフォルダを作る(PC の画面からだけ)。"""
    data = h.body()
    parent = Path(str(data.get("path") or "")).expanduser().resolve()
    name = str(data.get("name") or "").strip()
    if not parent.is_dir():
        raise KeyError("作る場所のフォルダが見つかりません")
    if not name or any(c in name for c in '\\/:*?"<>|') or name in (".", ".."):
        raise ValueError("フォルダの名前に使えない文字が含まれています")
    target = parent / name
    target.mkdir(exist_ok=True)
    h.send_json({"path": str(target)})


@route("GET", r"/api/vision/modes")
def vision_modes(h: Handler, m, q):
    h.send_json({"modes": [{"id": k, "label": v.label, "question": v.needs_question} for k, v in vision.MODES.items()],
                 "available": h.api.app._vision_model() is not None})


@route("POST", r"/api/vision")
def vision_run(h: Handler, m, q):
    """画像の認識(カメラ・写真・PC の画面)。スマホからも使える(PC の画面を見るのは、スマホからの操作が許可されているときだけ)。"""
    data = h.body()
    if data.get("source") == "screen":
        _require_remote_control(h)
        image = pc.capture(1280, "jpeg", 75)[0]
    else:
        image = vision.decode_image(str(data.get("image") or ""))
    result = h.api.app.see(image, str(data.get("mode") or "describe"), str(data.get("question") or ""), str(data.get("lang") or "ja"))
    if data.get("source") == "screen" and data.get("return_image"):
        result["image"] = base64.b64encode(image).decode("ascii")
    h.send_json(result)


@route("GET", r"/api/processes")
def processes_list(h: Handler, m, q):
    """エージェントが起動して、動かし続けているプログラム(サーバーなど)。"""
    h.send_json({"processes": toolbox.running_processes()})


@route("POST", r"/api/processes/stop")
def processes_stop(h: Handler, m, q):
    name = str(h.body().get("name") or "")
    if not toolbox.stop_named(name):
        raise KeyError("そのプログラムは動いていません")
    h.send_json({"ok": True})


@route("GET", r"/api/files")
def files_list(h: Handler, m, q):
    root, target, rel = _file_target(h, q)
    if not target.is_dir():
        raise KeyError("フォルダが見つかりません")
    entries = []
    for p in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:2000]:
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append({"name": p.name, "path": p.relative_to(root).as_posix(), "dir": p.is_dir(),
                        "size": st.st_size if p.is_file() else 0, "modified": st.st_mtime})
    h.send_json({"root": str(root), "path": rel, "entries": entries})


@route("GET", r"/api/files/raw")
def files_download(h: Handler, m, q):
    _, target, _ = _file_target(h, q)
    if not target.is_file():
        raise KeyError("ファイルが見つかりません")
    h.send_file(target, "application/octet-stream", target.name)


@route("PUT", r"/api/files/raw")
def files_upload(h: Handler, m, q):
    root, target, rel = _file_target(h, q)
    if not rel or target.is_dir():
        raise ValueError("保存先のファイル名を指定してください")
    length = int(h.headers.get("Content-Length") or -1)
    if length < 0:
        raise HttpError(411, "Content-Length が必要です")
    if length > MAX_UPLOAD:
        raise HttpError(413, "ファイルが大きすぎます(2GBまで)")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    try:
        with open(partial, "wb") as f:
            remaining = length
            while remaining > 0:
                chunk = h.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    raise ValueError("送信が途中で切れました")
                f.write(chunk)
                remaining -= len(chunk)
        h._consumed = True
        if target.exists():  # 上書きの前に元のファイルを残す
            backup = h.api.app.cfg.home / "backups" / time.strftime("%Y%m%d-%H%M%S") / "files" / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()
    h.api.bus.publish("files", path=rel)
    h.send_json({"ok": True, "path": rel, "size": length})


@route("POST", r"/api/files/mkdir")
def files_mkdir(h: Handler, m, q):
    _, target, rel = _file_target(h, {"path": [str(h.body().get("path", ""))]})
    target.mkdir(parents=True, exist_ok=True)
    h.send_json({"ok": True, "path": rel})


@route("POST", r"/api/files/delete")
def files_delete(h: Handler, m, q):
    _, target, rel = _file_target(h, {"path": [str(h.body().get("path", ""))]})
    if not target.is_file():
        raise ValueError("ファイルだけを削除できます")
    backup = h.api.app.cfg.home / "backups" / time.strftime("%Y%m%d-%H%M%S") / "files" / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(backup))  # 実際にはバックアップへ移すだけ
    h.api.bus.publish("files", path=rel)
    h.send_json({"ok": True, "backup": str(backup)})


# ---- 自動化(n8n のようなワークフロー) ---------------------------------------------

def _flow_view(h: Handler, flow: dict) -> dict:
    """画面に出す形。Webhook の鍵は PC の画面にだけ出す(スマホには出さない)。"""
    view = {k: v for k, v in flow.items() if k != "hook_key"}
    if flow["trigger"]["type"] == "webhook" and not h.remote:
        path = f"/api/hooks/{flow['id']}?key={flow['hook_key']}"
        hosts = [f"http://127.0.0.1:{h.api.port}"] + [f"http://{a}" for a in h.api.remote.addresses()]
        view["hook_urls"] = [host + path for host in hosts]
    return view


@route("GET", r"/api/flows")
def flows_list(h: Handler, m, q):
    cfg = h.api.app.cfg
    h.send_json({"flows": [_flow_view(h, f) for f in h.api.flows.list()], "steps": STEP_TYPES, "triggers": TRIGGER_TYPES,
                 "ops": CONDITION_OPS, "templates": FLOW_TEMPLATES, "can_edit": not h.remote,
                 "allowed": {"net": cfg.tools_net, "files": h.api.app.workspace_root() is not None,
                             "write": h.api.app.workspace_root() is not None and cfg.tools_write,
                             "run": cfg.tools_run}})


@route("POST", r"/api/flows", "local")
def flows_save(h: Handler, m, q):
    h.send_json(_flow_view(h, h.api.flows.save(h.body())))


@route("POST", r"/api/flows/generate")
def flows_generate(h: Handler, m, q):
    try:
        draft = build_flow(h.api.app, str(h.body().get("description") or ""))
    except BackendError as e:
        raise ValueError(f"AIに接続できません: {e}") from e
    h.send_json({"flow": draft})


@route("POST", r"/api/flows/([A-Za-z0-9_-]+)/run")
def flows_run(h: Handler, m, q):
    data = h.body() if h._has_body() else {}
    run_id = h.api.flows.run_async(m.group(1), str(data.get("input") or ""), "phone" if h.remote else "manual")
    h.send_json({"run_id": run_id})


@route("POST", r"/api/flows/([A-Za-z0-9_-]+)/enabled")
def flows_enabled(h: Handler, m, q):
    flow = h.api.flows.set_enabled(m.group(1), bool(h.body().get("enabled")))
    h.send_json(_flow_view(h, flow))


@route("POST", r"/api/flows/([A-Za-z0-9_-]+)/delete", "local")
def flows_delete(h: Handler, m, q):
    if not h.api.flows.delete(m.group(1)):
        raise KeyError("自動化が見つかりません")
    h.send_json({"ok": True})


@route("GET", r"/api/flows/([A-Za-z0-9_-]+)/runs")
def flows_runs(h: Handler, m, q):
    h.send_json({"runs": h.api.flows.store.runs(m.group(1), limit=int((q.get("limit") or ["20"])[0]))})


@route("GET", r"/api/flows/runs/(\d+)")
def flows_run_detail(h: Handler, m, q):
    run = h.api.flows.store.run(int(m.group(1)))
    if run is None:
        raise KeyError("実行の記録が見つかりません")
    h.send_json(run)


def _hook(h: Handler, m, q) -> None:
    """Webhook: 鍵つきのアドレスに送られた内容をきっかけに、流れを動かす。?wait=1 なら終わるまで待って結果を返す。"""
    flow = h.api.flows.store.get(m.group(1))
    key = (q.get("key") or [""])[0] or (h.headers.get("X-Celestial-Key") or "")
    if flow is None or flow["trigger"]["type"] != "webhook" or not secrets.compare_digest(key, flow.get("hook_key", "")):
        raise HttpError(404, "not found")
    if not flow.get("enabled"):
        raise HttpError(409, "この自動化は止まっています")
    given = h.raw_body(limit=1024**2).decode("utf-8", "replace") if h._has_body() else ""
    if not given and q:
        given = json.dumps({k: v[0] for k, v in q.items() if k not in ("key", "wait")}, ensure_ascii=False) if len(q) > 1 else ""
    if (q.get("wait") or ["0"])[0] == "1":
        run = h.api.flows.run_now(flow["id"], given, "webhook")
        h.send_json({"run_id": run.get("id"), "status": run.get("status"), "output": run.get("output")})
    else:
        h.send_json({"run_id": h.api.flows.run_async(flow["id"], given, "webhook")})


route("POST", r"/api/hooks/([A-Za-z0-9_-]+)", "public")(_hook)
route("GET", r"/api/hooks/([A-Za-z0-9_-]+)", "public")(_hook)


# ---- 知識ベース (RAG) ----------------------------------------------------------

@route("GET", r"/api/kb")
def kb_list(h: Handler, m, q):
    kb = h.api.app.kb
    h.send_json({"stats": kb.stats(), "docs": kb.list_docs(), "embed_model": h.api.app.embedder.model})


@route("POST", r"/api/kb/add", "local")
def kb_add(h: Handler, m, q):
    data = h.body()
    kb = h.api.app.kb
    kind = str(data.get("kind") or "note").strip().lower()
    tags = str(data.get("tags") or "").strip()
    if kind == "note":
        title = str(data.get("title") or "").strip()
        text = str(data.get("text") or "").strip()
        if not title or not text:
            raise ValueError("題名と本文を入力してください")
        res = kb.add_note(title, text, tags=tags)
    elif kind == "file":
        path = str(data.get("path") or "").strip()
        if not path:
            raise ValueError("ファイルパスを指定してください")
        res = kb.add_file(path, tags=tags)
    elif kind == "folder":
        path = str(data.get("path") or "").strip()
        if not path:
            raise ValueError("フォルダパスを指定してください")
        limit = max(1, min(200, int(data.get("max_files", 80))))
        res = kb.add_folder(path, limit=limit, tags=tags)
    elif kind == "url":
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("URL を指定してください")
        res = kb.add_url(url, title=str(data.get("title") or ""), tags=tags)
    else:
        raise ValueError(f"未対応の種類です: {kind}")
    h.send_json({"ok": True, "result": res, "stats": kb.stats()})


@route("POST", r"/api/kb/search")
def kb_search(h: Handler, m, q):
    data = h.body()
    query = str(data.get("query") or "").strip()
    top_k = max(1, min(20, int(data.get("top_k", 5))))
    hits = h.api.app.kb.search(query, top_k=top_k) if query else []
    h.send_json({"hits": hits})


@route("POST", r"/api/kb/delete", "local")
def kb_delete(h: Handler, m, q):
    data = h.body()
    target = data.get("id")
    if target is None or target == "":
        raise ValueError("id を指定してください")
    try:
        doc_key: int | str = int(target)
    except (TypeError, ValueError):
        doc_key = str(target)
    if not h.api.app.kb.remove(doc_key):
        raise KeyError("資料が見つかりません")
    h.send_json({"ok": True, "stats": h.api.app.kb.stats()})


# ---- スマホ用のモデルとアプリ ---------------------------------------------------

@route("GET", r"/api/phone/models")
def phone_models(h: Handler, m, q):
    h.send_json({"models": h.api.phone_models()})


@route("GET", r"/api/phone/models/file")
def phone_model_file(h: Handler, m, q):
    name = (q.get("name") or [""])[0]
    if name not in {x["name"] for x in h.api.phone_models()}:
        raise KeyError("そのモデルはスマホに配れません")
    info = h.api.app.backends["ollama"].model_file(name)
    h.send_file(Path(info["path"]), "application/octet-stream", name.replace(":", "-").replace("/", "-") + ".gguf")


@route("GET", r"/app/celestial\.apk", "public")
def apk(h: Handler, m, q):
    path = h.api.apk_path()
    if path is None:
        raise KeyError("アプリがまだ作られていません")
    h.send_file(path, "application/vnd.android.package-archive", "celestial.apk")


@route("GET", r"/api/app", "public")
def app_version(h: Handler, m, q):
    h.send_json(h.api.apk_info())


@route("GET", r"/pair", "public")
def pair_page(h: Handler, m, q):
    """QRコードをスマホのカメラで読むと開くページ。アプリを開くか、入れるかを選べる。"""
    code = "".join(ch for ch in (q.get("c") or [""])[0] if ch.isdigit())[:6]
    host_header = h.headers.get("Host") or ""
    host, _, port = host_header.rpartition(":") if ":" in host_header else (host_header, "", str(h.api.port))
    params = urllib.parse.urlencode({"h": host, "p": port or h.api.port, "c": code})
    intent = f"intent://pair?{params}#Intent;scheme=celestial;package=dev.celestial.celestial;end"
    has_apk = h.api.apk_path() is not None
    name = html.escape(socket.gethostname())
    body = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Celestial に接続</title><style>
:root{{--bg:#0d111d;--card:#151b2b;--fg:#e7eaf3;--muted:#98a2b8;--accent:#7f9cff;--line:#2a3349}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif;padding:32px 20px}}
main{{max-width:420px;margin:0 auto;display:flex;flex-direction:column;gap:18px}}
h1{{font-size:22px;margin:0}} p{{margin:0;color:var(--muted)}}
.code{{font:600 34px/1.2 ui-monospace,monospace;letter-spacing:.2em;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;text-align:center}}
a.btn{{display:block;text-align:center;padding:14px;border-radius:12px;text-decoration:none;font-weight:600}}
.primary{{background:var(--accent);color:#0b1026}} .ghost{{border:1px solid var(--line);color:var(--fg)}}
</style></head><body><main>
<h1>{name} に接続</h1>
<p>Celestial アプリで、この PC とスマホをつなぎます。</p>
<div class="code">{html.escape(code) or '------'}</div>
<a class="btn primary" href="{html.escape(intent)}">Celestial アプリで接続する</a>
{'<a class="btn ghost" href="/app/celestial.apk">アプリをダウンロード(Android)</a>' if has_apk else ''}
<p>アプリが開かない場合は、アプリの「PC と接続」でアドレス <b>{html.escape(host)}:{html.escape(str(port or h.api.port))}</b> と上のコードを入力してください。</p>
</main></body></html>"""
    h.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")


# ---- 起動 ------------------------------------------------------------------

def make_http_server(api: Api, host: str, port: int, remote: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.remote = remote
    return server


def make_server(app: Celestial, port: int = 8765) -> ThreadingHTTPServer:
    """このPC用の受け口を作る(テストでは port=0 で空いている番号を使う)。"""
    api = Api(app, port)
    server = make_http_server(api, "127.0.0.1", port)
    api.port = server.server_address[1]
    server.api = api
    api.flows.start()  # 時刻・間隔・ファイルのきっかけを見張る
    return server


def open_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    """専用ウィンドウ(Edge/Chrome のアプリモード)で開く。無ければ既定のブラウザ。

    専用のプロファイルで開くので、返したプロセスの終了 = ウィンドウが閉じられた、と判定できる。
    既定のブラウザで開いた場合は None を返す。
    """
    candidates = [shutil.which("msedge"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", shutil.which("chrome"),
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    for exe in candidates:
        if exe and Path(exe).is_file():
            try:
                return subprocess.Popen([exe, f"--app={url}", "--window-size=1240,840", f"--user-data-dir={profile_dir}",
                                         "--no-first-run", "--no-default-browser-check"])
            except OSError:
                continue
    webbrowser.open(url)
    return None


def serve(app: Celestial, port: int = 8765, open_browser: bool = True, keep_running: bool = False) -> int:
    """サーバーを起動する。keep_running=True ならウィンドウを閉じても動き続ける(スマホから使うため)。"""
    profile_dir = app.cfg.home / "window"
    try:
        server = make_server(app, port)
    except OSError:
        url = f"http://127.0.0.1:{port}/"
        print(f"ポート {port} は使用中です。すでに起動している画面を開きます: {url}")
        if open_browser:
            open_window(url, profile_dir)
        return 0
    api: Api = server.api
    threading.Thread(target=api.remote.loop, daemon=True, name="remote-listeners").start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Celestial を起動しました: {url}  (終了は Ctrl+C)")
    if open_browser:
        window = open_window(url, profile_dir)
        if window is not None and not keep_running and not app.cfg.keep_running:  # ウィンドウが閉じられたらサーバーも終了する
            threading.Thread(target=lambda: (window.wait(), server.shutdown()), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api.remote.stop()
        server.server_close()
    return 0
