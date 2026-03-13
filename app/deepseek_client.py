from __future__ import annotations

import json
from typing import Any, Iterator

import requests


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        chat_model: str,
        timeout_sec: float = 90,
    ) -> None:
        self.api_key = (api_key or "").strip()
        base = (base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        self.base_url = base
        self.chat_model = (chat_model or "deepseek-chat").strip()
        self.timeout_sec = max(10.0, float(timeout_sec or 90))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, prompt: str, stream: bool) -> dict[str, Any]:
        system_prompt = (
            "你是企业客服助手。"
            "只输出给用户的最终答复，不要输出分析过程、思考过程、草稿或推理。"
            "禁止使用‘好的，用户问的是’这类元叙述。"
        )
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 900,
            "stream": stream,
        }
        return payload

    def chat(self, prompt: str, timeout_sec: float | None = None) -> str:
        if not self.available:
            raise RuntimeError("DeepSeek API key is not configured")
        timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._build_payload(prompt, stream=False),
            timeout=timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        return str(message.get("content", "")).strip()

    def chat_stream(self, prompt: str, timeout_sec: float | None = None) -> Iterator[str]:
        if not self.available:
            raise RuntimeError("DeepSeek API key is not configured")
        timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        with requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._build_payload(prompt, stream=True),
            timeout=timeout,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                raw = line.strip()
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    data: dict[str, Any] = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices", [])
                if not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta", {})
                piece = str(delta.get("content", ""))
                if piece:
                    yield piece
