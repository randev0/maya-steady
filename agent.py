"""
Maya Steady Agent Runtime
Customer-facing model usage is bounded to extraction, summarization, and reply generation.
Business logic remains code-owned.
"""
import json
import re
from pathlib import Path
from typing import Optional
from uuid import UUID

import structlog

from config import settings
from database.dal import Database
from lead_state import (
    LeadState,
    normalize_budget_band,
    normalize_message_volume_band,
    normalize_timeline_band,
)
from llm import DisabledLLMClient, LLMError, LLMMessage, build_llm_client
from policy import apply_follow_up_update, build_policy_context, build_policy_decision
import tools.business_workflow_tool as bwt_tool
import tools.follow_up_scheduler as fus_tool
import tools.human_handoff_tool as hht_tool

log = structlog.get_logger()

_FALLBACK_REPLY = (
    "Thanks for reaching out. I want to make sure I guide you properly. "
    "Can you share a bit more about what you need help with?"
)
_OPTOUT_REPLY = "Understood. I won't send any more follow-ups. If you need anything later, just message here."
_HANDOFF_REPLY = "Okay, I'll connect you with our team and they’ll reach out shortly."
_INTERNAL_LABEL_TERMS = (
    "intent_stage",
    "qualification_stage",
    "lead_status",
    "policy_context",
    "tool_call",
    "backend policy",
)
_EXTRACTION_FIELDS = (
    "contact_name",
    "business_type",
    "pain_point",
    "current_process",
    "message_volume_band",
    "service_interest",
    "timeline_band",
    "budget_band",
)


