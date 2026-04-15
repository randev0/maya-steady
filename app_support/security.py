import hashlib
import hmac
from typing import Optional
from uuid import UUID

from fastapi import Header, HTTPException

from config import settings


def is_valid_fb_signature(body: bytes, signature: Optional[str]) -> bool:
    if not settings.fb_app_secret or not signature:
        return False
    try:
        scheme, expected = signature.split("=", 1)
    except ValueError:
        return False
    if scheme != "sha256":
        return False
    digest = hmac.new(
        settings.fb_app_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, expected)


def extract_admin_token(
    authorization: Optional[str],
    x_admin_token: Optional[str],
) -> Optional[str]:
    if x_admin_token:
        return x_admin_token.strip() or None
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def require_admin_access(
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None),
) -> None:
    expected = settings.admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API token is not configured")
    provided = extract_admin_token(authorization, x_admin_token)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def pause_action_secret() -> str:
    return settings.pause_action_secret or settings.wa_verify_token


def build_pause_action_token(conv_id: UUID, paused: bool) -> str:
    payload = f"{conv_id}:{int(paused)}".encode()
    return hmac.new(pause_action_secret().encode(), payload, hashlib.sha256).hexdigest()


def verify_pause_action_token(conv_id: UUID, paused: bool, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(token, build_pause_action_token(conv_id, paused))
