"""通知の配信と、実行(AIへの依頼1件)の管理。

実行は通信から切り離して裏で走らせる。スマホの電波が途切れても実行は続き、結果は会話に保存される。
画面は実行の出来事(文字・進行・確認)を購読するだけで、途中から購読し直すこともできる。
生成中の文章は一定間隔で会話にも保存するので、別の端末からも進み具合が見える。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .backends import BackendError
from .chats import ChatStore
from .tools import ToolError


class EventBus:
    """端末へ配る出来事の列。各端末は「最後に受け取った番号」より後を待ち受ける。"""

    def __init__(self, keep: int = 2000):
        self._cond = threading.Condition()
        self._items: deque[tuple[int, dict]] = deque(maxlen=keep)
        self._seq = 0

    def publish(self, event_type: str, *, target: str | None = None, **data) -> int:
        with self._cond:
            self._seq += 1
            self._items.append((self._seq, {**data, "type": event_type, "target": target}))
            self._cond.notify_all()
            return self._seq

    @property
    def last(self) -> int:
        with self._cond:
            return self._seq

    def wait(self, after: int, timeout: float, who: str = "pc") -> tuple[int, list[dict]]:
        """after より後の出来事のうち、自分宛て(または全員宛て)のもの。無ければ timeout まで待つ。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                fresh = [(s, e) for s, e in self._items if s > after]
                if fresh:
                    mine = [e for _, e in fresh if e.get("target") in (None, who)]
                    return fresh[-1][0], mine
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return after, []
                self._cond.wait(remaining)


class Cancelled(Exception):
    pass


@dataclass
class Run:
    id: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    who: str
    started: float = field(default_factory=time.time)
    events: list = field(default_factory=list)
    done: bool = False
    status: str = "running"  # running / done / error / cancelled
    cancel: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)
    allow_all: bool = False

    def push(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def finish(self, status: str) -> None:
        with self.cond:
            self.done, self.status = True, status
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.events) <= after and not self.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            return self.events[after:], self.done

    def summary(self) -> dict:
        return {"run_id": self.id, "conversation_id": self.conversation_id, "user_message_id": self.user_message_id,
                "assistant_message_id": self.assistant_message_id, "who": self.who, "started": self.started,
                "status": self.status, "done": self.done}


# 直前の会話を踏まえないと意味が通らない依頼(「それを英語で」「もっと短く」など)の手がかり
_FOLLOWUP = re.compile(
    r"^(それ|これ|あれ|その|この|あの|さっき|先ほど|上の|上記|前の|今の|もう(一度|少し|ちょっと)|もっと|続き|続けて|他に|ほかに|"
    r"次は|次に|じゃあ|では|なら|でも|あと|さらに|同じ|逆に|今度は|ありがとう|了解|違う|ちがう|"
    r"then\b|and\b|also\b|more\b|again\b|continue|it\b|that\b|this\b|those\b|what about|how about|why\b|ok\b|thanks)", re.I)


_REFERENCE = re.compile(r"それ|これ|あれ|上記|前述|さっき|先ほど|今の|前の|上の|同じ|その(?![後他])|この(?![後先])|あの|"
                        r"\b(it|that|this|these|those|above|previous)\b", re.I)


def needs_context(task: str, has_history: bool) -> bool:
    """会話の流れが必要そうか(必要なければ単独の依頼として扱い、キャッシュや学習済みの振り分けが効くようにする)。

    続きの発言を単独と誤る方が害が大きい(話が通じない)ので、迷う場合は流れを使う側に倒す。
    """
    if not has_history:
        return False
    text = task.strip()
    if len(text) > 80 and (":" in text or ":" in text or "\n" in text):
        return False  # 処理する本文を自分で含んでいる
    if len(text) <= 15 or _FOLLOWUP.search(text):
        return True
    return len(text) <= 60 and bool(_REFERENCE.search(text))


