"""会話(チャット)の保存と、端末間の同期。

すべての変更に通し番号(rev)を振る。端末は「最後に受け取った rev」を覚えておき、それより後の変更だけを取りに来る。
端末からの変更は updated(更新時刻)が新しい方を採用する(後勝ち)。削除は消さずに deleted=1 で残す
(他の端末へ「消えた」ことを伝えるため)。スマホがオフラインの間に作った会話も、つながった時点で取り込める。
"""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'pc',     -- 作った端末(pc / 端末のid)
    meta TEXT NOT NULL DEFAULT '{}',
    rev INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_rev ON conversations(rev);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,                     -- user / assistant / system
    content TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}',        -- 担当・モデル・使った道具・評価など
    rev INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created);
CREATE INDEX IF NOT EXISTS idx_messages_rev ON messages(rev);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

ROLES = ("user", "assistant", "system")
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_CONTENT = 200_000
MAX_META = 32_000


def new_id() -> str:
    return uuid.uuid4().hex


def _meta_json(meta) -> str:
    text = json.dumps(meta if isinstance(meta, dict) else {}, ensure_ascii=False)
    if len(text) > MAX_META:
        raise ValueError("メタ情報が大きすぎます")
    return text


def _conversation(row) -> dict:
    data = {k: row[k] for k in row.keys()}
    data["meta"] = json.loads(data.get("meta") or "{}")
    data["pinned"], data["deleted"] = bool(data["pinned"]), bool(data["deleted"])
    return data


def _message(row) -> dict:
    data = {k: row[k] for k in row.keys()}
    data["meta"] = json.loads(data.get("meta") or "{}")
    data["deleted"] = bool(data["deleted"])
    return data


