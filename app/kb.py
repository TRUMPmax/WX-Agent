from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
from pathlib import Path
from typing import Iterable

from docx import Document
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
    def __init__(self, db_path: str, max_chunk_chars: int = 500) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_chunk_chars = max_chunk_chars
        self._init_db()

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

        rows: list[tuple[str, str, str]] = []
        for chunk in chunks:
            emb_json = ""
            try:
                emb = ollama.embed(chunk)
                emb_json = json.dumps(emb, ensure_ascii=False)
            except Exception:
                emb_json = ""
            rows.append((source_name, chunk, emb_json))

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO kb_chunks(source_name, chunk_text, embedding_json) VALUES(?,?,?)",
                rows,
            )
            conn.commit()
        return len(rows)

    def remove_source(self, source_name: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM kb_chunks WHERE source_name = ?", (source_name,))
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

        for file_path in files:
            rel = file_path.relative_to(source_dir).as_posix()
            source_name = f"kbdir:{rel}"
            seen_sources.add(source_name)

            stat = file_path.stat()
            prev = index_map.get(source_name)
            if prev and prev["file_size"] == stat.st_size and prev["file_mtime"] == stat.st_mtime:
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
        q_emb: list[float] | None = None
        try:
            q_emb = ollama.embed(query)
        except Exception:
            q_emb = None

        scored: list[dict] = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT source_name, chunk_text, embedding_json FROM kb_chunks").fetchall()
        for source_name, chunk_text, emb_json in rows:
            score = 0.0
            if q_emb is not None and emb_json:
                emb = json.loads(emb_json)
                score = self._cosine_similarity(q_emb, emb)
            else:
                score = self._keyword_score(query, chunk_text)
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
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(cleaned):
            end = start + self.max_chunk_chars
            chunks.append(cleaned[start:end])
            start = end
        return chunks

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
