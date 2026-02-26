from __future__ import annotations

from typing import Any

import requests


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
