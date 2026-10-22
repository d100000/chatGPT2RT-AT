"""Export extracted OpenAI tokens as CLIProxyAPI (CPA) Codex auth records."""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import tempfile
from typing import Any

from .storage import check_output


def save_export(data: bytes, output: str | Path) -> Path:
    """Publish generated JSON/ZIP privately and atomically without overwriting."""
    path = check_output(output)
    fd, name = tempfile.mkstemp(prefix=".rt-extractor-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def build_cpa_record(payload: Mapping[str, Any]) -> dict:
    """Whitelist CPA auth fields without exporting input passwords or proxy settings.

    Tokens and metadata are copied from a saved extraction result. This is a file
    format conversion; it does not refresh tokens or verify JWT signatures.
    """
    if not isinstance(payload, Mapping) or any(
        not isinstance(payload.get(key), str) or not payload[key].strip()
        for key in ("email", "access_token", "refresh_token", "id_token")
    ):
        raise ValueError("成功结果缺少邮箱或完整 Token，无法生成 CPA JSON")

    record = {
        "type": "codex",
        "email": payload["email"].strip(),
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "id_token": payload["id_token"],
    }
    for source, target in (
        ("chatgpt_account_id", "account_id"),
        ("plan_type", "plan_type"),
        ("expires_at", "expired"),
        ("obtained_at", "last_refresh"),
    ):
        value = payload.get(source)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("成功结果的 CPA 元数据格式无效")
        if value.strip():
            record[target] = value.strip()
    return record
