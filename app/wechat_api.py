from __future__ import annotations

import threading
import time
from typing import Any

import requests


_token_lock = threading.Lock()
_token_value = ""
_token_expire_at = 0.0


def get_access_token(app_id: str, app_secret: str) -> dict[str, Any]:
    if not app_id or not app_secret:
        raise ValueError("WECHAT_APP_ID/WECHAT_APP_SECRET is not configured")
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_cached_access_token(app_id: str, app_secret: str, force_refresh: bool = False) -> str:
    global _token_value, _token_expire_at
    now = time.time()
    with _token_lock:
        if not force_refresh and _token_value and now < _token_expire_at:
            return _token_value
        data = get_access_token(app_id, app_secret)
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError(f"failed to get access_token: {data}")
        expires_in = int(data.get("expires_in", 7200))
        _token_value = token
        _token_expire_at = now + max(60, expires_in - 120)
        return _token_value


def send_custom_text_message(
    app_id: str,
    app_secret: str,
    openid: str,
    content: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    if not openid:
        raise ValueError("openid is required")
    if not content:
        return {"ok": False, "detail": "empty content"}

    token = access_token or get_cached_access_token(app_id, app_secret)
    url = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
    payload = {
        "touser": openid,
        "msgtype": "text",
        "text": {"content": content},
    }
    resp = requests.post(url, params={"access_token": token}, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode", 0) == 0:
        return {"ok": True, "detail": data, "access_token": token}

    # Retry once on token invalid/expired.
    if data.get("errcode") in {40001, 42001} and access_token is None:
        token = get_cached_access_token(app_id, app_secret, force_refresh=True)
        resp2 = requests.post(url, params={"access_token": token}, json=payload, timeout=30)
        resp2.raise_for_status()
        data2 = resp2.json()
        return {"ok": data2.get("errcode", 0) == 0, "detail": data2, "access_token": token}

    return {"ok": False, "detail": data, "access_token": token}
