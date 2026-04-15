"""
follow_up_scheduler tool
Schedules re-engagement messages for inactive leads and stores lead data for delivery.
"""
from datetime import datetime, timezone, timedelta
from uuid import UUID
from typing import Optional

from database.dal import Database
from lead_state import FOLLOW_UP_TYPE_TO_STAGE, normalize_facts, normalize_follow_up_stage

FOLLOW_UP_DELAYS = {
    "30min":     timedelta(minutes=30),
    "few_hours": timedelta(hours=3),
    "next_day":  timedelta(hours=22),
}

DEFAULT_MESSAGES = {
    "30min": (
        "Hey 😊 tadi kita tengah bincang pasal AI agent untuk bisnes you.\n"
        "Still relevant ke untuk you sekarang?"
    ),
    "few_hours": (
        "Just checking in 👍\n"
        "Kalau you masih busy, nanti bila free boleh sambung — saya boleh bantu suggest setup sesuai."
    ),
    "next_day": (
        "Hi 😊 semalam kita ada discuss pasal AI agent.\n"
        "Nak check kalau you masih consider nak automate reply customer tu?"
    ),
}

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "follow_up_scheduler",
        "description": (
            "Schedule a follow-up message to re-engage an inactive lead, "
            "or retrieve the lead's stored contact data for delivery. "
            "The backend policy usually decides follow-up scheduling. "
            "Call get_lead_data to check what info is already stored for this lead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["schedule", "get_lead_data", "cancel"],
                    "description": (
                        "schedule: queue a follow-up message at the given interval. "
                        "get_lead_data: return stored facts and contact info for this lead. "
                        "cancel: cancel all pending follow-ups (e.g. user re-engaged)."
                    ),
                },
                "follow_up_type": {
                    "type": "string",
                    "enum": ["30min", "few_hours", "next_day"],
                    "description": (
                        "When to send: "
                        "30min = 30 minutes after now (first nudge), "
                        "few_hours = ~3 hours (second nudge), "
                        "next_day = ~22 hours (final nudge)."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Custom follow-up message to send. "
                        "If omitted, a default message for the follow_up_type is used. "
                        "Keep it short, WhatsApp-style, and natural."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


async def execute(
    action: str,
    user_id: UUID,
    conversation_id: Optional[UUID] = None,
    external_id: Optional[str] = None,
    channel: Optional[str] = "whatsapp",
    follow_up_type: Optional[str] = None,
    message: Optional[str] = None,
) -> dict:
    if action == "get_lead_data":
        data = await Database.get_lead_data(user_id)
        if not data:
            return {"error": "No lead data found for this user."}
        facts = normalize_facts(data.get("facts", {}))
        return {
            "external_id": data.get("external_id"),
            "channel": data.get("channel"),
            "display_name": data.get("display_name"),
            "facts": facts,
            "score": data.get("score", 0),
            "score_label": data.get("score_label", "unqualified"),
            "lead_status": facts.get("lead_status", "active"),
            "qualification_stage": facts.get("qualification_stage", "new"),
            "follow_up_stage": normalize_follow_up_stage(facts.get("follow_up_stage")),
            "follow_up_count": facts.get("follow_up_count", 0),
            "last_message_at": str(data.get("last_message_at", "")),
        }

    if action == "cancel":
        await Database.cancel_pending_followups(user_id)
        return {"cancelled": True}

    if action == "schedule":
        if not follow_up_type:
            return {"error": "follow_up_type is required for schedule action."}
        if not external_id:
            return {"error": "Cannot schedule follow-up: no external_id (contact address) available."}

        delay = FOLLOW_UP_DELAYS.get(follow_up_type, timedelta(minutes=30))
        scheduled_at = datetime.now(timezone.utc) + delay
        text = message or DEFAULT_MESSAGES.get(follow_up_type, "")

        followup = await Database.schedule_followup(
            user_id=user_id,
            conversation_id=conversation_id,
            external_id=external_id,
            channel=channel or "whatsapp",
            follow_up_type=follow_up_type,
            message=text,
            scheduled_at=scheduled_at,
        )
        return {
            "scheduled": True,
            "follow_up_id": str(followup["id"]),
            "type": follow_up_type,
            "follow_up_stage": FOLLOW_UP_TYPE_TO_STAGE.get(follow_up_type),
            "send_at": scheduled_at.isoformat(),
            "message_preview": text[:80],
        }

    return {"error": f"Unknown action: {action}"}