class ChatStore:
    """Store と同じ SQLite を使う(同じロックで守る)。"""

    def __init__(self, store):
        self._db = store._db
        self._lock = store._lock
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO sync_state (key, value) VALUES ('rev', 0)")
            # このデータベースの識別子。PC のデータを作り直したとき、端末が「続きから」ではなく最初から同期し直せるように
            self._db.execute("INSERT OR IGNORE INTO sync_state (key, value) VALUES ('db', ?)", (secrets.randbits(62),))
            self.db_id = format(int(self._db.execute("SELECT value FROM sync_state WHERE key = 'db'").fetchone()[0]), "x")
        self.listeners: list[Callable[[int], None]] = []

    # ---- 内部 ----

    def _bump(self) -> int:
        """ロックとトランザクションの内側で呼ぶ。"""
        self._db.execute("UPDATE sync_state SET value = value + 1 WHERE key = 'rev'")
        return int(self._db.execute("SELECT value FROM sync_state WHERE key = 'rev'").fetchone()[0])

    def _notify(self, rev: int) -> None:
        for listener in list(self.listeners):
            try:
                listener(rev)
            except Exception:  # 通知先の不具合で保存を失敗させない
                pass

    def rev(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT value FROM sync_state WHERE key = 'rev'").fetchone()[0])

    # ---- 会話 ----

    def create_conversation(self, title: str = "", *, origin: str = "pc", id: str | None = None, meta: dict | None = None) -> dict:
        cid = id or new_id()
        if not _ID.match(cid):
            raise ValueError("会話の id が不正です")
        now = time.time()
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
                raise ValueError("同じ id の会話があります")
            rev = self._bump()
            self._db.execute("INSERT INTO conversations (id, title, created, updated, origin, meta, rev) VALUES (?,?,?,?,?,?,?)",
                             (cid, title[:300], now, now, origin[:64], _meta_json(meta or {}), rev))
        self._notify(rev)
        return self.conversation(cid)

    def conversation(self, cid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        return _conversation(row) if row else None

    def update_conversation(self, cid: str, *, title: str | None = None, pinned: bool | None = None,
                            deleted: bool | None = None, meta: dict | None = None) -> dict:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
            if row is None:
                raise KeyError("会話が見つかりません")
            merged = {**json.loads(row["meta"] or "{}"), **(meta or {})}
            rev, now = self._bump(), time.time()
            self._db.execute(
                "UPDATE conversations SET title = ?, pinned = ?, deleted = ?, meta = ?, updated = ?, rev = ? WHERE id = ?",
                (row["title"] if title is None else title[:300], row["pinned"] if pinned is None else int(pinned),
                 row["deleted"] if deleted is None else int(deleted), _meta_json(merged), now, rev, cid))
            if deleted:
                self._db.execute("UPDATE messages SET deleted = 1, updated = ?, rev = ? WHERE conversation_id = ? AND deleted = 0",
                                 (now, rev, cid))
        self._notify(rev)
        return self.conversation(cid)

    def list_conversations(self, query: str | None = None, limit: int = 300, include_deleted: bool = False) -> list[dict]:
        where, args = ["1 = 1"], []
        if not include_deleted:
            where.append("c.deleted = 0")
        if query:
            like = f"%{query}%"
            where.append("(c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.deleted = 0 AND m.content LIKE ?))")
            args += [like, like]
        sql = ("SELECT c.*, (SELECT content FROM messages m WHERE m.conversation_id = c.id AND m.deleted = 0 ORDER BY m.created DESC LIMIT 1) AS preview,"
               " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id AND m.deleted = 0) AS count"
               f" FROM conversations c WHERE {' AND '.join(where)} ORDER BY c.pinned DESC, c.updated DESC LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        out = []
        for row in rows:
            data = _conversation(row)
            data["preview"] = (data.get("preview") or "")[:160]
            out.append(data)
        return out

    # ---- メッセージ ----

    def add_message(self, conversation_id: str, role: str, content: str, *, meta: dict | None = None,
                    id: str | None = None, created: float | None = None) -> dict:
        if role not in ROLES:
            raise ValueError("role が不正です")
        if len(content) > MAX_CONTENT:
            raise ValueError("メッセージが長すぎます")
        mid = id or new_id()
        if not _ID.match(mid):
            raise ValueError("メッセージの id が不正です")
        now = time.time()
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM conversations WHERE id = ? AND deleted = 0", (conversation_id,)).fetchone():
                raise KeyError("会話が見つかりません")
            if self._db.execute("SELECT 1 FROM messages WHERE id = ?", (mid,)).fetchone():
                raise ValueError("同じ id のメッセージがあります")
            # 同じ会話の中では、後から足したメッセージの時刻を必ず後にする。依頼と回答の作成がほぼ同時でも、
            # 端末側(時刻だけで並べる)で順番が入れ替わらないように
            last = self._db.execute("SELECT MAX(created) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
            if created is None and last is not None and now <= last:
                now = last + 1e-6
            rev = self._bump()
            self._db.execute("INSERT INTO messages (id, conversation_id, role, content, created, updated, meta, rev) VALUES (?,?,?,?,?,?,?,?)",
                             (mid, conversation_id, role, content, created or now, now, _meta_json(meta or {}), rev))
            self._db.execute("UPDATE conversations SET updated = ?, rev = ? WHERE id = ?", (now, rev, conversation_id))
        self._notify(rev)
        return self.message(mid)

    def message(self, mid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        return _message(row) if row else None

    def update_message(self, mid: str, *, content: str | None = None, meta: dict | None = None, deleted: bool | None = None,
                       touch_conversation: bool = False) -> dict:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            if row is None:
                raise KeyError("メッセージが見つかりません")
            merged = {**json.loads(row["meta"] or "{}"), **(meta or {})}
            rev, now = self._bump(), time.time()
            self._db.execute("UPDATE messages SET content = ?, meta = ?, deleted = ?, updated = ?, rev = ? WHERE id = ?",
                             (row["content"] if content is None else content[:MAX_CONTENT], _meta_json(merged),
                              row["deleted"] if deleted is None else int(deleted), now, rev, mid))
            if touch_conversation:
                self._db.execute("UPDATE conversations SET updated = ?, rev = ? WHERE id = ?", (now, rev, row["conversation_id"]))
        self._notify(rev)
        return self.message(mid)

    def messages(self, conversation_id: str, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM messages WHERE conversation_id = ?" + ("" if include_deleted else " AND deleted = 0") + " ORDER BY created, rowid"
        with self._lock:
            rows = self._db.execute(sql, (conversation_id,)).fetchall()
        return [_message(r) for r in rows]

    def history_pairs(self, conversation_id: str, *, before: str | None = None, turns: int = 3) -> list[tuple[str, str]]:
        """直近の (ユーザーの発言, 回答) の組。before を指定したら、そのメッセージより前だけ。"""
        pairs, pending = [], None
        for m in self.messages(conversation_id):
            if m["id"] == before:
                break
            if m["role"] == "user":
                pending = m["content"]
            elif m["role"] == "assistant" and pending is not None and m["content"].strip():
                pairs.append((pending, m["content"]))
                pending = None
        return pairs[-turns:]

    # ---- 同期 ----

    def changes(self, since: int, limit: int = 500) -> dict:
        """since より後の変更。{rev: 次に使う since, conversations, messages, more, db}。

        since が今の番号より大きい(端末が別のデータベースと同期していた)ときは、最初から送り直す。
        """
        limit = max(1, min(2000, limit))
        with self._lock:
            current = int(self._db.execute("SELECT value FROM sync_state WHERE key = 'rev'").fetchone()[0])
            if since > current:
                since = 0
            convs = self._db.execute("SELECT * FROM conversations WHERE rev > ? ORDER BY rev LIMIT ?", (since, limit)).fetchall()
            msgs = self._db.execute("SELECT * FROM messages WHERE rev > ? ORDER BY rev LIMIT ?", (since, limit)).fetchall()
            items = sorted([(r["rev"], 0, r) for r in convs] + [(r["rev"], 1, r) for r in msgs], key=lambda t: (t[0], t[1]))
            more = len(convs) == limit or len(msgs) == limit
            if more:
                # 区切りの rev は丸ごと含める(会話の削除などは、1つの rev で多数のメッセージを変える)。
                # 区切りより前の rev は、両方の表から漏れなく取れている(各表の上限の内側にあるため)。
                upto = items[limit - 1][0] if len(items) >= limit else items[-1][0]
                group_c = self._db.execute("SELECT * FROM conversations WHERE rev = ?", (upto,)).fetchall()
                group_m = self._db.execute("SELECT * FROM messages WHERE rev = ?", (upto,)).fetchall()
                items = [i for i in items if i[0] < upto] + [(upto, 0, r) for r in group_c] + [(upto, 1, r) for r in group_m]
            else:
                upto = current
        return {"rev": upto, "more": more, "db": self.db_id,
                "conversations": [_conversation(r) for _, kind, r in items if kind == 0],
                "messages": [_message(r) for _, kind, r in items if kind == 1]}

    def apply(self, payload: dict, origin: str) -> dict:
        """他の端末からの変更を取り込む(後勝ち)。戻り値は {applied, skipped, rev}。"""
        conversations = payload.get("conversations") or []
        messages = payload.get("messages") or []
        if not isinstance(conversations, list) or not isinstance(messages, list) or len(conversations) + len(messages) > 5000:
            raise ValueError("同期データの形式が不正です")
        applied = skipped = 0
        last_rev = None
        with self._lock, self._db:
            for c in conversations:
                cid = str(c.get("id", ""))
                if not _ID.match(cid):
                    skipped += 1
                    continue
                updated = float(c.get("updated") or time.time())
                row = self._db.execute("SELECT updated FROM conversations WHERE id = ?", (cid,)).fetchone()
                if row is not None and row["updated"] >= updated:
                    skipped += 1
                    continue
                rev = last_rev = self._bump()
                values = (str(c.get("title", ""))[:300], float(c.get("created") or updated), updated, int(bool(c.get("pinned"))),
                          int(bool(c.get("deleted"))), _meta_json(c.get("meta") or {}), rev)
                if row is None:
                    self._db.execute("INSERT INTO conversations (title, created, updated, pinned, deleted, meta, rev, id, origin)"
                                     " VALUES (?,?,?,?,?,?,?,?,?)", (*values, cid, str(c.get("origin") or origin)[:64]))
                else:
                    self._db.execute("UPDATE conversations SET title = ?, created = ?, updated = ?, pinned = ?, deleted = ?, meta = ?,"
                                     " rev = ? WHERE id = ?", (*values, cid))
                applied += 1
            for m in messages:
                mid, cid = str(m.get("id", "")), str(m.get("conversation_id", ""))
                role, content = m.get("role"), m.get("content")
                if not (_ID.match(mid) and _ID.match(cid)) or role not in ROLES or not isinstance(content, str) or len(content) > MAX_CONTENT:
                    skipped += 1
                    continue
                if not self._db.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
                    skipped += 1
                    continue
                updated = float(m.get("updated") or time.time())
                row = self._db.execute("SELECT updated FROM messages WHERE id = ?", (mid,)).fetchone()
                if row is not None and row["updated"] >= updated:
                    skipped += 1
                    continue
                rev = last_rev = self._bump()
                values = (content, float(m.get("created") or updated), updated, int(bool(m.get("deleted"))), _meta_json(m.get("meta") or {}), rev)
                if row is None:
                    self._db.execute("INSERT INTO messages (content, created, updated, deleted, meta, rev, id, conversation_id, role)"
                                     " VALUES (?,?,?,?,?,?,?,?,?)", (*values, mid, cid, role))
                    self._db.execute("UPDATE conversations SET updated = MAX(updated, ?), rev = ? WHERE id = ?", (updated, rev, cid))
                else:
                    self._db.execute("UPDATE messages SET content = ?, created = ?, updated = ?, deleted = ?, meta = ?, rev = ? WHERE id = ?",
                                     (*values, mid))
                applied += 1
        if last_rev is not None:
            self._notify(last_rev)
        return {"applied": applied, "skipped": skipped, "rev": self.rev()}

    # ---- 書き出し ----

    def export_markdown(self, cid: str) -> str:
        conv = self.conversation(cid)
        if conv is None:
            raise KeyError("会話が見つかりません")
        lines = [f"# {conv['title'] or '無題の会話'}", ""]
        for m in self.messages(cid):
            who = {"user": "あなた", "assistant": "Celestial", "system": "システム"}[m["role"]]
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["created"]))
            lines += [f"## {who}({stamp})", "", m["content"], ""]
        return "\n".join(lines)
