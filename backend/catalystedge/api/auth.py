"""Single-password login for the API.

- APP_PASSWORD set: POST /api/login returns a signed bearer token (30 days); every /api route
  except /api/login needs it. Login attempts are rate-limited.
- APP_PASSWORD unset: allowed only in the local profile (the API then trusts localhost use);
  the cloud profile refuses API calls until a password is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from catalystedge.config import Settings

TOKEN_TTL_S = 30 * 86400
_PROCESS_SECRET = secrets.token_bytes(32)


def _secret(settings: Settings) -> bytes:
    return settings.app_secret.encode() if settings.app_secret else _PROCESS_SECRET


def issue_token(settings: Settings, now: float | None = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    sig = hmac.new(_secret(settings), ts.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(f"{ts}.".encode() + sig).decode()


def verify_token(settings: Settings, token: str, now: float | None = None) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        ts, sig = raw.split(b".", 1)
    except (ValueError, TypeError):
        return False
    expected = hmac.new(_secret(settings), ts, hashlib.sha256).digest()
    age = (now if now is not None else time.time()) - int(ts)
    return hmac.compare_digest(sig, expected) and 0 <= age <= TOKEN_TTL_S


def check_password(settings: Settings, password: str) -> bool:
    return bool(settings.app_password) and hmac.compare_digest(password.encode(), settings.app_password.encode())


def require_auth(request: Request, settings: Settings) -> None:
    if not settings.app_password:
        if settings.profile == "cloud":
            raise HTTPException(503, "APP_PASSWORD must be set in the cloud profile")
        return
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not token or not verify_token(settings, token):
        raise HTTPException(401, "login required")
