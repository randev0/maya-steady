from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agent import LeadQualificationAgent, _merge_lead_updates
from lead_state import LeadState
from llm import DisabledLLMClient, build_llm_client
from tools import business_workflow_tool, human_handoff_tool


def test_build_llm_client_defaults_to_ollama():
    with patch("llm.settings", SimpleNamespace(
        llm_provider="ollama",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="qwen2.5:1.5b",
        llm_timeout_seconds=20.0,
        llm_max_retries=2,
    )):
        client = build_llm_client()

    assert client.provider_name == "ollama"
    assert client.model_name == "qwen2.5:1.5b"


def test_merge_lead_updates_keeps_supported_normalized_fields_only():
    state = LeadState(budget_band="high", contact_name="Aisyah")

    updates = _merge_lead_updates(
        state,
        {
            "contact_name": " ",
            "budget_band": "",
            "timeline_band": "within_1_month",
            "message_volume_band": "45/day",
            "unsupported_key": "ignore me",
            "pain_point": "lambat reply customer",
        },
    )

    assert updates == {
        "timeline_band": "immediate",
        "message_volume_band": "medium",
        "pain_point": "lambat reply customer",
    }


def test_merge_lead_updates_does_not_overwrite_existing_good_values():
    state = LeadState(business_type="clinic", pain_point="slow replies")

    updates = _merge_lead_updates(
        state,
        {
            "business_type": "restaurant",
            "pain_point": "missed follow ups",
            "service_interest": "full",
        },
    )

    assert updates == {"service_interest": "full"}


@pytest.mark.asyncio
async def test_extract_lead_updates_returns_supported_fields_only():
    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.llm = SimpleNamespace(
        generate_json=AsyncMock(
            return_value={
                "business_type": "clinic",
                "message_volume_band": "120/day",
                "budget_band": "rm5k_rm10k",
                "lead_status": "handed_off",
            }
        )
    )

    with patch("agent.Database.record_tool_outcome", AsyncMock()):
        result = await agent_instance.extract_lead_updates(
            latest_user_message="Saya run clinic, sehari dalam 120 mesej.",
            recent_context="Customer: Hi",
            existing_state=LeadState(),
            user_id=uuid4(),
            conversation_id=uuid4(),
        )

    assert result == {
        "business_type": "clinic",
        "message_volume_band": "high",
        "budget_band": "high",
    }


def test_agent_init_falls_back_to_disabled_client_on_bad_provider():
    with patch("agent.build_llm_client", side_effect=ValueError("Unsupported LLM_PROVIDER: nope")):
        agent_instance = LeadQualificationAgent()

    assert isinstance(agent_instance.llm, DisabledLLMClient)
    assert agent_instance.model == "unavailable"


@pytest.mark.asyncio
async def test_handoff_tool_success():
    user_id = uuid4()
    conversation_id = uuid4()
    handoff_id = uuid4()

    with patch.object(human_handoff_tool, "Database") as mock_db:
        mock_db.get_lead_data = AsyncMock(
            return_value={"facts": {"business_type": "clinic", "pain_point": "slow replies"}}
        )
        mock_db.create_handoff = AsyncMock(
            return_value={"id": handoff_id, "status": "pending"}
        )

        result = await human_handoff_tool.execute(
            user_id=user_id,
            conversation_id=conversation_id,
            reason="user_request",
            priority="high",
            notes="Asked for a human",
        )

    assert result == {
        "success": True,
        "handoff_id": str(handoff_id),
        "status": "pending",
        "error": None,
        "lead_info": {"business_type": "clinic", "pain_point": "slow replies"},
        "reason": "user_request",
    }


@pytest.mark.asyncio
async def test_handoff_tool_failure_returns_structured_error():
    user_id = uuid4()
    conversation_id = uuid4()

    with patch.object(human_handoff_tool, "Database") as mock_db:
        mock_db.get_lead_data = AsyncMock(return_value={"facts": {"business_type": "clinic"}})
        mock_db.create_handoff = AsyncMock(side_effect=RuntimeError("db unavailable"))

        result = await human_handoff_tool.execute(
            user_id=user_id,
            conversation_id=conversation_id,
            reason="user_request",
            priority="high",
        )

    assert result["success"] is False
    assert result["handoff_id"] is None
    assert result["status"] == "failed"
    assert result["error"] == "db unavailable"


@pytest.mark.asyncio
async def test_export_lead_uses_internal_user_lookup():
    user_id = uuid4()

    with patch.object(business_workflow_tool, "Database") as mock_db, patch.object(
        business_workflow_tool, "settings", SimpleNamespace(lead_export_webhook_url="https://example.com/hook")
    ), patch.object(business_workflow_tool.httpx, "AsyncClient") as mock_client_cls:
        mock_db.get_or_create_profile = AsyncMock(
            return_value={"score": 4, "score_label": "qualified", "facts": {"service_interest": "ai agent"}}
        )
        mock_db.get_user_by_id = AsyncMock(
            return_value={"channel": "whatsapp", "display_name": "Aisyah"}
        )

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
        mock_client_cls.return_value = mock_client

        result = await business_workflow_tool.execute("export_lead", user_id)

    assert result == {"exported": True, "status_code": 200}
    mock_db.get_user_by_id.assert_awaited_once_with(user_id)


@pytest.mark.asyncio
async def test_export_lead_missing_user_fails_safely():
    user_id = uuid4()

    with patch.object(business_workflow_tool, "Database") as mock_db, patch.object(
        business_workflow_tool, "settings", SimpleNamespace(lead_export_webhook_url="https://example.com/hook")
    ):
        mock_db.get_or_create_profile = AsyncMock(
            return_value={"score": 4, "score_label": "qualified", "facts": {}}
        )
        mock_db.get_user_by_id = AsyncMock(return_value=None)

        result = await business_workflow_tool.execute("export_lead", user_id)

    assert result["exported"] is False
    assert "User not found" in result["error"]


def test_score_from_facts_accepts_normalized_enum_values():
    score, label = business_workflow_tool._score_from_facts(
        {"budget_range": "high", "timeline": "immediate", "service_interest": "full"}
    )
    assert score == 5
    assert label == "qualified"


def test_score_from_facts_accepts_legacy_enum_values():
    score, label = business_workflow_tool._score_from_facts(
        {"budget_range": "rm2k_rm5k", "timeline": "1_3_months", "service_interest": "simple"}
    )
    assert score == 3
    assert label == "warm"
