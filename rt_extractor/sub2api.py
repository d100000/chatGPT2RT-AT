"""Build the Sub2API v1 import document from extracted token records."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any


def build_sub2api_export(payloads: Iterable[Mapping[str, Any]]) -> dict:
    """Export only the sample's fields; never copy arbitrary input properties."""
    accounts = []
    for payload in payloads:
        if not isinstance(payload, Mapping) or any(
            not isinstance(payload.get(key), str) or not payload[key].strip()
            for key in ("email", "access_token", "refresh_token", "id_token")
        ):
            raise ValueError("成功结果缺少邮箱或完整 Token，无法生成 Sub2API JSON")
        email = payload["email"].strip()
        account_id = payload.get("chatgpt_account_id") or ""
        user_id = payload.get("chatgpt_user_id") or ""
        if not isinstance(account_id, str) or not isinstance(user_id, str):
            raise ValueError("成功结果的账号标识格式无效")
        accounts.append({
            "name": f"oauth---{email}",
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "access_token": payload["access_token"],
                "chatgpt_account_id": account_id,
                "email": email,
                "id_token": payload["id_token"],
                "refresh_token": payload["refresh_token"],
            },
            "extra": {
                "account_id": account_id,
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "email": email,
                "openai_long_context_billing_enabled": False,
                "openai_oauth_responses_websockets_v2_enabled": False,
                "openai_oauth_responses_websockets_v2_mode": "off",
                # Match the reference metadata; this does not change privacy settings.
                "privacy_mode": "training_set_failed",
            },
            "concurrency": 10,
            "priority": 1,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
        })
    if not accounts:
        raise ValueError("没有可导出的成功账号")
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }
