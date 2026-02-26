from __future__ import annotations

from typing import Any

import requests


class OllamaClient:
    def __init__(self, base_url: str, chat_model: str, embed_model: str, vision_model: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.vision_model = vision_model

    def embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.embed_model, "prompt": text},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        vector = data.get("embedding", [])
        if not vector:
            raise RuntimeError("Ollama embeddings returned empty vector")
        return vector

    def chat(self, prompt: str, timeout_sec: float | None = None) -> str:
        payload = {
            "model": self.chat_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        timeout = timeout_sec if timeout_sec is not None else 120
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data.get("message", {}).get("content", "").strip()

    def image_to_text(self, image_path: str) -> str:
        if not self.vision_model:
            return ""
        payload = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": "请读取图片中的文字和关键信息，输出纯文本。",
                    "images": [image_path],
                }
            ],
            "stream": False,
        }
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data.get("message", {}).get("content", "").strip()
