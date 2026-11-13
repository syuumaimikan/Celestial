"""自動化(n8n のようなワークフロー)。

きっかけ(手動・決まった時刻・一定の間隔・Webhook・作業フォルダへのファイルの追加)で、手順(AIへの依頼・ウェブ・
ファイル・文書・コマンド・通知・条件・文章の組み立て・待つ)を上から順に動かす。前の手順の結果は {{prev}} や
{{step2}} で後の手順に渡せる。きっかけの内容は {{trigger}}(Webhook の JSON なら {{trigger.項目}} も)。

ファイル・文書・コマンド・ウェブの手順は、AIエージェントと同じ道具(tools.execute)を通して動かすので、
作業フォルダの外には届かず、書き換えの前にはバックアップが残る。設定で許可していない種類の手順は動かない。
自動化は確認なしで動くため、作る・変えるのは PC の画面からだけにする(スマホからは実行・オンオフ・履歴の確認)。
"""

from __future__ import annotations

import fnmatch
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import web
from .tools import ToolContext, ToolError, execute

STEP_TYPES: dict[str, dict] = {
    "ai": {"label": "AIに頼む", "fields": {"prompt": "依頼文"}},
    "decide": {"label": "判断する", "fields": {"question": "質問", "options": "選択肢", "context": "判断の材料"}},
    "fetch": {"label": "ウェブページを読む", "fields": {"url": "アドレス"}, "needs": "net"},
    "post": {"label": "ウェブに送る", "fields": {"url": "送り先のアドレス", "body": "送る内容"}, "needs": "net"},
    "read_file": {"label": "ファイルを読む", "fields": {"path": "ファイル"}, "needs": "files"},
    "write_file": {"label": "ファイルに書く", "fields": {"path": "ファイル", "content": "内容", "mode": "書き方"}, "needs": "write"},
    "document": {"label": "文書を作る", "fields": {"kind": "種類", "path": "ファイル", "content": "内容"}, "needs": "write"},
    "command": {"label": "コマンドを実行", "fields": {"command": "コマンド"}, "needs": "run"},
    "notify": {"label": "通知する", "fields": {"title": "題", "text": "本文", "target": "送り先"}},
    "condition": {"label": "条件に合わなければ止める", "fields": {"value": "調べる文字", "op": "比べ方", "match": "比べる文字"}},
    "template": {"label": "文章を組み立てる", "fields": {"text": "文章"}},
    "wait": {"label": "待つ", "fields": {"seconds": "秒"}},
}
TRIGGER_TYPES = {"manual": "手動", "schedule": "決まった時刻", "interval": "一定の間隔", "webhook": "Webhook", "file": "ファイルの追加"}
CONDITION_OPS = {"contains": "を含む", "not_contains": "を含まない", "equals": "と同じ", "regex": "に当てはまる(正規表現)",
                 "empty": "が空", "not_empty": "が空でない"}
NEEDS_LABEL = {"net": "「ネットの利用を許可」", "files": "作業フォルダの設定", "write": "「ファイルの変更を許可」",
               "run": "「コマンドと Python の実行を許可」"}
WEEKDAYS = "月火水木金土日"
MAX_STEPS = 20
MAX_OUTPUT = 20000
_VAR = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


class FlowStop(Exception):
    """条件に合わなかったので、ここで止める(失敗ではない)。"""


# ---- 形の確かめ --------------------------------------------------------------

def _text(value, limit: int = 8000) -> str:
    return ("" if value is None else str(value))[:limit]


