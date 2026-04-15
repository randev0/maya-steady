"""
Regression tests for Maya's silent failure modes.

Bug 1: _strip_reasoning stripped bullet lists → dangling colon sent, or empty string sent
Bug 2: _wa_send_text("") silently no-oped via range(0,0,4000) → no message, no log
Bug 3: reply generation or orchestration returns empty → fallback must still be sent
"""
import asyncio
import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Make the project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import _strip_reasoning, _FALLBACK_REPLY


# ------------------------------------------------------------------ #
# Bug 1 — _strip_reasoning must never produce "" from real content
# ------------------------------------------------------------------ #

def test_strip_reasoning_bullet_list_not_stripped():
    """Regression: bullet lists after blank line were matched as reasoning and stripped."""
    text = "Kita ada beberapa package:\n\n- Basic\n- Standard\n- Premium"
    result = _strip_reasoning(text)
    assert result is not None
    assert "Basic" in result
    assert "Premium" in result


def test_strip_reasoning_numbered_list_is_stripped():
    """Numbered post-reply lists are internal reasoning — should be stripped."""
    text = "Okay, let me help you.\n\n1. I first checked your facts\n2. Then I retrieved skills"
    result = _strip_reasoning(text)
    assert result == "Okay, let me help you."


def test_strip_reasoning_first_person_block_is_stripped():
    """'I used...' blocks after reply are internal reasoning — should be stripped."""
    text = "Sure, here are the details.\n\nI used a consultative approach here."
    result = _strip_reasoning(text)
    assert result == "Sure, here are the details."


def test_strip_reasoning_returns_none_not_empty_string():
    """
    If stripping would empty the content, return None so caller can fall back
    to raw text. Must never return "".
    """
    # A reply that is ONLY a first-person reasoning block with no real content before it
    text = "\n\nI approached this by checking the user's profile first."
    result = _strip_reasoning(text)
    # Either None (stripped everything) or the original text — never ""
    assert result != ""


def test_strip_reasoning_plain_text_unchanged():
    """Normal short reply with no reasoning block passes through untouched."""
    text = "Okay makes sense. Dalam sehari berapa mesej masuk?"
    result = _strip_reasoning(text)
    assert result == text


def test_strip_reasoning_single_line_unchanged():
    """Single-line reply with no double newline is never touched."""
    text = "Package apa yang you nak?"
    result = _strip_reasoning(text)
    assert result == text


def test_strip_reasoning_never_returns_empty_string_on_any_input():
    """Exhaustive: _strip_reasoning must never return the empty string."""
    inputs = [
        "Hello",
        "Hello\n\n- item",
        "Hello\n\nI did things",
        "Hello\n\n1. step one",
        "\n\nI did things",
        "   ",
        "",
    ]
    for text in inputs:
        result = _strip_reasoning(text)
        assert result != "", f"Got empty string for input: {text!r}"


# ------------------------------------------------------------------ #
# Bug 2 — _wa_send_text must raise on empty input, never silently no-op
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_wa_send_text_empty_string_raises():
    """Regression: range(0, 0, 4000) was empty, loop never ran, nothing sent, no error."""
    # Import here so the env isn't needed at collection time
    from main import _wa_send_text
    with pytest.raises((ValueError, Exception), match="empty"):
        await _wa_send_text("12345", "")


@pytest.mark.asyncio
async def test_wa_send_text_whitespace_only_raises():
    from main import _wa_send_text
    with pytest.raises((ValueError, Exception)):
        await _wa_send_text("12345", "   \n  ")


@pytest.mark.asyncio
async def test_wa_send_text_valid_message_calls_http():
    """A non-empty message makes exactly one HTTP POST."""
    from main import _wa_send_text
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("main.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await _wa_send_text("12345", "Hello Maya")
        assert mock_client.post.called


# ------------------------------------------------------------------ #
# Bug 3 — reply generation and process_message must never go silent
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_generate_reply_returns_fallback_on_model_failure():
    from agent import LeadQualificationAgent
    from lead_state import LeadState
    from llm import LLMError
    from uuid import uuid4

    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20
    agent_instance.llm = MagicMock()
    agent_instance.llm.generate_text = AsyncMock(side_effect=LLMError("boom"))

    with patch("agent.Database.record_tool_outcome", AsyncMock()):
        result = await agent_instance.generate_reply(
            lead_state=LeadState(),
            policy_context="Your next question focus is: business_type.",
            recent_context="Customer: Hi",
            latest_user_message="Hi",
            user_id=uuid4(),
            conversation_id=uuid4(),
        )

    assert result == _FALLBACK_REPLY


@pytest.mark.asyncio
async def test_generate_reply_returns_fallback_on_empty_model_output():
    from agent import LeadQualificationAgent
    from lead_state import LeadState
    from uuid import uuid4

    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20
    agent_instance.llm = MagicMock()
    agent_instance.llm.generate_text = AsyncMock(return_value="   ")

    with patch("agent.Database.record_tool_outcome", AsyncMock()):
        result = await agent_instance.generate_reply(
            lead_state=LeadState(),
            policy_context="Your next question focus is: business_type.",
            recent_context="Customer: Hi",
            latest_user_message="Hi",
            user_id=uuid4(),
            conversation_id=uuid4(),
        )

    assert result == _FALLBACK_REPLY


@pytest.mark.asyncio
async def test_generate_reply_returns_fallback_on_unexpected_model_exception():
    from agent import LeadQualificationAgent
    from lead_state import LeadState
    from uuid import uuid4

    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20
    agent_instance.llm = MagicMock()
    agent_instance.llm.generate_text = AsyncMock(side_effect=RuntimeError("unexpected boom"))

    with patch("agent.Database.record_tool_outcome", AsyncMock()):
        result = await agent_instance.generate_reply(
            lead_state=LeadState(),
            policy_context="Your next question focus is: business_type.",
            recent_context="Customer: Hi",
            latest_user_message="Hi",
            user_id=uuid4(),
            conversation_id=uuid4(),
        )

    assert result == _FALLBACK_REPLY


# ------------------------------------------------------------------ #
# End-to-end: process_message never returns "" or None
# ------------------------------------------------------------------ #

@pytest.mark.asyncio
async def test_process_message_never_returns_empty(tmp_path):
    """Even if _agent_loop returns '', process_message returns the fallback."""
    from agent import LeadQualificationAgent, _FALLBACK_REPLY
    from uuid import uuid4

    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20

    fake_user = {"id": uuid4(), "display_name": None}
    fake_conv = {"id": uuid4()}

    with patch("agent.Database") as mock_db:
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.store_message = AsyncMock()
        mock_db.cancel_pending_followups = AsyncMock()
        mock_db.get_conversation_history = AsyncMock(return_value=[])
        mock_db.get_lead_state_snapshot = AsyncMock(
            return_value={
                "display_name": None,
                "facts": {},
                "score": 0,
                "score_label": "unqualified",
                "maya_paused": False,
                "conversation_status": "active",
                "open_handoff": None,
                "pending_followup": None,
            }
        )
        mock_db.update_facts = AsyncMock()
        mock_db.record_tool_outcome = AsyncMock()

        with patch.object(agent_instance, "summarize_recent_conversation", AsyncMock(return_value=None)), patch.object(
            agent_instance, "extract_lead_updates", AsyncMock(return_value={})
        ), patch.object(
            agent_instance, "generate_reply", AsyncMock(return_value="")
        ):
            result = await agent_instance.process_message(
                external_id="12345",
                message="Hello",
                channel="test",
            )

    assert result, "Must be truthy"
    assert result.strip() != ""
    assert result == _FALLBACK_REPLY
