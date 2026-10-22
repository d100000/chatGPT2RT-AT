"""Signed, expiring anonymous browser identities without a server-side registry."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets

from itsdangerous import BadData, URLSafeTimedSerializer


DEVICE_COOKIE = "bob_ai_device"
SESSION_COOKIE = "bob_ai_session"
DEVICE_MAX_AGE = 30 * 86400
SESSION_MAX_AGE = 12 * 3600

_COOKIE_MAX_LENGTH = 256
_IDENTIFIER = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class IdentityCookie:
    name: str
    value: str
    max_age: int | None


@dataclass(frozen=True)
class BrowserIdentity:
    device: str | None
    session: str | None
    cookies: tuple[IdentityCookie, ...]


class BrowserIdentities:
    """Validate cookies, optionally issuing only missing or invalid identities.

    The signing key lives only in this instance. Restarting the process makes
    earlier cookies invalid; no identity, request body, or user data is retained.
    Cookie security attributes are applied by the HTTP response layer.
    """

    def __init__(self):
        signing_key = secrets.token_bytes(32)
        signer_kwargs = {"digest_method": hashlib.sha256}
        self._device_serializer = URLSafeTimedSerializer(
            signing_key, salt="bob-ai-browser-device-v1", signer_kwargs=signer_kwargs,
        )
        self._session_serializer = URLSafeTimedSerializer(
            signing_key, salt="bob-ai-browser-session-v1", signer_kwargs=signer_kwargs,
        )

    @staticmethod
    def _read(serializer, cookie, max_age: int, fields: set[str]) -> dict[str, str] | None:
        if type(cookie) is not str or not cookie or len(cookie) > _COOKIE_MAX_LENGTH:
            return None
        try:
            payload = serializer.loads(cookie, max_age=max_age)
        except (BadData, UnicodeError):
            return None
        if type(payload) is not dict or payload.keys() != fields:
            return None
        if any(type(value) is not str or _IDENTIFIER.fullmatch(value) is None
               for value in payload.values()):
            return None
        return payload

    def resolve(self, cookies: Mapping[str, object], *, issue: bool = False) -> BrowserIdentity:
        device_payload = self._read(
            self._device_serializer, cookies.get(DEVICE_COOKIE), DEVICE_MAX_AGE, {"id"},
        )
        device = device_payload["id"] if device_payload is not None else None
        session = None
        if device is not None:
            session_payload = self._read(
                self._session_serializer, cookies.get(SESSION_COOKIE), SESSION_MAX_AGE,
                {"id", "device"},
            )
            if session_payload is not None and hmac.compare_digest(session_payload["device"], device):
                session = session_payload["id"]

        pending: list[IdentityCookie] = []
        if issue:
            if device is None:
                device = secrets.token_hex(32)
                pending.append(IdentityCookie(
                    DEVICE_COOKIE, self._device_serializer.dumps({"id": device}), DEVICE_MAX_AGE,
                ))
            if session is None:
                session = secrets.token_hex(32)
                pending.append(IdentityCookie(
                    SESSION_COOKIE,
                    self._session_serializer.dumps({"id": session, "device": device}),
                    None,
                ))
        return BrowserIdentity(device, session, tuple(pending))
