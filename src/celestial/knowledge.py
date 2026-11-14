"""ローカル ナレッジベース(RAG): テキスト・Markdown・コード・PDF・Office 文書・URL を索引化し、
意味ベクトル(bge-m3 等)＋語彙類似度のハイブリッド検索で関連箇所を取り出す。"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
from pathlib import Path
from typing import Callable

from . import office, web
from .textsim import cosine, normalize, vectorize

_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache"}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".pyw", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".sql", ".sh", ".ps1", ".bat",
    ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".xml", ".log",
}
_DOC_EXTS = set(office.READABLE_DOC_SUFFIXES)
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}|[ぁ-んァ-ヶ一-龠ー]{2,}")


def chunk_text(text: str, *, max_chars: int = 550, overlap: int = 80) -> list[str]:
    """見出し・改行のまとまりをなるべく保ちながら、検索しやすい長さに分割する。"""
    cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks: list[str] = []
    pos = 0
    n = len(cleaned)
    while pos < n:
        end = min(n, pos + max_chars)
        if end < n:
            cut = cleaned.rfind("\n\n", pos + max_chars // 2, end)
            if cut == -1:
                cut = cleaned.rfind("\n", pos + max_chars // 2, end)
            if cut == -1:
                cut = cleaned.rfind("。", pos + max_chars // 2, end)
            if cut != -1 and cut > pos:
                end = cut + 1
        piece = cleaned[pos:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        pos = max(pos + 1, end - overlap)
    return chunks


def read_document_text(path: Path, limit: int = 200_000) -> str:
    """拡張子に応じてドキュメント(PDF / Office / テキスト・ソースコード)から文字を読む。"""
    suffix = path.suffix.lower()
    if suffix in _DOC_EXTS:
        return office.read_text(path, limit=limit)
    data = path.read_bytes()[:limit * 3]
    if b"\x00" in data[:4096]:
        raise ValueError(f"バイナリファイルは読み込めません: {path.name}")
    for enc in ("utf-8-sig", "utf-8", "cp932", "latin-1"):
        try:
            return data.decode(enc)[:limit]
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")[:limit]


def _lex_terms(text: str) -> set[str]:
    norm = normalize(text)
    terms = set(_TOKEN_RE.findall(norm))
    if not terms and norm:
        terms = {norm}
    return terms


class KnowledgeBase:
    """SQLite(Store) と Embedder を使うハイブリッド RAG。"""

    def __init__(self, store, embedder=None, *, semantic: Callable[[], bool] | bool = True):
        self.store = store
        self.embedder = embedder
        self._semantic = semantic if callable(semantic) else (lambda: bool(semantic))

    def _index(self, *, source: str, title: str, kind: str, content: str, tags: str = "") -> dict:
        text = (content or "").strip()
        if not text:
            raise ValueError("登録する内容が空です")
        chunks = chunk_text(text)
        c_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        doc_id, changed = self.store.kb_upsert_doc(
            source=source, title=title.strip() or source, kind=kind, tags=tags.strip(),
            content_hash=c_hash, chars=len(text), chunks=chunks,
        )
        if changed and self.embedder is not None and self._semantic():
            # インデックス時に非致命で事前埋め込み計算を行う
            try:
                self.embedder.vectors(chunks[:64])
            except Exception:
                pass
        return {"id": doc_id, "source": source, "title": title.strip() or source, "kind": kind,
                "chunks": len(chunks), "chars": len(text), "updated": changed}

    def add_note(self, title: str, content: str, *, tags: str = "") -> dict:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("メモのタイトルを指定してください")
        return self._index(source=f"note:{clean_title}", title=clean_title, kind="note", content=content, tags=tags)

    def add_file(self, path: str | Path, *, source_name: str | None = None, tags: str = "") -> dict:
        target = Path(path).resolve()
        if not target.is_file():
            raise ValueError(f"ファイルが見つかりません: {path}")
        if target.stat().st_size > 25_000_000:
            raise ValueError(f"ファイルが大きすぎます(上限 25MB): {target.name}")
        content = read_document_text(target)
        return self._index(
            source=source_name or str(target),
            title=target.name,
            kind="file",
            content=content,
            tags=tags,
        )

    def add_folder(self, folder: str | Path, *, pattern: str = "*", tags: str = "", limit: int = 80) -> dict:
        root = Path(folder).resolve()
        if not root.is_dir():
            raise ValueError(f"フォルダが見つかりません: {folder}")
        indexed, skipped = [], []
        allowed_exts = _TEXT_EXTS | _DOC_EXTS
        for p in sorted(root.rglob("*")):
            if len(indexed) >= limit:
                break
            if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if not p.is_file() or p.suffix.lower() not in allowed_exts:
                continue
            if not fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                continue
            if p.stat().st_size > 4_000_000:
                skipped.append(p.name)
                continue
            try:
                rel = p.relative_to(root).as_posix()
                indexed.append(self.add_file(p, source_name=str(p), tags=tags or rel))
            except (ValueError, OSError):
                skipped.append(p.name)
        return {"indexed": len(indexed), "skipped": len(skipped), "docs": indexed[:20]}

    def add_url(self, url: str, *, title: str = "", tags: str = "") -> dict:
        content = web.fetch_url(url)
        first_line = content.splitlines()[0].lstrip("# ").strip() if content.splitlines() else url
        return self._index(source=url.strip(), title=title.strip() or first_line[:80], kind="url", content=content, tags=tags)

    def list_docs(self) -> list[dict]:
        return [dict(r) for r in self.store.kb_docs()]

    def stats(self) -> dict[str, int]:
        return self.store.kb_stats()

    def remove(self, doc_id_or_source: int | str) -> bool:
        return self.store.kb_delete_doc(doc_id_or_source)

    def search(self, query: str, *, top_k: int = 5, min_score: float = 0.18) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        rows = self.store.kb_chunks()
        if not rows:
            return []
        q_vec = vectorize(q)
        q_terms = _lex_terms(q)
        q_norm = normalize(q)

        # 語彙スコア(文字 n-gram + 語句一致 + タイトル/タグ一致)
        lex_scores: list[tuple[int, float]] = []
        for idx, r in enumerate(rows):
            title_tags = f"{r['title']} {r['tags']}".strip()
            body_norm = normalize(r["text"])
            sim = cosine(q_vec, vectorize(f"{title_tags}\n{r['text']}"))
            hit_ratio = (sum(1 for t in q_terms if t in body_norm or t in normalize(title_tags)) / len(q_terms)) if q_terms else 0.0
            exact_bonus = 0.25 if (len(q_norm) >= 3 and q_norm in body_norm) else 0.0
            lex = min(1.0, 0.55 * sim + 0.35 * hit_ratio + exact_bonus)
            lex_scores.append((idx, lex))

        # 意味ベクトル(使える場合のみ上位候補 + 小規模コーパス全体へ適用)
        sem_map: dict[int, float] = {}
        if self.embedder is not None and self._semantic():
            if len(rows) <= 250:
                cand_indices = list(range(len(rows)))
            else:
                cand_indices = [i for i, _ in sorted(lex_scores, key=lambda x: x[1], reverse=True)[:120]]
            texts = [q] + [rows[i]["text"] for i in cand_indices]
            vecs = self.embedder.vectors(texts)
            if vecs is not None:
                qv, cvs = vecs[0], vecs[1:]
                dot = math.sumprod if hasattr(math, "sumprod") else (lambda a, b: sum(x * y for x, y in zip(a, b)))
                for ci, cv in zip(cand_indices, cvs):
                    sem_map[ci] = max(0.0, dot(qv, cv))

        scored: list[tuple[float, dict]] = []
        for idx, lex in lex_scores:
            if idx in sem_map:
                score = 0.62 * sem_map[idx] + 0.38 * lex
            else:
                score = lex
            if score >= min_score:
                r = rows[idx]
                scored.append((score, {
                    "doc_id": r["doc_id"], "source": r["source"], "title": r["title"],
                    "kind": r["kind"], "tags": r["tags"], "idx": r["idx"],
                    "text": r["text"], "score": round(score, 3),
                }))
        scored.sort(key=lambda x: x[0], reverse=True)

        # 同じ文書ばかりが上位を埋めないよう 1 文書あたり最大 2 チャンクに抑える
        picked, per_doc = [], {}
        for _, item in scored:
            d = item["doc_id"]
            if per_doc.get(d, 0) >= 2:
                continue
            per_doc[d] = per_doc.get(d, 0) + 1
            picked.append(item)
            if len(picked) >= top_k:
                break
        return picked


def context_prompt(hits: list[dict]) -> str:
    """検索でヒットしたナレッジを回答時の参考資料として渡す文字列。"""
    if not hits:
        return ""
    blocks = "\n".join(f"- [{h['title']}] {h['text'][:550]}" for h in hits)
    return ("\n\n利用者の知識ベース(登録済みのローカル資料)から関連する箇所が見つかりました。"
            "質問に関係がある場合はこの資料に基づいて正確に答えてください:\n" + blocks)

