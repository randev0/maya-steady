from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest

from agent import LeadQualificationAgent, _HANDOFF_REPLY, _OPTOUT_REPLY
from lead_state import (
    LeadState,
    normalize_facts,
    normalize_intent_stage,
    normalize_lead_status,
    normalize_qualification_stage,
)
from policy import apply_follow_up_update, build_policy_context, build_policy_decision


def test_normalize_facts_maps_legacy_fields():
    facts = normalize_facts(
        {
            "budget_range": "rm5k_rm10k",
            "timeline": "within_1_month",
            "message_volume": "45/day",
            "current_tools": "reply manually",
        }
    )
    assert facts["budget_band"] == "high"
    assert facts["timeline_band"] == "immediate"
    assert facts["message_volume_band"] == "medium"
    assert facts["current_process"] == "reply manually"


def test_normalize_facts_maps_legacy_stage_values():
    facts = normalize_facts(
        {
            "intent_stage": "warm",
            "qualification_stage": "qualified",
            "lead_status": "handoff",
        }
    )
    assert facts["intent_stage"] == "medium"
    assert facts["qualification_stage"] == "interested"
    assert facts["lead_status"] == "handed_off"


def test_stage_normalizers_handle_legacy_aliases():
    assert normalize_intent_stage("hot_lead") == "high"
    assert normalize_qualification_stage("sales_ready") == "hot"
    assert normalize_lead_status("opted_out") == "disqualified"


def test_policy_selects_next_missing_field_without_reasking_known_data():
    state = LeadState(business_type="clinic", pain_point="slow replies", qualification_stage="qualifying")
    decision = build_policy_decision(state, "Hi, nak tahu lebih")
    assert decision.next_field == "current_process"
    context = build_policy_context(state, decision)
    assert "Do not ask again about: business_type, pain_point" in context


def test_policy_next_field_advances_after_new_data_is_already_known():
    state = LeadState(
        business_type="clinic",
        pain_point="slow replies",
        current_process="manual reply",
        qualification_stage="qualifying",
    )
    decision = build_policy_decision(state, "Kami masih reply manual buat masa ni")
    assert decision.next_field == "message_volume_band"


def test_policy_triggers_handoff_for_explicit_human_request():
    state = LeadState(
        business_type="clinic",
        pain_point="slow replies",
        service_interest="full",
        budget_band="high",
        timeline_band="immediate",
    )
    decision = build_policy_decision(state, "Boleh saya cakap dengan orang atau call me?")
    assert decision.handoff_required is True
    assert decision.handoff_reason == "user_request"


def test_policy_triggers_handoff_for_high_intent_qualified_lead():
    state = LeadState(
        business_type="clinic",
        pain_point="slow replies",
        current_process="manual",
        message_volume_band="high",
        service_interest="full",
        budget_band="high",
        timeline_band="immediate",
        score=4,
    )
    decision = build_policy_decision(state, "Harga pakej macam mana untuk clinic?")
    assert decision.handoff_required is True
    assert decision.handoff_reason == "high_score"
    assert decision.state_updates["qualification_stage"] == "handoff"
    assert decision.state_updates["lead_status"] == "handed_off"


def test_policy_does_not_ask_new_questions_during_handoff():
    state = LeadState(
        business_type="clinic",
        pain_point="slow replies",
        service_interest="full",
        human_handoff_requested=True,
        handoff_reason="user_request",
        lead_status="handed_off",
    )
    decision = build_policy_decision(state, "Hello?")
    assert decision.next_field is None
    assert decision.follow_up_type is None


def test_policy_respects_opt_out():
    state = LeadState()
    decision = build_policy_decision(state, "No thanks, stop messaging me")
    assert decision.opt_out is True
    assert decision.state_updates["lead_status"] == "disqualified"
    assert decision.state_updates["qualification_stage"] == "closed"


def test_policy_follow_up_stages_progress():
    state = LeadState(follow_up_count=1)
    decision = build_policy_decision(state, "ok")
    assert decision.follow_up_type == "few_hours"
    update = apply_follow_up_update(state, decision.follow_up_type, "2026-01-01T00:00:00+00:00")
    assert update["follow_up_stage"] == "nudge"
    assert update["follow_up_count"] == 2


@pytest.mark.asyncio
async def test_process_message_handoff_after_extraction_skips_reply_model():
    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20

    fake_user = {"id": uuid4(), "display_name": None}
    fake_conv = {"id": uuid4()}
    initial_snapshot = {
        "display_name": None,
        "facts": {
            "business_type": "clinic",
            "pain_point": "slow replies",
        },
        "score": 0,
        "score_label": "unqualified",
        "maya_paused": False,
        "conversation_status": "active",
        "open_handoff": None,
        "pending_followup": None,
    }
    refreshed_snapshot = {
        **initial_snapshot,
        "facts": {
            "business_type": "clinic",
            "pain_point": "slow replies",
            "service_interest": "full",
            "budget_band": "high",
            "timeline_band": "immediate",
        },
        "score": 4,
        "score_label": "qualified",
    }

    with patch("agent.Database") as mock_db, patch(
        "agent.bwt_tool.execute", AsyncMock(return_value={"score": 4, "label": "qualified"})
    ), patch(
        "agent.fus_tool.execute", AsyncMock()
    ), patch(
        "agent.hht_tool.execute",
        AsyncMock(return_value={"success": True, "reason": "high_score", "handoff_id": "1", "status": "pending"}),
    ), patch.object(
        agent_instance, "extract_lead_updates", AsyncMock(return_value={
            "service_interest": "full",
            "budget_band": "high",
            "timeline_band": "immediate",
        })
    ), patch.object(
        agent_instance, "generate_reply", AsyncMock(side_effect=AssertionError("reply model should not run"))
    ), patch.object(
        agent_instance, "summarize_recent_conversation", AsyncMock(return_value=None)
    ):
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.store_message = AsyncMock()
        mock_db.cancel_pending_followups = AsyncMock()
        mock_db.get_conversation_history = AsyncMock(return_value=[])
        mock_db.get_lead_state_snapshot = AsyncMock(side_effect=[initial_snapshot, refreshed_snapshot, refreshed_snapshot])
        mock_db.update_facts = AsyncMock()

        result = await agent_instance.process_message("6012", "Nak proceed cepat, boleh tunjuk demo?", "whatsapp")

    assert result == _HANDOFF_REPLY


