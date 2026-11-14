"""全実行履歴を保持するSQLiteストア。キャッシュ・ルーター学習・手本記憶・モデル選択はすべてここから導出する。"""

from __future__ import annotations

import json
import sqlite3
from array import array
import threading
import time
from pathlib import Path

from ..textsim import task_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT NOT NULL,            -- llm / cache / swarm / rejected
    route_source TEXT NOT NULL,      -- learned / manager / heuristic / forced / plan / cache
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    overhead_tokens INTEGER NOT NULL DEFAULT 0,  -- 管理AIが振り分け・検証・分解に使った分
    latency_s REAL NOT NULL DEFAULT 0,           -- ユーザーから見た総所要時間
    gen_latency_s REAL NOT NULL DEFAULT 0,       -- ワーカーの生成時間
    feedback INTEGER,                -- 1 = good, -1 = bad, NULL = 未評価
    verified INTEGER NOT NULL DEFAULT 0,         -- 1 = 検証合格, -1 = 不合格
    reward REAL NOT NULL DEFAULT 0,
    parent_id INTEGER,               -- スウォームのサブタスクなら親のid
    origin_id INTEGER                -- キャッシュヒットなら元のid
);
CREATE INDEX IF NOT EXISTS idx_interactions_hash ON interactions(task_hash);
CREATE TABLE IF NOT EXISTS study (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    task_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    model TEXT NOT NULL,             -- 手本の回答を書いたモデル(教師)
    answer TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    task TEXT NOT NULL,
    task_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    chosen TEXT NOT NULL,
    rejected TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'study',
    UNIQUE(task_hash, chosen, rejected)
);
CREATE TABLE IF NOT EXISTS embeddings (
    task_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vec BLOB NOT NULL,               -- float32 の並び
    PRIMARY KEY (task_hash, model)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bench_runs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    label TEXT NOT NULL,
    suite TEXT NOT NULL,             -- タスク集の識別子(内容のハッシュ)。同じ suite 同士だけ比較できる
    n INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    route_n INTEGER NOT NULL,
    route_correct INTEGER NOT NULL,
    avg_tokens REAL NOT NULL,
    avg_latency_s REAL NOT NULL,
    by_role TEXT NOT NULL            -- JSON: {役割: [正解数, 件数, 合計秒]}
);
CREATE TABLE IF NOT EXISTS bench_items (  -- 計測の問題ごとの結果。答え直す境目(確からしさ)の学習に使う
    run_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    role TEXT,
    model TEXT NOT NULL,
    ok INTEGER NOT NULL,
    confidence REAL,                 -- 最初に答えたモデルの確からしさ(出だしの平均の対数確率)
    latency_s REAL NOT NULL,
    escalated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS kb_docs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL UNIQUE,     -- ファイルパス・URL・note:<title>
    title TEXT NOT NULL,
    kind TEXT NOT NULL,              -- file / url / note
    tags TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    chars INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    UNIQUE(doc_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
"""


def compute_reward(feedback: int | None, verified: int, gen_latency_s: float) -> float:
    """品質 − 時間コスト。モデル選択(バンディット)の報酬になる。"""
    if feedback == 1:
        quality = 1.0
    elif feedback == -1:
        quality = 0.0
    elif verified == 1:
        quality = 0.85
    elif verified == -1:
        quality = 0.3
    else:
        quality = 0.7  # 文句が出なかった = 暗黙の合格
    return quality - 0.3 * min(1.0, gen_latency_s / 60.0)


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(study)").fetchall()}
            if "verified" not in cols:
                self._db.execute("ALTER TABLE study ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def log(self, *, task: str, role: str, model: str, answer: str, source: str, route_source: str,
            prompt_tokens: int = 0, completion_tokens: int = 0, overhead_tokens: int = 0,
            latency_s: float = 0.0, gen_latency_s: float = 0.0, verified: int = 0,
            parent_id: int | None = None, origin_id: int | None = None) -> int:
        reward = compute_reward(None, verified, gen_latency_s)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO interactions (ts, task, task_hash, role, model, answer, source, route_source,"
                " prompt_tokens, completion_tokens, overhead_tokens, latency_s, gen_latency_s, verified, reward,"
                " parent_id, origin_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), task, task_hash(task), role, model, answer, source, route_source,
                 prompt_tokens, completion_tokens, overhead_tokens, latency_s, gen_latency_s, verified, reward,
                 parent_id, origin_id),
            )
            return int(cur.lastrowid)

    def last_interaction_id(self) -> int:
        rows = self._query("SELECT MAX(id) AS last FROM interactions")
        return int(rows[0]["last"] or 0) if rows else 0

    def get(self, interaction_id: int) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM interactions WHERE id = ?", (interaction_id,))
        return rows[0] if rows else None

    def set_feedback(self, interaction_id: int, value: int) -> sqlite3.Row | None:
        """評価を記録する。キャッシュヒットへの評価は元の回答にも伝える。戻り値は元(実体)の行。"""
        row = self.get(interaction_id)
        if row is None:
            return None
        ids = [interaction_id]
        if row["origin_id"]:
            ids.append(row["origin_id"])
        with self._lock, self._db:
            for i in ids:
                r = self._db.execute("SELECT verified, gen_latency_s FROM interactions WHERE id = ?", (i,)).fetchone()
                if r:
                    self._db.execute(
                        "UPDATE interactions SET feedback = ?, reward = ? WHERE id = ?",
                        (value, compute_reward(value, r["verified"], r["gen_latency_s"]), i),
                    )
        return self.get(ids[-1])

    def set_parent(self, ids: list[int], parent_id: int) -> None:
        with self._lock, self._db:
            self._db.executemany("UPDATE interactions SET parent_id = ? WHERE id = ?", [(parent_id, i) for i in ids])

    def cache_lookup(self, task: str, after_id: int = 0) -> sqlite3.Row | None:
        """同一タスクの最新の実回答。それが悪評価なら使わない。after_id 以前の記録の回答も使わない
        (AIへの指示や覚えていることを変えたあとは、古い指示で作った回答を返さないため)。"""
        rows = self._query(
            "SELECT * FROM interactions WHERE task_hash = ? AND source IN ('llm', 'swarm') AND parent_id IS NULL AND id > ?"
            " ORDER BY id DESC LIMIT 1",
            (task_hash(task), after_id),
        )
        if rows and rows[0]["feedback"] != -1:
            return rows[0]
        return None

    def model_stats(self, role: str) -> dict[str, tuple[int, float]]:
        """役割ごとの {モデル名: (試行回数, 平均報酬)}。"""
        rows = self._query(
            "SELECT model, COUNT(*) AS n, AVG(reward) AS r FROM interactions"
            " WHERE role = ? AND source IN ('llm', 'chat', 'rejected') GROUP BY model",
            (role,),
        )
        return {r["model"]: (r["n"], r["r"]) for r in rows}

    def routing_examples(self) -> list[sqlite3.Row]:
        """ルーター学習用。管理AIまたはユーザーが決めた振り分けだけを教師にする(自己強化による劣化を防ぐ)。"""
        return self._query(
            "SELECT task, role FROM interactions WHERE route_source IN ('manager', 'forced', 'plan')"
            " AND source IN ('llm', 'swarm')"
        )

    def good_examples(self) -> list[sqlite3.Row]:
        """手本に使える回答(良評価または検証合格)。"""
        return self._query(
            "SELECT id, task, role, answer FROM interactions"
            " WHERE source = 'llm' AND (feedback = 1 OR (verified = 1 AND feedback IS NULL))"
        )

    def top_level(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM interactions WHERE parent_id IS NULL AND source != 'rejected' ORDER BY id")

    def role_model_table(self) -> list[sqlite3.Row]:
        return self._query(
            "SELECT role, model, COUNT(*) AS n, AVG(reward) AS reward, AVG(gen_latency_s) AS latency,"
            " AVG(prompt_tokens + completion_tokens) AS tokens FROM interactions"
            " WHERE source IN ('llm', 'chat', 'rejected') GROUP BY role, model ORDER BY role, reward DESC"
        )

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM interactions WHERE parent_id IS NULL AND source != 'rejected' ORDER BY id DESC LIMIT ?", (limit,))

    def savings(self) -> tuple[int, float]:
        """キャッシュで節約できた (トークン, 秒)。"""
        row = self._query(
            "SELECT COALESCE(SUM(o.prompt_tokens + o.completion_tokens + o.overhead_tokens), 0) AS t,"
            " COALESCE(SUM(o.latency_s), 0) AS s FROM interactions c JOIN interactions o ON c.origin_id = o.id"
            " WHERE c.source = 'cache'")[0]
        return int(row["t"]), float(row["s"])

    def get_embeddings(self, hashes: list[str], model: str) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(hashes))
        for i in range(0, len(unique), 500):
            chunk = unique[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._query(f"SELECT task_hash, vec FROM embeddings WHERE model = ? AND task_hash IN ({marks})", (model, *chunk)):
                values = array("f")
                values.frombytes(row["vec"])
                found[row["task_hash"]] = values.tolist()
        return found

    def put_embeddings(self, items: list[tuple[str, list[float]]], model: str) -> None:
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO embeddings (task_hash, model, vec) VALUES (?, ?, ?)",
                                 [(h, model, array("f", v).tobytes()) for h, v in items])

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self._query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    def add_study(self, *, task: str, role: str, model: str, answer: str, verified: bool = False) -> bool:
        """自習の成果を1件保存する。同じ問題が既にあれば False。"""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO study (ts, task, task_hash, role, model, answer, verified) VALUES (?,?,?,?,?,?,?)",
                (time.time(), task, task_hash(task), role, model, answer, int(bool(verified))))
            return cur.rowcount == 1

    def delete_study(self, task: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM study WHERE task_hash = ?", (task_hash(task),))

    def update_study_answer(self, task: str, answer: str, *, verified: bool | None = None) -> None:
        with self._lock, self._db:
            if verified is None:
                self._db.execute("UPDATE study SET answer = ? WHERE task_hash = ?", (answer, task_hash(task)))
            else:
                self._db.execute("UPDATE study SET answer = ?, verified = ? WHERE task_hash = ?",
                                 (answer, int(bool(verified)), task_hash(task)))

    def study_rows(self) -> list[sqlite3.Row]:
        return self._query("SELECT task, role, model, answer, verified FROM study ORDER BY id")

    def study_counts_by_role(self) -> dict[str, int]:
        rows = self._query("SELECT role, COUNT(*) AS n FROM study GROUP BY role")
        return {r["role"]: int(r["n"]) for r in rows}

    def add_preference(self, *, task: str, role: str, chosen: str, rejected: str, source: str = "study") -> bool:
        """良否の比較ペア(DPO/ORPO学習用)を保存する。"""
        chosen_s, rejected_s = chosen.strip(), rejected.strip()
        if not chosen_s or not rejected_s or chosen_s == rejected_s:
            return False
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO preferences (ts, task, task_hash, role, chosen, rejected, source)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), task, task_hash(task), role, chosen_s, rejected_s, source),
            )
            return cur.rowcount == 1

    def preference_rows(self, role: str | None = None) -> list[sqlite3.Row]:
        if role is not None:
            return self._query("SELECT task, role, chosen, rejected, source FROM preferences WHERE role = ? ORDER BY id", (role,))
        return self._query("SELECT task, role, chosen, rejected, source FROM preferences ORDER BY id")

    def interaction_preferences(self) -> list[dict]:
        """悪評価・却下された回答と、同じ依頼の良回答・自習の手本をペアにして返す。"""
        bads = self._query(
            "SELECT task, task_hash, role, answer FROM interactions"
            " WHERE (feedback = -1 OR verified = -1 OR source = 'rejected') AND answer != ''"
            " ORDER BY id DESC"
        )
        if not bads:
            return []
        good_map: dict[str, tuple[str, str]] = {}
        for r in self._query("SELECT task_hash, role, answer FROM study"):
            good_map[r["task_hash"]] = (r["role"], r["answer"])
        for r in self._query(
            "SELECT task_hash, role, answer FROM interactions"
            " WHERE source = 'llm' AND (feedback = 1 OR (verified = 1 AND feedback IS NULL)) ORDER BY id"
        ):
            good_map[r["task_hash"]] = (r["role"], r["answer"])
        pairs, seen = [], set()
        for b in bads:
            good = good_map.get(b["task_hash"])
            if not good or good[1].strip() == b["answer"].strip():
                continue
            key = (b["task_hash"], good[1], b["answer"])
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"task": b["task"], "role": good[0] or b["role"], "chosen": good[1], "rejected": b["answer"], "source": "feedback"})
        return pairs

    def bench_priors(self, suites: set[str] | None = None) -> dict[str, dict[str, tuple[float, float, int]]]:
        """{モデル名: {役割: (正解率, 平均秒, 課題数)}}。モデルを固定した計測のうち、それぞれ最新のものを使う。

        課題集が変わると難しさが変わり、正解率を比べられない(易しい古い課題集で満点のモデルが、
        難しい新しい課題集で測ったモデルより良く見えてしまう)。suites(今の課題集の識別子)を渡すと、
        それで測った結果があればそれだけを使う。
        """
        rows = self._query("SELECT label, suite, by_role, avg_latency_s FROM bench_runs WHERE label NOT LIKE 'agent:%' ORDER BY id")
        if suites and any(r["suite"] in suites for r in rows):
            rows = [r for r in rows if r["suite"] in suites]
        priors: dict[str, dict[str, tuple[float, float, int]]] = {}
        for row in rows:
            table = {}
            for role, cell in json.loads(row["by_role"]).items():
                correct, count = cell[0], cell[1]
                latency = cell[2] / count if len(cell) > 2 and count else row["avg_latency_s"]
                if count:
                    table[role] = (correct / count, latency, count)
            priors[row["label"]] = table
        return priors

    def log_bench(self, *, label: str, suite: str, n: int, correct: int, route_n: int, route_correct: int,
                  avg_tokens: float, avg_latency_s: float, by_role: str, items: list[dict] | None = None) -> int:
        """計測1回分を記録する。items は問題ごとの結果 [{task_id, role, model, ok, confidence, latency_s, escalated}]。"""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO bench_runs (ts, label, suite, n, correct, route_n, route_correct, avg_tokens,"
                " avg_latency_s, by_role) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), label, suite, n, correct, route_n, route_correct, avg_tokens, avg_latency_s, by_role),
            )
            run_id = int(cur.lastrowid)
            self._db.executemany(
                "INSERT OR REPLACE INTO bench_items (run_id, task_id, role, model, ok, confidence, latency_s, escalated)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, i["task_id"], i.get("role"), i["model"], int(bool(i["ok"])), i.get("confidence"),
                  float(i.get("latency_s") or 0.0), int(bool(i.get("escalated")))) for i in items or []])
            return run_id

    def agent_priors(self, min_tasks: int = 4, recent: int = 3) -> dict[str, tuple[float, float, int]]:
        """エージェントの計測 {モデル名: (成功率, 1課題の平均秒, 課題数)}。

        モデルごとに、最新の課題集での直近 recent 回を合わせたもの(課題の少ない試しの計測は除く)。小さいモデルは
        1回ごとのぶれが大きく(同じ版で 4/9 〜 9/9 があった)、たまたまよかった1回だけで選び直さないため。
        """
        rows = self._query("SELECT label, suite, n, correct, avg_latency_s FROM bench_runs WHERE label LIKE 'agent:%'"
                           " AND n >= ? ORDER BY id DESC", (min_tasks,))
        runs: dict[str, list[sqlite3.Row]] = {}
        for r in rows:  # 新しい順
            mine = runs.setdefault(r["label"][len("agent:"):], [])
            if len(mine) < recent and (not mine or r["suite"] == mine[0]["suite"]):  # 前の課題集の結果は混ぜない
                mine.append(r)
        priors = {}
        for name, mine in runs.items():
            n = sum(r["n"] for r in mine)
            priors[name] = (sum(r["correct"] for r in mine) / n, sum(r["avg_latency_s"] * r["n"] for r in mine) / n, n)
        return priors

    def bench_version(self) -> int:
        """計測の記録が増えるたびに変わる値(学んだ境目の作り直しの合図)。"""
        return int(self._query("SELECT COALESCE(MAX(id), 0) AS v FROM bench_runs")[0]["v"])

    def bench_items(self, label: str) -> dict[str, dict]:
        """その名前(モデル名)で最後に行った計測の、問題ごとの結果 {問題ID: {ok, confidence, latency_s, role}}。"""
        rows = self._query(
            "SELECT task_id, role, ok, confidence, latency_s FROM bench_items WHERE run_id ="
            " (SELECT MAX(r.id) FROM bench_runs r WHERE r.label = ? AND EXISTS (SELECT 1 FROM bench_items i WHERE i.run_id = r.id))",
            (label,))
        return {r["task_id"]: {"ok": bool(r["ok"]), "confidence": r["confidence"], "latency_s": r["latency_s"], "role": r["role"]}
                for r in rows}

    def bench_history(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM bench_runs ORDER BY id")

    def export_rows(self) -> list[sqlite3.Row]:
        return self._query(
            "SELECT task, role, model, answer, feedback, verified FROM interactions"
            " WHERE source = 'llm' AND (feedback IS NULL OR feedback = 1) AND verified != -1 ORDER BY id"
        )

    # ---- ナレッジベース(RAG) ------------------------------------------------

    def kb_upsert_doc(self, *, source: str, title: str, kind: str, tags: str, content_hash: str,
                      chars: int, chunks: list[str]) -> tuple[int, bool]:
        """文書とチャンクを登録または更新する。同じ content_hash なら更新をスキップし (doc_id, False) を返す。"""
        now = time.time()
        with self._lock, self._db:
            existing = self._db.execute("SELECT id, content_hash FROM kb_docs WHERE source = ?", (source,)).fetchone()
            if existing and existing["content_hash"] == content_hash and existing["id"]:
                self._db.execute("UPDATE kb_docs SET title = ?, tags = ?, updated = ? WHERE id = ?",
                                 (title, tags, now, existing["id"]))
                return int(existing["id"]), False
            if existing:
                doc_id = int(existing["id"])
                self._db.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
                self._db.execute(
                    "UPDATE kb_docs SET title=?, kind=?, tags=?, content_hash=?, chars=?, chunks=?, updated=? WHERE id=?",
                    (title, kind, tags, content_hash, chars, len(chunks), now, doc_id),
                )
            else:
                cur = self._db.execute(
                    "INSERT INTO kb_docs (source, title, kind, tags, content_hash, chars, chunks, updated)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (source, title, kind, tags, content_hash, chars, len(chunks), now),
                )
                doc_id = int(cur.lastrowid)
            self._db.executemany(
                "INSERT INTO kb_chunks (doc_id, idx, text, text_hash) VALUES (?,?,?,?)",
                [(doc_id, idx, txt, task_hash(txt)) for idx, txt in enumerate(chunks)],
            )
            return doc_id, True

    def kb_docs(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM kb_docs ORDER BY updated DESC, id DESC")

    def kb_get_doc(self, doc_id_or_source: int | str) -> sqlite3.Row | None:
        if isinstance(doc_id_or_source, int) or str(doc_id_or_source).isdigit():
            rows = self._query("SELECT * FROM kb_docs WHERE id = ?", (int(doc_id_or_source),))
        else:
            rows = self._query("SELECT * FROM kb_docs WHERE source = ?", (str(doc_id_or_source),))
        return rows[0] if rows else None

    def kb_delete_doc(self, doc_id_or_source: int | str) -> bool:
        doc = self.kb_get_doc(doc_id_or_source)
        if doc is None:
            return False
        with self._lock, self._db:
            self._db.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc["id"],))
            deleted = self._db.execute("DELETE FROM kb_docs WHERE id = ?", (doc["id"],)).rowcount
            return bool(deleted)

    def kb_chunks(self) -> list[sqlite3.Row]:
        return self._query(
            "SELECT c.id, c.doc_id, c.idx, c.text, c.text_hash, d.source, d.title, d.kind, d.tags"
            " FROM kb_chunks c JOIN kb_docs d ON c.doc_id = d.id ORDER BY c.id"
        )

    def kb_stats(self) -> dict[str, int]:
        row = self._query("SELECT COUNT(*) AS docs, COALESCE(SUM(chunks), 0) AS chunks, COALESCE(SUM(chars), 0) AS chars FROM kb_docs")[0]
        emb = self._query("SELECT COUNT(DISTINCT c.id) AS n FROM kb_chunks c JOIN embeddings e ON c.text_hash = e.task_hash")[0]
        return {"docs": int(row["docs"]), "chunks": int(row["chunks"]), "chars": int(row["chars"]), "embedded": int(emb["n"])}