def normalize_flow(data: dict) -> dict:
    """画面やAIが作った流れを確かめて、保存する形にそろえる。おかしなところは ValueError(日本語の説明)。"""
    if not isinstance(data, dict):
        raise ValueError("流れの形が正しくありません")
    name = _text(data.get("name"), 80).strip() or "名前のない自動化"
    trigger = data.get("trigger") or {"type": "manual"}
    kind = trigger.get("type") if isinstance(trigger, dict) else None
    if kind not in TRIGGER_TYPES:
        raise ValueError(f"きっかけの種類が正しくありません: {kind}")
    clean_trigger: dict = {"type": kind}
    if kind == "schedule":
        clock = _text(trigger.get("time"), 5).strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", clock):
            raise ValueError("時刻は 08:30 のように書いてください")
        hour, minute = clock.split(":")
        days = trigger.get("days")
        days = sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6}) if isinstance(days, list) else list(range(7))
        clean_trigger.update(time=f"{int(hour):02d}:{minute}", days=days or list(range(7)))
    elif kind == "interval":
        try:
            minutes = int(trigger.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if not 1 <= minutes <= 7 * 24 * 60:
            raise ValueError("間隔は 1〜10080 分で指定してください")
        clean_trigger["minutes"] = minutes
    elif kind == "file":
        folder = _text(trigger.get("folder"), 300).strip().strip("/\\") or "."
        if Path(folder).is_absolute() or ".." in Path(folder).parts:
            raise ValueError("見張るフォルダは作業フォルダの中で指定してください")
        clean_trigger.update(folder=folder, pattern=_text(trigger.get("pattern"), 60).strip() or "*")
    steps = data.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("手順を1つ以上入れてください")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"手順は {MAX_STEPS} 個までです")
    clean_steps = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("type") not in STEP_TYPES:
            raise ValueError(f"{i}番目の手順の種類が正しくありません")
        spec = STEP_TYPES[step["type"]]
        clean = {"type": step["type"], "label": _text(step.get("label"), 60).strip()}
        for field in spec["fields"]:
            clean[field] = _text(step.get(field))
        if step["type"] == "write_file":
            clean["mode"] = "append" if clean["mode"] == "append" else "overwrite"
        if step["type"] == "document":
            clean["kind"] = clean["kind"] if clean["kind"] in ("docx", "xlsx", "pptx") else "docx"
        if step["type"] == "notify":
            clean["target"] = clean["target"] if clean["target"] in ("all", "pc", "phone") else "all"
        if step["type"] == "condition":
            clean["op"] = clean["op"] if clean["op"] in CONDITION_OPS else "contains"
        if step["type"] == "wait":
            try:
                clean["seconds"] = str(max(0, min(600, int(float(clean["seconds"] or 0)))))
            except ValueError:
                raise ValueError(f"{i}番目の手順(待つ)の秒数は数で書いてください") from None
        required = {"ai": "prompt", "decide": "question", "fetch": "url", "post": "url", "read_file": "path", "write_file": "path",
                    "document": "path", "command": "command", "template": "text"}.get(step["type"])
        if required and not clean[required].strip():
            raise ValueError(f"{i}番目の手順({spec['label']})の「{spec['fields'][required]}」を入れてください")
        clean_steps.append(clean)
    return {"id": _text(data.get("id"), 40) or uuid.uuid4().hex[:12], "name": name, "enabled": bool(data.get("enabled", True)),
            "trigger": clean_trigger, "steps": clean_steps, "hook_key": _text(data.get("hook_key"), 64) or secrets.token_urlsafe(18)}


def describe_trigger(trigger: dict) -> str:
    kind = trigger.get("type")
    if kind == "schedule":
        days = trigger.get("days") or list(range(7))
        when = "毎日" if len(days) == 7 else "平日" if days == [0, 1, 2, 3, 4] else "・".join(WEEKDAYS[d] for d in days) + "曜日"
        return f"{when} {trigger.get('time')}"
    if kind == "interval":
        minutes = int(trigger.get("minutes") or 0)
        return f"{minutes // 60}時間ごと" if minutes % 60 == 0 else f"{minutes}分ごと"
    if kind == "file":
        return f"「{trigger.get('folder')}」に {trigger.get('pattern')} が追加されたとき"
    return TRIGGER_TYPES.get(kind, kind or "")