class RunManager:
    def __init__(self, app, chats: ChatStore, bus: EventBus, result_dict: Callable):
        self.app, self.chats, self.bus = app, chats, bus
        self.result_dict = result_dict
        self._runs: dict[str, Run] = {}
        self._slots: dict[str, dict] = {}  # 確認待ち: id → {run, event, allow, all}
        self._lock = threading.Lock()

    # ---- 開始 ----

    def start(self, *, task: str, who: str, conversation_id: str | None = None, user_message_id: str | None = None,
              images: list[bytes] | None = None, context: str = "auto", options: dict | None = None) -> Run:
        task = task.strip()
        if not task:
            raise ValueError("依頼が空です")
        options = options or {}
        chats = self.chats
        conversation = chats.conversation(conversation_id) if conversation_id else None
        if conversation is None or conversation["deleted"]:
            conversation = chats.create_conversation(_title(task), origin=who,
                                                     id=conversation_id if conversation is None and conversation_id else None)
        cid = conversation["id"]
        previous = chats.history_pairs(cid, turns=3)
        user = chats.add_message(cid, "user", task, id=user_message_id, meta={"device": who, "images": len(images or [])})
        assistant = chats.add_message(cid, "assistant", "", meta={"status": "running", "device": "pc",
                                                                   "agent": bool(options.get("agent"))})
        if not conversation["title"]:
            chats.update_conversation(cid, title=_title(task))
        use_history = context == "on" or (context == "auto" and needs_context(task, bool(previous)))
        run = Run(uuid.uuid4().hex, cid, user["id"], assistant["id"], who)
        with self._lock:
            self._runs[run.id] = run
            for old in [r for r in self._runs.values() if r.done and time.time() - r.started > 3600]:
                self._runs.pop(old.id, None)
        run.push({"type": "run", **run.summary(), "context": use_history})
        self.bus.publish("run_started", **run.summary())
        threading.Thread(target=self._work, args=(run, task, previous if use_history else None, images, options),
                         daemon=True, name=f"run-{run.id[:8]}").start()
        return run

    def regenerate(self, *, message_id: str, who: str, options: dict | None = None) -> Run:
        """回答をもう一度作る。元の依頼はそのままに、その回答だけを作り直す(キャッシュは使わず、検証つきで丁寧に)。"""
        chats = self.chats
        target = chats.message(message_id)
        if target is None or target["deleted"] or target["role"] != "assistant":
            raise KeyError("回答が見つかりません")
        if (target.get("meta") or {}).get("status") == "running":
            raise ValueError("この回答はまだ作成中です")
        cid = target["conversation_id"]
        messages = chats.messages(cid)
        index = next(i for i, m in enumerate(messages) if m["id"] == message_id)
        user = next((m for m in reversed(messages[:index]) if m["role"] == "user"), None)
        if user is None:
            raise ValueError("元の依頼が見つかりません")
        if (user.get("meta") or {}).get("images"):
            raise ValueError("画像つきの依頼は、画像を添えてもう一度送ってください")
        options = {"use_cache": False, "verify": True, **(options or {})}
        previous = chats.history_pairs(cid, before=user["id"], turns=3)
        old = target.get("meta") or {}
        # 前の回答への評価・エラー・出典は、作り直した回答には引き継がない
        chats.update_message(message_id, content="", meta={"status": "running", "device": "pc", "agent": bool(options.get("agent") or old.get("agent")),
                                                            "regenerated": int(old.get("regenerated") or 0) + 1, "feedback": None,
                                                            "error": None, "sources": [], "escalated": None},
                             touch_conversation=True)
        if old.get("agent"):
            options["agent"] = True
        use_history = bool(previous) and needs_context(user["content"], True)
        run = Run(uuid.uuid4().hex, cid, user["id"], message_id, who)
        with self._lock:
            self._runs[run.id] = run
        run.push({"type": "run", **run.summary(), "context": use_history})
        self.bus.publish("run_started", **run.summary())
        threading.Thread(target=self._work, args=(run, user["content"], previous if use_history else None, None, options),
                         daemon=True, name=f"run-{run.id[:8]}").start()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def active(self) -> list[dict]:
        with self._lock:
            return [r.summary() for r in self._runs.values() if not r.done]

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.done:
            return False
        run.cancel.set()
        with self._lock:  # 確認待ちで止まっていたら、拒否として起こす
            for slot in self._slots.values():
                if slot["run"] is run:
                    slot["event"].set()
        return True

    # ---- 確認 ----

    def approve(self, slot_id: str, allow: bool, all_: bool = False) -> bool:
        with self._lock:
            slot = self._slots.get(slot_id)
        if slot is None:
            return False
        slot["allow"], slot["all"] = allow, all_
        slot["event"].set()
        return True

    def _approver(self, run: Run) -> Callable[[dict], bool]:
        def approve(request: dict) -> bool:
            if run.allow_all:
                return True
            if run.cancel.is_set():
                return False
            slot_id = uuid.uuid4().hex
            slot = {"run": run, "event": threading.Event(), "allow": False, "all": False}
            with self._lock:
                self._slots[slot_id] = slot
            event = {"type": "approval", "id": slot_id, "run_id": run.id, "conversation_id": run.conversation_id, **request}
            run.push(event)
            self.bus.publish("approval", **{k: v for k, v in event.items() if k != "type"})  # 別の端末からも許可できるように
            try:
                answered = slot["event"].wait(300)
                allowed = answered and slot["allow"] and not run.cancel.is_set()
                run.allow_all = allowed and slot["all"]
                return allowed
            finally:
                with self._lock:
                    self._slots.pop(slot_id, None)
                self.bus.publish("approval_done", id=slot_id, run_id=run.id, allowed=slot["allow"])
                run.push({"type": "approval_done", "id": slot_id, "allowed": slot["allow"]})
        return approve

    # ---- 実行 ----

    def _work(self, run: Run, task: str, history, images, options: dict) -> None:
        chats = self.chats
        parts: list[str] = []
        last_save = [time.monotonic()]

        def check() -> None:
            if run.cancel.is_set():
                raise Cancelled()

        def on_token(piece: str) -> None:
            check()
            parts.append(piece)
            run.push({"type": "token", "text": piece})
            if time.monotonic() - last_save[0] > 1.5:  # 他の端末にも途中経過を見せる
                last_save[0] = time.monotonic()
                try:
                    chats.update_message(run.assistant_message_id, content="".join(parts))
                except (KeyError, ValueError):
                    pass

        def on_stage(stage: str, info: dict) -> None:
            check()
            run.push({"type": "stage", "stage": stage, **info})
            if stage in ("agent_step", "route", "worker"):
                self.bus.publish("run_stage", run_id=run.id, conversation_id=run.conversation_id, stage=stage,
                                 **{k: v for k, v in info.items() if k in ("label", "detail", "n", "role", "model")})

        status = "done"
        try:
            result = self.app.run(
                task, role=options.get("role") or None, model=options.get("model") or None, verify=bool(options.get("verify")),
                swarm=options.get("swarm"), use_cache=options.get("use_cache", True) is not False, history=history,
                agent=bool(options.get("agent")), approve=self._approver(run), images=images or None,
                folder=options.get("folder") or None,
                on_token=on_token, on_stage=on_stage)
            data = self.result_dict(result)
            meta = {"status": "done", "interaction_id": result.id, "role": result.role, "model": result.model,
                    "source": result.source, "route_source": result.route_source, "tokens": result.tokens,
                    "latency_s": round(result.latency_s, 3), "examples_used": result.examples_used,
                    "tool_calls": [{k: c.get(k) for k in ("name", "label", "detail", "ok")} for c in result.tool_calls],
                    "subresults": data.get("subresults", []), "escalated": data.get("escalated"),
                    "sources": data.get("sources") or [], "changes": data.get("changes") or []}
            message = chats.update_message(run.assistant_message_id, content=result.text, meta=meta, touch_conversation=True)
            run.push({"type": "done", "result": data, "message": message})
            self._name_conversation(run.conversation_id, task)
            if result.source not in ("memory", "calc"):
                self._suggest_memory(run.assistant_message_id, task)
        except Cancelled:
            status = "cancelled"
            text = "".join(parts).rstrip()
            chats.update_message(run.assistant_message_id, content=(text + "\n\n" if text else "") + "(停止しました)",
                                 meta={"status": "cancelled"}, touch_conversation=True)
            run.push({"type": "cancelled"})
        except (BackendError, ValueError, ToolError, KeyError, OSError) as e:
            status = "error"
            self._fail(run, str(e.args[0]) if e.args else str(e))
        except Exception as e:  # 想定外の不具合も、会話に残して利用者に見せる
            status = "error"
            self._fail(run, f"予期しないエラー: {type(e).__name__}: {e}")
        finally:
            run.finish(status)
            self.bus.publish("run_finished", **run.summary())

    def _name_conversation(self, cid: str, task: str) -> None:
        """最初のやり取りが終わったら、依頼の文の頭(途中で切れた題名)の代わりに、内容を表す短い題名を付ける(裏で)。
        利用者が題名を変えていたら、そのままにする。"""
        if not getattr(self.app.cfg, "auto_title", True) or not hasattr(self.app, "make_title"):
            return
        conversation = self.chats.conversation(cid)
        if conversation is None or conversation["title"] != _title(task):
            return
        if sum(1 for m in self.chats.messages(cid) if m["role"] == "user") != 1:
            return

        def work() -> None:
            try:
                title = self.app.make_title(task)
            except Exception:  # 題名は付かなくても困らない(元の題名のまま)
                return
            current = self.chats.conversation(cid)
            if title and current is not None and current["title"] == _title(task) and title != current["title"]:
                self.chats.update_conversation(cid, title=title)

        threading.Thread(target=work, daemon=True, name="title").start()

    def _suggest_memory(self, message_id: str, task: str) -> None:
        """発言に利用者についての事実があれば、「覚えますか」の候補を回答に付ける(裏で。判断は1語の生成なので軽い)。"""
        if not getattr(self.app.cfg, "memory_suggest", True) or not hasattr(self.app, "suggest_memory"):
            return

        def work() -> None:
            try:
                fact = self.app.suggest_memory(task)
            except Exception:  # 候補は出なくても困らない
                return
            if fact:
                try:
                    self.chats.update_message(message_id, meta={"memory_suggestion": fact})
                except (KeyError, ValueError):
                    pass

        threading.Thread(target=work, daemon=True, name="memory-gate").start()

    def _fail(self, run: Run, message: str) -> None:
        try:
            self.chats.update_message(run.assistant_message_id, content=f"エラー: {message}", meta={"status": "error", "error": message},
                                      touch_conversation=True)
        except (KeyError, ValueError):
            pass
        run.push({"type": "error", "message": message})


def _title(task: str) -> str:
    first = task.strip().splitlines()[0] if task.strip() else ""
    first = re.sub(r"\s+", " ", first)
    return first[:40] + ("…" if len(first) > 40 else "")
