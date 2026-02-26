from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    wechat_token: str = os.getenv("WECHAT_TOKEN", "")
    wechat_app_id: str = os.getenv("WECHAT_APP_ID", "")
    wechat_app_secret: str = os.getenv("WECHAT_APP_SECRET", "")
    wechat_encoding_aes_key: str = os.getenv("WECHAT_ENCODING_AES_KEY", "").strip()
    wechat_reply_timeout_sec: float = float(os.getenv("WECHAT_REPLY_TIMEOUT_SEC", "4.0"))
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_chat_model: str = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:14b")
    ollama_embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "qwen3:14b")
    ollama_vision_model: str = os.getenv("OLLAMA_VISION_MODEL", "").strip()
    kb_db_path: str = os.getenv("KB_DB_PATH", "./data/kb.sqlite3")
    kb_source_dir: str = os.getenv("KB_SOURCE_DIR", "./kb_source")
    kb_auto_sync_on_start: bool = _as_bool(os.getenv("KB_AUTO_SYNC_ON_START", "1"), default=True)
    kb_sync_interval_sec: int = int(os.getenv("KB_SYNC_INTERVAL_SEC", "0"))
    max_chunk_chars: int = int(os.getenv("MAX_CHUNK_CHARS", "500"))
    top_k: int = int(os.getenv("TOP_K", "4"))


settings = Settings()