def next_run(trigger: dict, after: float) -> float | None:
    """次に動かす時刻(時刻・間隔のきっかけだけ)。"""
    if trigger.get("type") == "interval":
        return after + int(trigger.get("minutes") or 60) * 60
    if trigger.get("type") != "schedule":
        return None
    hour, minute = (int(x) for x in trigger["time"].split(":"))
    days = trigger.get("days") or list(range(7))
    base = time.localtime(after)
    for offset in range(0, 8):
        t = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + offset, hour, minute, 0, 0, 0, -1))
        if t > after and time.localtime(t).tm_wday in days:
            return t
    return None


def render(template: str, values: dict[str, str]) -> str:
    """{{prev}} {{step2}} {{trigger}} {{trigger.項目}} {{date}} {{time}} を値に置き換える(知らない名前はそのまま)。"""
    return _VAR.sub(lambda m: values.get(m.group(1), m.group(0)), template or "")


def check(value: str, op: str, match: str) -> bool:
    if op == "empty":
        return not value.strip()
    if op == "not_empty":
        return bool(value.strip())
    if op == "equals":
        return value.strip() == match.strip()
    if op == "not_contains":
        return match not in value
    if op == "regex":
        try:
            return re.search(match, value) is not None
        except re.error as e:
            raise ValueError(f"正規表現が正しくありません: {e}") from e
    return match in value


# ---- 保存 -------------------------------------------------------------------

class FlowStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS flows (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL, updated REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, flow_id TEXT NOT NULL, started REAL, finished REAL,"
                             " status TEXT, source TEXT, input TEXT, output TEXT, log TEXT)")
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_flow ON runs(flow_id, id)")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM flows ORDER BY created").fetchall()
        return [json.loads(r["data"]) for r in rows]

    def get(self, flow_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM flows WHERE id = ?", (flow_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save(self, flow: dict) -> dict:
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO flows (id, data, created, updated) VALUES (?, ?, ?, ?)"
                             " ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                             (flow["id"], json.dumps(flow, ensure_ascii=False), now, now))
        return flow

    def delete(self, flow_id: str) -> bool:
        with self._lock, self._db:
            deleted = self._db.execute("DELETE FROM flows WHERE id = ?", (flow_id,)).rowcount
            self._db.execute("DELETE FROM runs WHERE flow_id = ?", (flow_id,))
        return bool(deleted)

    def start_run(self, flow_id: str, source: str, given: str) -> int:
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO runs (flow_id, started, status, source, input, log) VALUES (?, ?, 'running', ?, ?, '[]')",
                                   (flow_id, time.time(), source, given[:MAX_OUTPUT]))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, output: str, log: list[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET finished = ?, status = ?, output = ?, log = ? WHERE id = ?",
                             (time.time(), status, output[:MAX_OUTPUT], json.dumps(log, ensure_ascii=False), run_id))
            # 履歴は流れごとに新しい 50 件まで
            self._db.execute("DELETE FROM runs WHERE flow_id = (SELECT flow_id FROM runs WHERE id = ?) AND id NOT IN"
                             " (SELECT id FROM runs WHERE flow_id = (SELECT flow_id FROM runs WHERE id = ?) ORDER BY id DESC LIMIT 50)",
                             (run_id, run_id))

    def runs(self, flow_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM runs WHERE flow_id = ? ORDER BY id DESC LIMIT ?", (flow_id, limit)).fetchall()
        return [{**dict(r), "log": json.loads(r["log"] or "[]")} for r in rows]

    def run(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return {**dict(row), "log": json.loads(row["log"] or "[]")} if row else None

    def last_runs(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT r.* FROM runs r JOIN (SELECT flow_id, MAX(id) AS id FROM runs GROUP BY flow_id) m"
                                    " ON r.id = m.id").fetchall()
        return {r["flow_id"]: {"id": r["id"], "status": r["status"], "started": r["started"], "finished": r["finished"]} for r in rows}


# ---- 実行 -------------------------------------------------------------------

class FlowEngine:
    """流れを動かす。時刻・間隔・ファイルのきっかけは、裏のスレッドが 10 秒ごとに確かめる。"""

    def __init__(self, store: FlowStore, app, publish: Callable[..., object] | None = None):
        self.store, self.app = store, app
        self.publish = publish or (lambda *a, **k: None)
        self._next: dict[str, float] = {}
        self._seen: dict[str, dict[str, float]] = {}
        self._running: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 一覧と保存 ----

    def list(self) -> list[dict]:
        last = self.store.last_runs()
        out = []
        for flow in self.store.all():
            out.append({**flow, "summary": describe_trigger(flow["trigger"]), "last_run": last.get(flow["id"]),
                        "next_run": self._next.get(flow["id"]) if flow.get("enabled") else None,
                        "running": flow["id"] in self._running.values()})
        return out

    def save(self, data: dict) -> dict:
        old = self.store.get(_text(data.get("id"), 40)) if data.get("id") else None
        flow = normalize_flow({**data, "hook_key": (old or {}).get("hook_key") or data.get("hook_key")})
        self.store.save(flow)
        with self._lock:
            self._next.pop(flow["id"], None)
            self._seen.pop(flow["id"], None)
        self.publish("flows")
        return flow

    def set_enabled(self, flow_id: str, enabled: bool) -> dict:
        flow = self.store.get(flow_id)
        if flow is None:
            raise KeyError("自動化が見つかりません")
        flow["enabled"] = enabled
        self.store.save(flow)
        with self._lock:
            self._next.pop(flow_id, None)
            self._seen.pop(flow_id, None)
        self.publish("flows")
        return flow

    def delete(self, flow_id: str) -> bool:
        ok = self.store.delete(flow_id)
        self.publish("flows")
        return ok

    # ---- 動かす ----

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="flows")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(10):
            try:
                self.tick()
            except Exception:  # 1つの流れの不具合で、ほかの流れを止めない
                pass

    def tick(self, now: float | None = None) -> list[int]:
        """時刻・間隔・ファイルのきっかけを確かめ、動かすものを動かす。戻り値は始めた実行の番号。"""
        now = time.time() if now is None else now
        started = []
        for flow in self.store.all():
            if not flow.get("enabled"):
                continue
            trigger = flow["trigger"]
            if trigger["type"] in ("schedule", "interval"):
                with self._lock:
                    due = self._next.get(flow["id"])
                    if due is None:
                        self._next[flow["id"]] = next_run(trigger, now) or 0
                        continue
                    if now < due:
                        continue
                    self._next[flow["id"]] = next_run(trigger, now) or 0
                started.append(self.run_async(flow["id"], "", trigger["type"]))
            elif trigger["type"] == "file":
                for payload in self._new_files(flow, now):
                    started.append(self.run_async(flow["id"], payload, "file"))
        return started

    def _workspace(self) -> Path | None:
        folder = self.app.cfg.workspace
        return Path(folder) if folder and Path(folder).is_dir() else None

    def _new_files(self, flow: dict, now: float) -> list[str]:
        root = self._workspace()
        if root is None:
            return []
        folder = (root / flow["trigger"]["folder"]).resolve()
        if root.resolve() not in folder.parents and folder != root.resolve():
            return []
        current = {}
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_file() and fnmatch.fnmatch(p.name.lower(), flow["trigger"]["pattern"].lower()):
                    try:
                        current[p.name] = p.stat().st_mtime
                    except OSError:
                        pass
        with self._lock:
            seen = self._seen.get(flow["id"])
            self._seen[flow["id"]] = current
        if seen is None:  # 最初は今あるファイルを覚えるだけ(前からあるものでは動かさない)
            return []
        payloads = []
        for name, mtime in current.items():
            if seen.get(name) != mtime:
                path = folder / name
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT] if path.stat().st_size < 2_000_000 else ""
                except OSError:
                    text = ""
                rel = path.relative_to(root.resolve()).as_posix()
                payloads.append(json.dumps({"path": rel, "name": name, "text": text}, ensure_ascii=False))
        return payloads

    def run_async(self, flow_id: str, given: str = "", source: str = "manual") -> int:
        flow = self.store.get(flow_id)
        if flow is None:
            raise KeyError("自動化が見つかりません")
        run_id = self.store.start_run(flow_id, source, given)
        with self._lock:
            self._running[run_id] = flow_id
        self.publish("flow_run", flow_id=flow_id, run_id=run_id, status="running")
        threading.Thread(target=self._run, args=(flow, given, run_id), daemon=True, name=f"flow-{run_id}").start()
        return run_id

    def run_now(self, flow_id: str, given: str = "", source: str = "manual") -> dict:
        """その場で最後まで動かして結果を返す(試験・Webhook の結果待ち用)。"""
        flow = self.store.get(flow_id)
        if flow is None:
            raise KeyError("自動化が見つかりません")
        run_id = self.store.start_run(flow_id, source, given)
        with self._lock:
            self._running[run_id] = flow_id
        self._run(flow, given, run_id)
        return self.store.run(run_id) or {}

    def _run(self, flow: dict, given: str, run_id: int) -> None:
        values = self._values(given)
        log: list[dict] = []
        status, output = "done", ""
        try:
            ctx = self._context()
            for number, step in enumerate(flow["steps"], 1):
                t0 = time.time()
                entry = {"n": number, "type": step["type"], "label": step.get("label") or STEP_TYPES[step["type"]]["label"]}
                try:
                    result = self._step(step, values, ctx)
                except FlowStop as e:
                    log.append({**entry, "status": "stopped", "output": str(e), "seconds": round(time.time() - t0, 2)})
                    status = "stopped"
                    break
                except Exception as e:  # 手順の失敗は記録して、そこで止める
                    log.append({**entry, "status": "error", "output": _error_text(e), "seconds": round(time.time() - t0, 2)})
                    status = "error"
                    break
                extras: dict[str, str] = {}
                if isinstance(result, tuple):  # (結果, 付け足しの値): 判断の確率など。{{step2.probability}} で使える
                    result, extras = result
                result = _text(result, MAX_OUTPUT)
                values[f"step{number}"] = values["prev"] = result
                for key, value in extras.items():
                    values[f"step{number}.{key}"] = values[f"prev.{key}"] = value
                if extras:
                    entry["extras"] = extras
                log.append({**entry, "status": "ok", "output": result[:4000], "seconds": round(time.time() - t0, 2)})
            output = values["prev"] if status == "done" else (log[-1]["output"] if log else "")
        finally:
            self.store.finish_run(run_id, status, output, log)
            with self._lock:
                self._running.pop(run_id, None)
            self.publish("flow_run", flow_id=flow["id"], run_id=run_id, status=status)
            self.publish("flows")

    def _values(self, given: str) -> dict[str, str]:
        t = time.localtime()
        values = {"trigger": given, "prev": given, "date": f"{t.tm_year}年{t.tm_mon}月{t.tm_mday}日({WEEKDAYS[t.tm_wday]})",
                  "time": f"{t.tm_hour}:{t.tm_min:02d}", "now": time.strftime("%Y-%m-%d %H:%M", t),
                  "weekday": f"{WEEKDAYS[t.tm_wday]}曜日", "day": f"{WEEKDAYS[t.tm_wday]}曜日"}
        try:  # Webhook やファイルのきっかけの JSON は、{{trigger.項目}} でも使える
            data = json.loads(given) if given.strip().startswith("{") else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                values[f"trigger.{key}"] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return values

    def _context(self) -> ToolContext:
        cfg = self.app.cfg
        # 確認なしで動かす(流れを作った人が PC の画面で手順を決めているため)。許可していない種類の手順は道具が断る
        return ToolContext(workspace=self._workspace(), allow_write=cfg.tools_write, allow_run=cfg.tools_run,
                           allow_net=cfg.tools_net, backup_dir=cfg.home / "backups", confirm=False, scratch_dir=cfg.home / "scratch")

    def _need(self, step: dict, ctx: ToolContext) -> None:
        need = STEP_TYPES[step["type"]].get("needs")
        ok = {"net": ctx.allow_net, "files": ctx.workspace is not None, "write": ctx.workspace is not None and ctx.allow_write,
              "run": ctx.allow_run}.get(need, True)
        if not ok:
            raise ToolError(f"この手順には、設定の{NEEDS_LABEL[need]}が必要です")

    def _step(self, step: dict, values: dict[str, str], ctx: ToolContext) -> str:
        kind = step["type"]
        self._need(step, ctx)
        r = {k: render(v, values) if isinstance(v, str) else v for k, v in step.items()}
        if kind == "ai":
            return self.app.run(r["prompt"]).text
        if kind == "decide":  # 文章を作らずに選ぶ(Jev のような判断)。確率は {{stepN.probability}}
            options = [o.strip() for o in re.split(r"[\n,、]", r["options"]) if o.strip()]
            decision = self.app.decide(r["question"], options or None, r["context"] if r["context"].strip() else values.get("prev", ""))
            return decision.answer, {"probability": f"{decision.probability:.2f}"}
        if kind == "fetch":
            return execute(ctx, "fetch_url", {"url": r["url"].strip()})
        if kind == "post":
            return web.post(r["url"], r["body"])
        if kind == "read_file":
            return execute(ctx, "read_file", {"path": r["path"]})
        if kind == "write_file":
            execute(ctx, "append_file" if r["mode"] == "append" else "write_file", {"path": r["path"], "content": r["content"]})
            return r["content"]
        if kind == "document":
            path = r["path"] if r["path"].lower().endswith("." + r["kind"]) else f"{r['path']}.{r['kind']}"
            tool = {"docx": "make_word", "xlsx": "make_excel", "pptx": "make_slides"}[r["kind"]]
            return execute(ctx, tool, {"path": path, ("data" if r["kind"] == "xlsx" else "content"): r["content"]})
        if kind == "command":
            return execute(ctx, "run_command", {"command": r["command"]})
        if kind == "notify":
            text = r["text"] or values.get("prev", "")
            target = {"pc": "pc", "phone": None, "all": None}[r["target"]]
            self.publish("push", target=target, kind="text", text=text[:2000], title=r["title"] or "自動化",
                         sender="自動化", only_phones=r["target"] == "phone")
            return text
        if kind == "condition":
            if not check(r["value"], r["op"], r["match"]):
                raise FlowStop(f"条件に合わないため止めました(「{r['value'][:40]}」{CONDITION_OPS[r['op']]}"
                               + (f"「{r['match'][:40]}」" if r["op"] not in ("empty", "not_empty") else "") + "、ではありませんでした)")
            return values.get("prev", "")
        if kind == "template":
            return r["text"]
        if kind == "wait":
            if self._stop.wait(min(600, int(r["seconds"] or 0))):
                raise FlowStop("終了のため止めました")
            return values.get("prev", "")
        raise ValueError(f"未知の手順: {kind}")


