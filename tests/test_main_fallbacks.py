from unittest.mock import AsyncMock, patch

import pytest

from agent import AgentError, _FALLBACK_REPLY
from main import _dispatch_due_followups_once, _handle_fb_message, _wa_handle_message, _wa_handle_unsupported, chat_history


@pytest.mark.asyncio
async def test_handle_fb_message_sends_fallback_on_processing_error():
    with patch("main.agent.process_message", AsyncMock(side_effect=AgentError("boom"))), patch(
        "main._store_outbound_message", AsyncMock()
    ) as mock_store, patch(
        "main._send_fb_reply", AsyncMock()
    ) as mock_send:
        await _handle_fb_message("user-123", "Hi")

    mock_store.assert_awaited_once_with("user-123", "facebook", _FALLBACK_REPLY)
    mock_send.assert_awaited_once_with("user-123", _FALLBACK_REPLY)


@pytest.mark.asyncio
async def test_wa_handle_message_sends_fallback_on_processing_error():
    fake_user = {"id": "user-id", "display_name": None}
    fake_conv = {"id": "conv-id"}

    with patch("main._is_manager", return_value=False), patch(
        "main._is_paused", return_value=False
    ), patch("main._check_trial_gate", AsyncMock(return_value=None)), patch(
        "main.Database"
    ) as mock_db, patch("main.agent.process_message", AsyncMock(side_effect=AgentError("boom"))), patch(
        "main._store_outbound_message", AsyncMock()
    ) as mock_store, patch(
        "main._wa_send_text", AsyncMock()
    ) as mock_send, patch("main._tg_alert", AsyncMock()):
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_pause_state = AsyncMock(return_value={"paused": False, "paused_at": None})
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.get_conversation_history = AsyncMock(return_value=[{"role": "user", "content": "Hi"}])
        mock_db.clear_pause_history = AsyncMock()
        mock_db.store_message = AsyncMock()

        await _wa_handle_message("60123456789", "Hi", "Aisyah")

    mock_store.assert_awaited_once_with("60123456789", "whatsapp", _FALLBACK_REPLY)
    mock_send.assert_awaited_once_with("60123456789", _FALLBACK_REPLY)


@pytest.mark.asyncio
async def test_chat_history_does_not_create_user_for_missing_external_id():
    with patch("main.Database") as mock_db:
        mock_db.get_user_by_external_id = AsyncMock(return_value=None)
        result = await chat_history("missing-user")

    assert result == {"messages": []}
    mock_db.get_user_by_external_id.assert_awaited_once_with("missing-user")


@pytest.mark.asyncio
async def test_dispatch_due_followups_persists_message_before_send():
    followup = {
        "id": "fu1",
        "user_id": "user1",
        "conversation_id": "conv1",
        "external_id": "6012",
        "channel": "whatsapp",
        "message": "Checking in",
        "follow_up_type": "30min",
        "message_id": None,
    }
    with patch("main.Database") as mock_db, patch(
        "main._store_outbound_message", AsyncMock(return_value={"id": "msg1"})
    ) as mock_store, patch("main._wa_send_text", AsyncMock()) as mock_send:
        mock_db.get_due_followups = AsyncMock(return_value=[followup])
        mock_db.attach_followup_message = AsyncMock()
        mock_db.mark_followup_sent = AsyncMock()
        mock_db.record_tool_outcome = AsyncMock()

        await _dispatch_due_followups_once()

    mock_store.assert_awaited_once_with(
        external_id="6012",
        channel="whatsapp",
        text="Checking in",
        conversation_id="conv1",
        source="follow_up",
    )
    mock_db.attach_followup_message.assert_awaited_once_with("fu1", "msg1")
    mock_send.assert_awaited_once_with("6012", "Checking in")


@pytest.mark.asyncio
async def test_dispatch_due_followups_does_not_duplicate_stored_message():
    followup = {
        "id": "fu1",
        "user_id": "user1",
        "conversation_id": "conv1",
        "external_id": "6012",
        "channel": "whatsapp",
        "message": "Checking in",
        "follow_up_type": "30min",
        "message_id": "msg1",
    }
    with patch("main.Database") as mock_db, patch(
        "main._store_outbound_message", AsyncMock()
    ) as mock_store, patch("main._wa_send_text", AsyncMock()) as mock_send:
        mock_db.get_due_followups = AsyncMock(return_value=[followup])
        mock_db.mark_followup_sent = AsyncMock()
        mock_db.record_tool_outcome = AsyncMock()

        await _dispatch_due_followups_once()

    mock_store.assert_not_awaited()
    mock_send.assert_awaited_once_with("6012", "Checking in")


@pytest.mark.asyncio
async def test_wa_handle_unsupported_persists_message():
    with patch("main._store_outbound_message", AsyncMock()) as mock_store, patch(
        "main._wa_send_text", AsyncMock()
    ) as mock_send:
        await _wa_handle_unsupported("6012")

    expected = "Hai! Saya Maya 😊 Saya hanya boleh baca teks buat masa ni. Boleh taip soalan awak?"
    mock_store.assert_awaited_once_with("6012", "whatsapp", expected)
    mock_send.assert_awaited_once_with("6012", expected)