@pytest.mark.asyncio
async def test_process_message_short_circuits_opt_out_without_model():
    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20

    fake_user = {"id": uuid4(), "display_name": None}
    fake_conv = {"id": uuid4()}
    snapshot = {
        "display_name": None,
        "facts": {},
        "score": 0,
        "score_label": "unqualified",
        "maya_paused": False,
        "conversation_status": "active",
        "open_handoff": None,
        "pending_followup": None,
    }

    with patch("agent.Database") as mock_db, patch("agent.bwt_tool.execute", AsyncMock()), patch(
        "agent.fus_tool.execute", AsyncMock()
    ), patch("agent.hht_tool.execute", AsyncMock()), patch.object(
        agent_instance, "extract_lead_updates", AsyncMock(side_effect=AssertionError("extractor should not run"))
    ), patch.object(
        agent_instance, "generate_reply", AsyncMock(side_effect=AssertionError("reply model should not run"))
    ):
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.store_message = AsyncMock()
        mock_db.cancel_pending_followups = AsyncMock()
        mock_db.get_conversation_history = AsyncMock(return_value=[])
        mock_db.get_lead_state_snapshot = AsyncMock(return_value=snapshot)
        mock_db.update_facts = AsyncMock()

        result = await agent_instance.process_message("6012", "stop please", "whatsapp")

    assert result == _OPTOUT_REPLY


@pytest.mark.asyncio
async def test_process_message_short_circuits_handoff_without_model():
    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20

    fake_user = {"id": uuid4(), "display_name": None}
    fake_conv = {"id": uuid4()}
    snapshot = {
        "display_name": None,
        "facts": {
            "business_type": "clinic",
            "pain_point": "slow replies",
            "service_interest": "full",
            "budget_band": "high",
            "timeline_band": "immediate",
        },
        "score": 4,
        "score_label": "qualified",
        "maya_paused": False,
        "conversation_status": "active",
        "open_handoff": None,
        "pending_followup": None,
    }

    with patch("agent.Database") as mock_db, patch(
        "agent.bwt_tool.execute", AsyncMock(return_value={"score": 4})
    ), patch(
        "agent.fus_tool.execute", AsyncMock()
    ), patch(
        "agent.hht_tool.execute",
        AsyncMock(return_value={"success": True, "reason": "user_request", "handoff_id": "1", "status": "pending"}),
    ), patch.object(
        agent_instance, "extract_lead_updates", AsyncMock(side_effect=AssertionError("extractor should not run"))
    ), patch.object(
        agent_instance, "generate_reply", AsyncMock(side_effect=AssertionError("reply model should not run"))
    ):
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.store_message = AsyncMock()
        mock_db.cancel_pending_followups = AsyncMock()
        mock_db.get_conversation_history = AsyncMock(return_value=[])
        mock_db.get_lead_state_snapshot = AsyncMock(return_value=snapshot)
        mock_db.update_facts = AsyncMock()

        result = await agent_instance.process_message("6012", "Can I get a demo and payment details?", "whatsapp")

    assert result == _HANDOFF_REPLY


@pytest.mark.asyncio
async def test_process_message_respects_existing_handoff_without_creating_new_one():
    agent_instance = LeadQualificationAgent.__new__(LeadQualificationAgent)
    agent_instance.model = "test-model"
    agent_instance.max_tokens = 100
    agent_instance.context_window = 20

    fake_user = {"id": uuid4(), "display_name": None}
    fake_conv = {"id": uuid4()}
    snapshot = {
        "display_name": None,
        "facts": {"human_handoff_requested": True, "lead_status": "handed_off"},
        "score": 4,
        "score_label": "qualified",
        "maya_paused": False,
        "conversation_status": "handoff",
        "open_handoff": {"id": "h1", "reason": "user_request"},
        "pending_followup": None,
    }

    with patch("agent.Database") as mock_db, patch(
        "agent.hht_tool.execute", AsyncMock(side_effect=AssertionError("handoff should not be recreated"))
    ), patch.object(
        agent_instance, "extract_lead_updates", AsyncMock(side_effect=AssertionError("extractor should not run"))
    ), patch.object(
        agent_instance, "generate_reply", AsyncMock(side_effect=AssertionError("reply model should not run"))
    ):
        mock_db.get_or_create_user = AsyncMock(return_value=fake_user)
        mock_db.get_active_conversation = AsyncMock(return_value=fake_conv)
        mock_db.store_message = AsyncMock()
        mock_db.cancel_pending_followups = AsyncMock()
        mock_db.get_conversation_history = AsyncMock(return_value=[])
        mock_db.get_lead_state_snapshot = AsyncMock(return_value=snapshot)

        result = await agent_instance.process_message("6012", "Hello?", "whatsapp")

    assert result == _HANDOFF_REPLY
