"""
human_handoff_tool
Escalates the conversation to a human operator.
"""
from typing import Optional
from uuid import UUID

import structlog

from database.dal import Database

log = structlog.get_logger()

_CANONICAL_REASONS = {
    "high_score",
    "user_request",
    "urgency",
    "frustration",
    "out_of_scope",
}


def _normalize_reason(reason: str) -> str:
    normalized = (reason or "").strip().lower().replace("-", "_")
    if normalized in _CANONICAL_REASONS:
        return normalized
    if any(token in normalized for token in ("trial", "signup", "payment", "demo", "setup", "proceed", "buy", "purchase")):
        return "user_request"
    if any(token in normalized for token in ("urgent", "asap", "immediate")):
        return "urgency"
    if any(token in normalized for token in ("frustrat", "confus", "angry", "upset")):
        return "frustration"
    return "user_request"

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "human_handoff_tool",
        "description": (
            "Escalate the conversation to a human operator. "
            "MUST be called when: (1) lead score >= 4, (2) user explicitly asks for a human, "
            "(3) user expresses urgency or frustration, or (4) query is outside your scope. "
            "After calling this tool, inform the user a human will follow up shortly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["high_score", "user_request", "urgency", "frustration", "out_of_scope"],
                    "description": "Why this lead is being escalated.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": (
                        "high = hot lead (score 4-5) or urgent; "
                        "medium = warm lead or user request; "
                        "low = low priority or out of scope."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Brief context note for the operator (max 200 chars).",
                },
            },
            "required": ["reason", "priority"],
        },
    },
}

async def execute(
    user_id: UUID,
    conversation_id: Optional[UUID],
    reason: str,
    priority: str,
    notes: Optional[str] = None,
) -> dict:
    normalized_reason = _normalize_reason(reason)
    lead_data = await Database.get_lead_data(user_id)
    lead_info = lead_data.get("facts", {})

    try:
        handoff = await Database.create_handoff(
            user_id=user_id,
            conversation_id=conversation_id,
            reason=normalized_reason,
            priority=priority,
            notes=notes,
        )
    except Exception as exc:
        log.error(
            "handoff_create_failed",
            user_id=str(user_id),
            conversation_id=str(conversation_id) if conversation_id else None,
            reason=normalized_reason,
            priority=priority,
            error=str(exc),
        )
        return {
            "success": False,
            "handoff_id": None,
            "status": "failed",
            "error": str(exc),
            "lead_info": lead_info,
            "reason": normalized_reason,
        }

    return {
        "success": True,
        "handoff_id": str(handoff["id"]),
        "status": handoff.get("status", "pending"),
        "error": None,
        "lead_info": lead_info,
        "reason": normalized_reason,
    }