async def _record_tool_outcome_safe(
    tool_name: str,
    success: bool,
    reason: Optional[str] = None,
    details: Optional[dict] = None,
    user_id: Optional[UUID] = None,
    conversation_id: Optional[UUID] = None,
) -> None:
    try:
        await Database.record_tool_outcome(
            tool_name=tool_name,
            success=success,
            reason=reason,
            details=details,
            user_id=user_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        log.warning("tool_outcome_audit_failed", tool=tool_name, error=str(exc))


class AgentError(Exception):
    """Raised when the agent cannot produce a real reply."""


_OPENCLAW_CONFIG = json.loads(
    (Path(__file__).parent / ".openclaw" / "openclaw.json").read_text()
)


def _load_system_prompt() -> str:
    base = Path(__file__).parent / "agent_config"
    return (
        (base / "system_prompt.md").read_text()
        + "\n\n"
        + (base / "personality_guidelines.md").read_text()
    )


_SYSTEM_PROMPT = _load_system_prompt()


def reload_prompt() -> None:
    global _SYSTEM_PROMPT
    _SYSTEM_PROMPT = _load_system_prompt()
    log.info("system_prompt_reloaded")


def _strip_reasoning(text: str) -> Optional[str]:
    parts = re.split(r"\n\n+", text, maxsplit=1)
    if len(parts) == 2:
        second = parts[1].strip()
        if re.match(r"^(I |This |Here |My |By |\d+\.)", second):
            stripped = parts[0].strip()
            if not stripped:
                log.warning("strip_reasoning_emptied_content", original_len=len(text), preview=text[:120])
                return None
            log.debug("reasoning_stripped", stripped_len=len(second))
            return stripped
    return text.strip() or None


def _format_history_block(history: list[dict]) -> str:
    lines = []
    for message in history:
        role = "Customer" if message["role"] == "user" else "Maya"
        lines.append(f"{role}: {message['content']}")
    return "\n".join(lines)


def _build_recent_context(summary: Optional[str], recent_messages: list[dict]) -> str:
    parts = []
    if summary:
        parts.append(f"Earlier conversation summary:\n{summary}")
    if recent_messages:
        parts.append(f"Recent messages:\n{_format_history_block(recent_messages)}")
    return "\n\n".join(parts).strip()


def _clean_text_value(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_extracted_value(field_name: str, value) -> Optional[str]:
    cleaned = _clean_text_value(value)
    if cleaned is None:
        return None
    if field_name == "budget_band":
        return normalize_budget_band(cleaned)
    if field_name == "timeline_band":
        return normalize_timeline_band(cleaned)
    if field_name == "message_volume_band":
        return normalize_message_volume_band(cleaned)
    return cleaned


def _merge_lead_updates(existing_state: LeadState, raw_updates: Optional[dict]) -> dict:
    if not isinstance(raw_updates, dict):
        return {}
    merged = {}
    for field_name in _EXTRACTION_FIELDS:
        if field_name not in raw_updates:
            continue
        normalized = _normalize_extracted_value(field_name, raw_updates.get(field_name))
        if normalized is None:
            continue
        current_value = getattr(existing_state, field_name, None)
        if current_value and str(current_value).strip():
            if str(current_value).strip().lower() != str(normalized).strip().lower():
                log.info(
                    "llm_extraction_preserved_existing_value",
                    field=field_name,
                    existing=str(current_value),
                    ignored=str(normalized),
                )
            continue
        merged[field_name] = normalized
    return merged


def _is_safe_reply(text: str) -> bool:
    lowered = text.lower()
    return not any(term in lowered for term in _INTERNAL_LABEL_TERMS)


class LeadQualificationAgent:
    def __init__(self):
        agent_cfg = _OPENCLAW_CONFIG["agent"]
        self.max_tokens = agent_cfg["max_tokens"]
        self.context_window = _OPENCLAW_CONFIG["memory"]["context_window"]
        try:
            self.llm = build_llm_client()
        except Exception as exc:
            log.error("llm_client_init_failed", error=str(exc))
            self.llm = DisabledLLMClient(reason="llm_client_init_failed")
        self.model = self.llm.model_name

    async def summarize_recent_conversation(
        self,
        *,
        history: list[dict],
        user_id: UUID,
        conversation_id: UUID,
    ) -> Optional[str]:
        if len(history) <= 8:
            return None

        older_messages = history[:-8]
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "Summarize the conversation so far for Maya. Keep it factual and short. "
                    "Focus on known lead details, objections, and what was already asked. "
                    "Do not invent facts."
                ),
            ),
            LLMMessage(role="user", content=_format_history_block(older_messages)),
        ]
        try:
            summary = await self.llm.generate_text(messages=messages, temperature=0.1, max_tokens=220)
            await _record_tool_outcome_safe(
                tool_name="llm_summarize_context",
                success=True,
                details={"chars": len(summary)},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return summary.strip()
        except LLMError as exc:
            log.warning("context_summary_failed", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_summarize_context",
                success=False,
                reason=str(exc),
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return None
        except Exception as exc:
            log.error("context_summary_unexpected_failure", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_summarize_context",
                success=False,
                reason="unexpected_llm_error",
                details={"error": str(exc)},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return None

    async def extract_lead_updates(
        self,
        *,
        latest_user_message: str,
        recent_context: str,
        existing_state: LeadState,
        user_id: UUID,
        conversation_id: UUID,
    ) -> dict:
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "Extract structured lead updates from the customer's latest message. "
                    "Return JSON only. Use only these keys if present: "
                    f"{', '.join(_EXTRACTION_FIELDS)}. "
                    "Handle Malay, English, and mixed Manglish. "
                    "Never include blanks, nulls, unknown fields, stage labels, prices, or policy decisions."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    f"Existing known fields: {existing_state.known_fields()}\n"
                    f"Recent context:\n{recent_context or '(none)'}\n\n"
                    f"Latest customer message:\n{latest_user_message}"
                ),
            ),
        ]
        try:
            raw_updates = await self.llm.generate_json(messages=messages, temperature=0.0, max_tokens=220)
            normalized = _merge_lead_updates(existing_state, raw_updates)
            await _record_tool_outcome_safe(
                tool_name="llm_extract_lead_updates",
                success=True,
                details={"updates": normalized},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return normalized
        except LLMError as exc:
            log.warning("lead_extraction_failed", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_extract_lead_updates",
                success=False,
                reason=str(exc),
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return {}
        except Exception as exc:
            log.error("lead_extraction_unexpected_failure", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_extract_lead_updates",
                success=False,
                reason="unexpected_llm_error",
                details={"error": str(exc)},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return {}

    async def generate_reply(
        self,
        *,
        lead_state: LeadState,
        policy_context: str,
        recent_context: str,
        latest_user_message: str,
        user_id: UUID,
        conversation_id: UUID,
    ) -> str:
        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="system",
                content=(
                    "Stay within Maya's tone and reply style. "
                    "Only write the customer-facing reply. "
                    "Do not expose backend labels, policy notes, tools, or internal reasoning. "
                    "Do not repeat questions already answered. "
                    "If a next question focus is provided, ask only that one question."
                ),
            ),
            LLMMessage(role="system", content=policy_context),
            LLMMessage(
                role="user",
                content=(
                    f"Normalized lead state:\n{lead_state.known_fields()}\n\n"
                    f"Recent context:\n{recent_context or '(none)'}\n\n"
                    f"Latest customer message:\n{latest_user_message}"
                ),
            ),
        ]
        try:
            reply = await self.llm.generate_text(messages=messages, temperature=0.35, max_tokens=260)
            cleaned = _strip_reasoning(reply) or reply.strip()
            if not cleaned or not _is_safe_reply(cleaned):
                raise LLMError("unsafe_or_empty_reply")
            await _record_tool_outcome_safe(
                tool_name="llm_generate_reply",
                success=True,
                details={"chars": len(cleaned)},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return cleaned
        except LLMError as exc:
            log.error("reply_generation_failed", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_generate_reply",
                success=False,
                reason=str(exc),
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return _FALLBACK_REPLY
        except Exception as exc:
            log.error("reply_generation_unexpected_failure", error=str(exc), conversation_id=str(conversation_id))
            await _record_tool_outcome_safe(
                tool_name="llm_generate_reply",
                success=False,
                reason="unexpected_llm_error",
                details={"error": str(exc)},
                user_id=user_id,
                conversation_id=conversation_id,
            )
            return _FALLBACK_REPLY

    async def _handoff_and_reply(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        external_id: str,
        lead_state: LeadState,
        handoff_reason: Optional[str],
        handoff_notes: Optional[str],
    ) -> str:
        handoff_result = await hht_tool.execute(
            user_id=user_id,
            conversation_id=conversation_id,
            reason=handoff_reason or "user_request",
            priority="high" if lead_state.intent_stage == "high" else "medium",
            notes=handoff_notes,
        )
        await _record_tool_outcome_safe(
            tool_name="human_handoff_tool",
            success=bool(handoff_result.get("success")),
            reason=handoff_result.get("error") or handoff_result.get("reason"),
            details=handoff_result,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if not handoff_result.get("success"):
            response_text = _FALLBACK_REPLY
        else:
            await Database.update_facts(
                user_id,
                {
                    "human_handoff_requested": True,
                    "handoff_reason": handoff_result.get("reason"),
                    "qualification_stage": "handoff",
                    "lead_status": "handed_off",
                },
                changed_by="policy",
                conversation_id=conversation_id,
            )
            response_text = _HANDOFF_REPLY
        await Database.store_message(conversation_id, "assistant", response_text)
        return response_text

    async def process_message(
        self,
        external_id: str,
        message: str,
        channel: str = "test",
    ) -> str:
        user = await Database.get_or_create_user(external_id, channel)
        user_id: UUID = user["id"]

        conv = await Database.get_active_conversation(user_id)
        if not conv:
            conv = await Database.create_conversation(user_id)
        conv_id: UUID = conv["id"]

        await Database.store_message(conv_id, "user", message)
        await Database.cancel_pending_followups(user_id)

        history = await Database.get_conversation_history(conv_id, limit=self.context_window)
        state_snapshot = await Database.get_lead_state_snapshot(user_id)
        lead_state = LeadState.from_snapshot(state_snapshot)

        if lead_state.opt_out:
            await Database.cancel_pending_followups(user_id)
            await Database.store_message(conv_id, "assistant", _OPTOUT_REPLY)
            return _OPTOUT_REPLY

        if lead_state.human_handoff_requested or lead_state.lead_status == "handed_off":
            await Database.store_message(conv_id, "assistant", _HANDOFF_REPLY)
            return _HANDOFF_REPLY

        initial_policy_decision = build_policy_decision(lead_state, message)
        if initial_policy_decision.state_updates:
            await Database.update_facts(
                user_id,
                initial_policy_decision.state_updates,
                changed_by="policy",
                conversation_id=conv_id,
            )

        if initial_policy_decision.opt_out:
            await Database.cancel_pending_followups(user_id)
            await Database.store_message(conv_id, "assistant", _OPTOUT_REPLY)
            return _OPTOUT_REPLY

        if initial_policy_decision.handoff_required:
            return await self._handoff_and_reply(
                user_id=user_id,
                conversation_id=conv_id,
                external_id=external_id,
                lead_state=lead_state,
                handoff_reason=initial_policy_decision.handoff_reason,
                handoff_notes=initial_policy_decision.handoff_notes,
            )

        summary = await self.summarize_recent_conversation(
            history=history,
            user_id=user_id,
            conversation_id=conv_id,
        )
        recent_context = _build_recent_context(summary, history[-8:])

        extracted_updates = await self.extract_lead_updates(
            latest_user_message=message,
            recent_context=recent_context,
            existing_state=lead_state,
            user_id=user_id,
            conversation_id=conv_id,
        )
        if extracted_updates:
            await Database.update_facts(
                user_id,
                extracted_updates,
                changed_by="llm_extraction",
                conversation_id=conv_id,
            )

        refreshed_snapshot = await Database.get_lead_state_snapshot(user_id)
        refreshed_state = LeadState.from_snapshot(refreshed_snapshot)

        if refreshed_state.service_interest and refreshed_state.budget_band and refreshed_state.timeline_band:
            score_result = await bwt_tool.execute(action="calculate_score", user_id=user_id)
            await _record_tool_outcome_safe(
                tool_name="business_workflow_tool",
                success="score" in score_result,
                reason=score_result.get("error") or score_result.get("label"),
                details=score_result,
                user_id=user_id,
                conversation_id=conv_id,
            )

        final_policy_decision = build_policy_decision(refreshed_state, message)
        if final_policy_decision.state_updates:
            await Database.update_facts(
                user_id,
                final_policy_decision.state_updates,
                changed_by="policy",
                conversation_id=conv_id,
            )
            refreshed_snapshot = await Database.get_lead_state_snapshot(user_id)
            refreshed_state = LeadState.from_snapshot(refreshed_snapshot)

        if final_policy_decision.handoff_required:
            return await self._handoff_and_reply(
                user_id=user_id,
                conversation_id=conv_id,
                external_id=external_id,
                lead_state=refreshed_state,
                handoff_reason=final_policy_decision.handoff_reason,
                handoff_notes=final_policy_decision.handoff_notes,
            )

        if final_policy_decision.follow_up_type:
            followup_result = await fus_tool.execute(
                action="schedule",
                user_id=user_id,
                conversation_id=conv_id,
                external_id=external_id,
                channel=channel,
                follow_up_type=final_policy_decision.follow_up_type,
            )
            await _record_tool_outcome_safe(
                tool_name="follow_up_scheduler",
                success=bool(followup_result.get("scheduled")),
                reason=followup_result.get("error") or followup_result.get("type"),
                details=followup_result,
                user_id=user_id,
                conversation_id=conv_id,
            )
            if followup_result.get("scheduled"):
                followup_updates = apply_follow_up_update(
                    refreshed_state,
                    final_policy_decision.follow_up_type,
                    followup_result.get("send_at"),
                )
                await Database.update_facts(
                    user_id,
                    followup_updates,
                    changed_by="policy",
                    conversation_id=conv_id,
                )
                refreshed_snapshot = await Database.get_lead_state_snapshot(user_id)
                refreshed_state = LeadState.from_snapshot(refreshed_snapshot)
                final_policy_decision = build_policy_decision(refreshed_state, message)

        policy_context = build_policy_context(refreshed_state, final_policy_decision)
        response_text = await self.generate_reply(
            lead_state=refreshed_state,
            policy_context=policy_context,
            recent_context=recent_context,
            latest_user_message=message,
            user_id=user_id,
            conversation_id=conv_id,
        )

        if not response_text or not response_text.strip():
            log.error("empty_reply_not_stored", user_id=str(user_id), conv_id=str(conv_id))
            response_text = _FALLBACK_REPLY

        await Database.store_message(conv_id, "assistant", response_text)
        return response_text


agent = LeadQualificationAgent()