def _error_text(e: Exception) -> str:
    return str(e.args[0]) if getattr(e, "args", None) else f"{type(e).__name__}"


# ---- AIで作る -------------------------------------------------------------------

_BUILD_SYSTEM = """You design automations (workflows) for a personal AI assistant. Output JSON only.
A workflow has a trigger and steps that run in order. Step results can be used in later steps: {{prev}} is the previous step's result,
{{step1}} {{step2}} ... are results of the numbered steps, {{trigger}} is the trigger's input, {{date}} {{weekday}} {{time}} are today's date,
day of the week and time. Use only these variables.
Triggers: manual; schedule (time "HH:MM", days: list of weekday numbers 0=Mon..6=Sun); interval (minutes); webhook; file (folder in the workspace, pattern like "*.txt").
Steps (type: fields):
- ai: prompt (a request to the AI, can include {{prev}} etc.)
- decide: question, options (one per line; empty means yes/no), context (default {{prev}})  (picks one option with a probability;
  the result is the chosen option text, so a following condition step can branch on it)
- fetch: url (read a public web page as text)
- post: url, body (send text/JSON to a public webhook URL)
- read_file: path (a file in the workspace)
- write_file: path, content, mode (overwrite or append)
- document: kind (docx, xlsx or pptx), path, content (Markdown, or CSV for xlsx)
- command: command (a shell command)
- notify: title, text, target (all, phone or pc)
- condition: value, op (contains, not_contains, equals, regex, empty, not_empty), match  (stops the workflow when false)
- template: text
- wait: seconds
Use the fewest steps that do the job. Write prompts, titles and labels in the user's language. Give each step a short label."""

