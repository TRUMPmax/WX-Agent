from __future__ import annotations

import json
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
        system_prompt = (
            "你是企业客服助手。"
            "只输出给用户的最终答复，不要输出分析过程、思考过程、草稿或推理。"
            "禁止使用“好的，用户问的是”这类元叙述。"
        )
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 160,
            },
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

    def chat_stream(self, prompt: str, timeout_sec: float | None = None):
        system_prompt = (
            "你是企业客服助手。"
            "只输出给用户的最终答复，不要输出分析过程、思考过程、草稿或推理。"
            "禁止使用“好的，用户问的是”这类元叙述。"
        )
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "think": False,
            "options": {
                "temperature": 0.2,
                # qwen3 may emit a long <think> section before the final answer.
                # Keep this high enough so we can still receive the user-facing reply.
                "num_predict": 800,
            },
        }
        timeout = timeout_sec if timeout_sec is not None else 120
        with requests.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = data.get("message", {})
                piece = message.get("content", "")
                if piece:
                    yield piece
                if data.get("done"):
                    break
