"""Local RFC 6238 TOTP, restricted to the supported SHA-1 / 6 / 30 profile."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse


def normalize_totp_secret(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("2FA 密钥必须是字符串")
    secret = value.strip()
    if secret.lower().startswith("otpauth://"):
        uri = urlparse(secret)
        if uri.scheme.lower() != "otpauth" or uri.netloc.lower() != "totp":
            raise ValueError("仅支持 TOTP 类型的 otpauth URI")
        query = parse_qs(uri.query, keep_blank_values=True)
        for key in ("secret", "algorithm", "digits", "period"):
            if len(query.get(key, [])) > 1:
                raise ValueError("otpauth URI 包含重复参数")
        algorithm = query.get("algorithm", ["SHA1"])[0].upper()
        digits = query.get("digits", ["6"])[0]
        period = query.get("period", ["30"])[0]
        if (algorithm, digits, period) != ("SHA1", "6", "30"):
            raise ValueError("仅支持 SHA1、6 位、30 秒的 TOTP")
        secret = query.get("secret", [""])[0]
    normalized = "".join(secret.replace("-", "").split()).upper().rstrip("=")
    if not normalized:
        raise ValueError("2FA 密钥为空")
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("2FA 密钥不是有效的 Base32") from None
    if not key:
        raise ValueError("2FA 密钥为空")
    return normalized


def generate_totp(secret: str, timestamp: float | None = None) -> str:
    normalized = normalize_totp_secret(secret)
    key = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    current = time.time() if timestamp is None else timestamp
    digest = hmac.new(key, struct.pack(">Q", int(current // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def totp_seconds_remaining(timestamp: float | None = None) -> int:
    current = time.time() if timestamp is None else timestamp
    return 30 - int(current % 30)