_BUILD_EXAMPLE_IN = "毎朝8時にNHKのニュースのページを読んで、3行に要約してスマホに通知して"
_BUILD_EXAMPLE_OUT = {"name": "朝のニュース要約", "trigger": {"type": "schedule", "time": "08:00", "days": [0, 1, 2, 3, 4, 5, 6]},
                      "steps": [{"type": "fetch", "label": "ニュースを読む", "url": "https://www3.nhk.or.jp/news/"},
                                {"type": "ai", "label": "3行に要約", "prompt": "次のニュースのページの内容を3行に要約して:\n{{prev}}"},
                                {"type": "notify", "label": "スマホに通知", "title": "今朝のニュース", "text": "{{prev}}", "target": "phone"}]}

_FIELDS = sorted({f for spec in STEP_TYPES.values() for f in spec["fields"]})
BUILD_SCHEMA = {
    "type": "object", "required": ["name", "trigger", "steps"],
    "properties": {
        "name": {"type": "string"},
        "trigger": {"type": "object", "required": ["type"], "properties": {
            "type": {"type": "string", "enum": list(TRIGGER_TYPES)}, "time": {"type": "string"},
            "days": {"type": "array", "items": {"type": "integer"}}, "minutes": {"type": "integer"},
            "folder": {"type": "string"}, "pattern": {"type": "string"}}},
        "steps": {"type": "array", "items": {"type": "object", "required": ["type", "label"], "properties": {
            "type": {"type": "string", "enum": list(STEP_TYPES)}, "label": {"type": "string"},
            **{f: {"type": "string"} for f in _FIELDS}}}},
    },
}


