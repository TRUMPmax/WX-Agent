from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from docx import Document
import jieba
from PIL import Image
from pypdf import PdfReader
import pytesseract

from app.ollama_client import OllamaClient


SUPPORTED_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".pdf",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
}


def is_supported_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


class KnowledgeBase:
    def __init__(
        self,
        db_path: str,
        max_chunk_chars: int = 500,
        chunk_overlap_chars: int = 80,
        hybrid_dense_weight: float = 0.65,
        hybrid_bm25_weight: float = 0.35,
        hybrid_rrf_k: int = 60,
        retrieval_candidates: int = 40,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_chunk_chars = max(100, max_chunk_chars)
        self.chunk_overlap_chars = max(0, min(chunk_overlap_chars, self.max_chunk_chars - 1))
        self.hybrid_dense_weight = max(0.0, hybrid_dense_weight)
        self.hybrid_bm25_weight = max(0.0, hybrid_bm25_weight)
        self.hybrid_rrf_k = max(1, hybrid_rrf_k)
        self.retrieval_candidates = max(4, retrieval_candidates)
        self.fts_enabled = True
        self._init_db()
        if self.fts_enabled:
            self._ensure_fts_index()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_source_name ON kb_chunks(source_name)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_file_index (
                    source_name TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    file_mtime REAL NOT NULL,
                    file_size INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts
                    USING fts5(source_name UNINDEXED, chunk_text UNINDEXED, seg_text)
                    """
                )
            except sqlite3.OperationalError:
                self.fts_enabled = False
            conn.commit()

    def _ensure_fts_index(self) -> None:
        if not self.fts_enabled:
            return
        with sqlite3.connect(self.db_path) as conn:
            chunk_count = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM kb_chunks_fts").fetchone()[0]
            if chunk_count == fts_count:
                return

            conn.execute("DELETE FROM kb_chunks_fts")
            rows = conn.execute("SELECT id, source_name, chunk_text FROM kb_chunks").fetchall()
            fts_rows = []
            for row_id, source_name, chunk_text in rows:
                fts_rows.append((row_id, source_name, chunk_text, self._segment_text(chunk_text)))
            conn.executemany(
                "INSERT INTO kb_chunks_fts(rowid, source_name, chunk_text, seg_text) VALUES(?,?,?,?)",
                fts_rows,
            )
            conn.commit()

    def add_document(
        self,
        source_name: str,
        text: str,
        ollama: OllamaClient,
        replace_existing: bool = False,
    ) -> int:
        chunks = list(self._split_text(text))
        if replace_existing:
            self.remove_source(source_name)
        if not chunks:
            return 0

        rows: list[tuple[str, str, str, str]] = []
        for chunk in chunks:
            emb_json = ""
            try:
                emb = ollama.embed(chunk)
                emb_json = json.dumps(emb, ensure_ascii=False)
            except Exception:
                emb_json = ""
            rows.append((source_name, chunk, emb_json, self._segment_text(chunk)))

        inserted = 0
        with sqlite3.connect(self.db_path) as conn:
            for src, chunk_text, emb_json, seg_text in rows:
                cur = conn.execute(
                    "INSERT INTO kb_chunks(source_name, chunk_text, embedding_json) VALUES(?,?,?)",
                    (src, chunk_text, emb_json),
                )
                row_id = cur.lastrowid
                if self.fts_enabled:
                    conn.execute(
                        "INSERT INTO kb_chunks_fts(rowid, source_name, chunk_text, seg_text) VALUES(?,?,?,?)",
                        (row_id, src, chunk_text, seg_text),
                    )
                inserted += 1
            conn.commit()
        return inserted

    def remove_source(self, source_name: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM kb_chunks WHERE source_name = ?", (source_name,))
            if self.fts_enabled:
                conn.execute("DELETE FROM kb_chunks_fts WHERE source_name = ?", (source_name,))
            conn.commit()
            return cur.rowcount

    def sync_directory(self, source_dir: Path, ollama: OllamaClient) -> dict:
        source_dir = source_dir.expanduser().resolve()
        if not source_dir.exists():
            return {
                "ok": False,
                "source_dir": str(source_dir),
                "detail": "source directory does not exist",
                "total_files": 0,
                "indexed_files": 0,
                "skipped_files": 0,
                "removed_files": 0,
                "failed_files": 0,
                "total_chunks": 0,
                "errors": [],
            }

        files = sorted([p for p in source_dir.rglob("*") if p.is_file() and is_supported_file(p)])
        index_map = self._get_index_map()
        seen_sources: set[str] = set()
        indexed_files = 0
        skipped_files = 0
        removed_files = 0
        failed_files = 0
        total_chunks = 0
        errors: list[dict] = []
        current_embed_model = (ollama.embed_model or "").strip()
        indexed_embed_model = self._get_meta("embed_model")
        embed_model_changed = bool(current_embed_model) and current_embed_model != indexed_embed_model

        for file_path in files:
            rel = file_path.relative_to(source_dir).as_posix()
            source_name = f"kbdir:{rel}"
            seen_sources.add(source_name)

            stat = file_path.stat()
            prev = index_map.get(source_name)
            unchanged = prev and prev["file_size"] == stat.st_size and prev["file_mtime"] == stat.st_mtime
            has_full_embeddings = self._source_has_full_embeddings(source_name)
            if unchanged and has_full_embeddings and not embed_model_changed:
                skipped_files += 1
                continue

            try:
                text = extract_text_from_file(file_path, ollama=ollama)
                if not text.strip():
                    raise ValueError("no text extracted from file")
                chunks = self.add_document(source_name, text, ollama=ollama, replace_existing=True)
                self._upsert_index(
                    source_name=source_name,
                    file_path=str(file_path),
                    file_mtime=stat.st_mtime,
                    file_size=stat.st_size,
                )
                indexed_files += 1
                total_chunks += chunks
            except Exception as exc:
                failed_files += 1
                if len(errors) < 30:
                    errors.append({"file": str(file_path), "error": str(exc)})

        for source_name in index_map.keys():
            if source_name in seen_sources:
                continue
            self.remove_source(source_name)
            self._delete_index(source_name)
            removed_files += 1

        if current_embed_model:
            self._set_meta("embed_model", current_embed_model)

        return {
            "ok": True,
            "source_dir": str(source_dir),
            "total_files": len(files),
            "indexed_files": indexed_files,
            "skipped_files": skipped_files,
            "removed_files": removed_files,
            "failed_files": failed_files,
            "total_chunks": total_chunks,
            "errors": errors,
        }

    def list_sources(self, limit: int = 500) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT source_name, COUNT(*) AS chunks
                FROM kb_chunks
                GROUP BY source_name
                ORDER BY source_name
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{"source_name": row[0], "chunks": row[1]} for row in rows]

    def search(self, query: str, top_k: int, ollama: OllamaClient) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []

        candidate_limit = max(top_k, self.retrieval_candidates)
        dense_hits = self._dense_search(query, candidate_limit, ollama)
        bm25_hits = self._bm25_search(query, candidate_limit)
        hybrid_hits = self._hybrid_merge(dense_hits, bm25_hits, top_k)
        if hybrid_hits:
            return hybrid_hits

        return self._keyword_fallback_search(query, top_k)

    def _dense_search(self, query: str, top_k: int, ollama: OllamaClient) -> list[dict]:
        try:
            q_emb = ollama.embed(query)
        except Exception:
            return []

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT source_name, chunk_text, embedding_json FROM kb_chunks WHERE embedding_json <> ''"
            ).fetchall()

        scored: list[dict] = []
        for source_name, chunk_text, emb_json in rows:
            try:
                emb = json.loads(emb_json)
            except Exception:
                continue
            score = self._cosine_similarity(q_emb, emb)
            if score <= 0:
                continue
            scored.append({"source_name": source_name, "chunk_text": chunk_text, "dense_score": score})

        scored.sort(key=lambda x: x["dense_score"], reverse=True)
        return scored[:top_k]

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        if not self.fts_enabled:
            return []

        tokens = self._tokenize_text(query)
        if not tokens:
            return []
        match_query = " OR ".join(tokens[:32])
        if not match_query:
            return []

        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT source_name, chunk_text, bm25(kb_chunks_fts) AS bm25_score
                    FROM kb_chunks_fts
                    WHERE kb_chunks_fts MATCH ?
                    ORDER BY bm25_score
                    LIMIT ?
                    """,
                    (match_query, top_k),
                ).fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            {"source_name": source_name, "chunk_text": chunk_text, "bm25_score": bm25_score}
            for source_name, chunk_text, bm25_score in rows
        ]

    def _hybrid_merge(self, dense_hits: list[dict], bm25_hits: list[dict], top_k: int) -> list[dict]:
        if not dense_hits and not bm25_hits:
            return []

        dense_weight = self.hybrid_dense_weight
        bm25_weight = self.hybrid_bm25_weight
        weight_sum = dense_weight + bm25_weight
        if weight_sum <= 0:
            dense_weight, bm25_weight = 0.65, 0.35
            weight_sum = 1.0
        dense_weight /= weight_sum
        bm25_weight /= weight_sum

        merged: dict[tuple[str, str], dict] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            key = (hit["source_name"], hit["chunk_text"])
            row = merged.get(key)
            if row is None:
                row = {"source_name": key[0], "chunk_text": key[1], "score": 0.0}
                merged[key] = row
            row["dense_score"] = hit.get("dense_score", 0.0)
            row["score"] += dense_weight * (1.0 / (self.hybrid_rrf_k + rank))

        for rank, hit in enumerate(bm25_hits, start=1):
            key = (hit["source_name"], hit["chunk_text"])
            row = merged.get(key)
            if row is None:
                row = {"source_name": key[0], "chunk_text": key[1], "score": 0.0}
                merged[key] = row
            row["bm25_score"] = hit.get("bm25_score", 0.0)
            row["score"] += bm25_weight * (1.0 / (self.hybrid_rrf_k + rank))

        ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
        return ranked[:top_k]

    def _keyword_fallback_search(self, query: str, top_k: int) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT source_name, chunk_text FROM kb_chunks LIMIT 5000").fetchall()
        scored = []
        for source_name, chunk_text in rows:
            score = self._keyword_score(query, chunk_text)
            if score <= 0:
                continue
            scored.append({"source_name": source_name, "chunk_text": chunk_text, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def _get_index_map(self) -> dict[str, dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT source_name, file_path, file_mtime, file_size FROM kb_file_index"
            ).fetchall()
        result: dict[str, dict] = {}
        for source_name, file_path, file_mtime, file_size in rows:
            result[source_name] = {
                "file_path": file_path,
                "file_mtime": file_mtime,
                "file_size": file_size,
            }
        return result

    def _source_has_full_embeddings(self, source_name: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN embedding_json <> '' THEN 1 ELSE 0 END) AS embedding_count
                FROM kb_chunks
                WHERE source_name = ?
                """,
                (source_name,),
            ).fetchone()
        total_count = int(row[0] or 0)
        embedding_count = int(row[1] or 0)
        return total_count > 0 and total_count == embedding_count

    def _get_meta(self, key: str) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM kb_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO kb_meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def _upsert_index(self, source_name: str, file_path: str, file_mtime: float, file_size: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO kb_file_index(source_name, file_path, file_mtime, file_size, indexed_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(source_name) DO UPDATE SET
                    file_path=excluded.file_path,
                    file_mtime=excluded.file_mtime,
                    file_size=excluded.file_size,
                    indexed_at=excluded.indexed_at
                """,
                (source_name, file_path, file_mtime, file_size, now),
            )
            conn.commit()

    def _delete_index(self, source_name: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM kb_file_index WHERE source_name = ?", (source_name,))
            conn.commit()

    def _split_text(self, text: str) -> Iterable[str]:
        cleaned = re.sub(r"\r\n?", "\n", (text or "")).strip()
        if not cleaned:
            return []
        if len(cleaned) <= self.max_chunk_chars:
            return [cleaned]

        chunks: list[str] = []
        total_len = len(cleaned)
        start = 0

        while start < total_len:
            hard_end = min(start + self.max_chunk_chars, total_len)
            end = hard_end
            if hard_end < total_len:
                window = cleaned[start:hard_end]
                boundary_positions = [
                    window.rfind("\n"),
                    window.rfind("。"),
                    window.rfind("！"),
                    window.rfind("？"),
                    window.rfind("；"),
                    window.rfind("."),
                    window.rfind("!"),
                    window.rfind("?"),
                    window.rfind(";"),
                ]
                boundary = max(boundary_positions)
                if boundary >= max(0, len(window) // 2):
                    end = start + boundary + 1

            if end <= start:
                end = hard_end
            chunk = cleaned[start:end].strip()
            if chunk:
                chunks.append(chunk)

            if end >= total_len:
                break
            next_start = max(0, end - self.chunk_overlap_chars)
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    @staticmethod
    def _tokenize_text(text: str) -> list[str]:
        if not text.strip():
            return []
        raw_tokens = [t.strip().lower() for t in jieba.cut_for_search(text) if t and t.strip()]
        tokens: list[str] = []
        seen: set[str] = set()
        for token in raw_tokens:
            normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", token)
            if not normalized:
                continue
            if normalized.isascii() and len(normalized) < 2:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            tokens.append(normalized)
        return tokens

    def _segment_text(self, text: str) -> str:
        return " ".join(self._tokenize_text(text))

    @staticmethod
    def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1))
        n2 = math.sqrt(sum(b * b for b in v2))
        if n1 == 0 or n2 == 0:
            return 0.0
        return dot / (n1 * n2)

    @staticmethod
    def _keyword_score(query: str, chunk: str) -> float:
        q = (query or "").lower().strip()
        c = (chunk or "").lower()
        if not q or not c:
            return 0.0
        if " " not in q:
            chars = set(q)
            if not chars:
                return 0.0
            return sum(1 for ch in chars if ch in c) / len(chars)
        words = [w for w in q.split() if w]
        if words:
            hit = sum(1 for w in words if w in c)
            return hit / len(words)
        chars = set(q)
        if not chars:
            return 0.0
        return sum(1 for ch in chars if ch in c) / len(chars)


def extract_text_from_file(file_path: Path, ollama: OllamaClient) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return file_path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        reader = PdfReader(str(file_path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if suffix == ".docx":
        doc = Document(str(file_path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        text = _ocr_with_tesseract(file_path)
        if text.strip():
            return text
        return ollama.image_to_text(str(file_path))
    raise ValueError(f"Unsupported file type: {suffix}")


def _ocr_with_tesseract(file_path: Path) -> str:
    try:
        img = Image.open(file_path)
        return pytesseract.image_to_string(img, lang="chi_sim+eng")
    except Exception:
        return ""
