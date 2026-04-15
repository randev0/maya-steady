from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from database.dal import Database
from main import (
    PauseRequest,
    _build_pause_action_token,
    _extract_admin_token,
    _is_valid_fb_signature,
    _require_admin_access,
    set_pause_by_conv,
)
from whatsapp_identity import normalize_external_id, normalize_whatsapp_id


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireContext(self._conn)


def test_normalize_whatsapp_id_variants_match():
    assert normalize_whatsapp_id("60123456789@s.whatsapp.net") == "60123456789"
    assert normalize_whatsapp_id("60123456789@c.us") == "60123456789"
    assert normalize_whatsapp_id("60123456789@lid") == "60123456789"
    assert normalize_whatsapp_id(" 60123456789 ") == "60123456789"


def test_normalize_external_id_only_changes_whatsapp():
    assert normalize_external_id("whatsapp", "60123456789@c.us") == "60123456789"
    assert normalize_external_id("facebook", "123456789") == "123456789"


@pytest.mark.asyncio
async def test_get_or_create_user_uses_atomic_upsert_with_normalized_whatsapp_id():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "user1", "external_id": "60123456789", "channel": "whatsapp"})

    original_pool = Database.pool
    Database.pool = _FakePool(conn)
    try:
        user = await Database.get_or_create_user("60123456789@c.us", "whatsapp")
    finally:
        Database.pool = original_pool

    assert user["external_id"] == "60123456789"
    sql = conn.fetchrow.await_args.args[0]
    assert "ON CONFLICT (external_id) DO UPDATE" in sql
    assert conn.fetchrow.await_args.args[1:] == ("60123456789", "whatsapp")


@pytest.mark.asyncio
async def test_set_pause_requires_valid_signed_token():
    conv_id = uuid4()

    with patch("main.Database") as mock_db:
        mock_db.get_conversation_detail = AsyncMock(return_value={"user_id": uuid4(), "external_id": "60123456789"})
        mock_db.set_user_paused = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await set_pause_by_conv(conv_id, PauseRequest(paused=True, token="bad-token"))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_set_pause_accepts_valid_signed_token():
    conv_id = uuid4()
    user_id = uuid4()
    token = _build_pause_action_token(conv_id, True)

    with patch("main.Database") as mock_db:
        mock_db.get_conversation_detail = AsyncMock(return_value={"user_id": user_id, "external_id": "60123456789@c.us"})
        mock_db.set_user_paused = AsyncMock()

        result = await set_pause_by_conv(conv_id, PauseRequest(paused=True, token=token))

    assert result == {"ok": True, "paused": True}
    mock_db.set_user_paused.assert_awaited_once_with(user_id, True)


def test_extract_admin_token_supports_bearer_and_header():
    assert _extract_admin_token("Bearer topsecret", None) == "topsecret"
    assert _extract_admin_token(None, "header-secret") == "header-secret"
    assert _extract_admin_token("Basic nope", None) is None


def test_require_admin_access_accepts_matching_token():
    with patch("main.settings.admin_api_token", "topsecret"):
        _require_admin_access(authorization="Bearer topsecret", x_admin_token=None)


def test_require_admin_access_rejects_bad_token():
    with patch("main.settings.admin_api_token", "topsecret"):
        with pytest.raises(HTTPException) as exc:
            _require_admin_access(authorization="Bearer wrong", x_admin_token=None)
    assert exc.value.status_code == 401


def test_is_valid_fb_signature_checks_sha256():
    body = b'{"object":"page"}'
    secret = "fb-secret"
    token = _build_pause_action_token(uuid4(), True)
    assert token  # guard import of hashlib/hmac path stays live

    import hashlib
    import hmac

    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with patch("main.settings.fb_app_secret", secret):
        assert _is_valid_fb_signature(body, signature) is True
        assert _is_valid_fb_signature(body, "sha256=bad") is False