def build_messages(description: str) -> list[dict]:
    return [{"role": "system", "content": _BUILD_SYSTEM},
            {"role": "user", "content": _BUILD_EXAMPLE_IN},
            {"role": "assistant", "content": json.dumps(_BUILD_EXAMPLE_OUT, ensure_ascii=False)},
            {"role": "user", "content": description.strip()[:1500]}]


def build_flow(app, description: str) -> dict:
    """「毎朝8時に〜して通知」のような説明から、流れの下書きを作る(計測で最も正確なAIに、JSON の形で書かせる)。"""
    if not description.strip():
        raise ValueError("作りたい自動化の説明を書いてください")
    candidates = [m for m in app.models() if m.can_manage] or app.models()
    model = app._most_accurate("general") or max(candidates, key=lambda m: m.size_bytes)
    gen = app._generate(model, build_messages(description), temperature=0.0, max_tokens=1500, json_schema=BUILD_SCHEMA)
    return draft_from_ai(gen.text)


def draft_from_ai(text: str) -> dict:
    """AIの出力(JSON)を、保存できる形の下書きにする。直せない形なら ValueError。"""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError("AIが流れの形を作れませんでした。言い方を変えて、もう一度試してください") from e
    if isinstance(data, dict):
        for step in data.get("steps") or []:
            if isinstance(step, dict) and step.get("type") == "wait" and isinstance(step.get("seconds"), (int, float)):
                step["seconds"] = str(step["seconds"])
    flow = normalize_flow({**data, "enabled": False})  # 下書きは止めた状態で返す(内容を確かめてから使う)
    flow.pop("id", None)
    flow.pop("hook_key", None)
    return flow
