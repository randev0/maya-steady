"""
business_workflow_tool
Calculates lead score and exports qualified leads.
"""
import httpx
import structlog
from uuid import UUID

from database.dal import Database
from config import settings
from lead_state import normalize_facts

log = structlog.get_logger()

_BUDGET_SCORE_MAP = {
    "low": 0,
    "mid": 1,
    "high": 2,
}

_TIMELINE_SCORE_MAP = {
    "immediate": 2,
    "soon": 1,
    "exploring": 0,
}

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "business_workflow_tool",
        "description": (
            "Calculate the lead qualification score using normalized lead-state bands, "
            "and export leads when needed. The backend may also trigger scoring directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["calculate_score", "export_lead"],
                    "description": "The operation to perform.",
                },
            },
            "required": ["action"],
        },
    },
}


def _score_from_facts(facts: dict) -> tuple[int, str]:
    """
    Scoring rules (max 5 points):
      Budget ≥ RM5k  → +2
      Timeline ≤ 1mo → +2
      Clear service  → +1
    """
    score = 0

    normalized_facts = normalize_facts(facts)

    budget = normalized_facts.get("budget_band")
    score += _BUDGET_SCORE_MAP.get(budget or "", 0)

    timeline = normalized_facts.get("timeline_band")
    score += _TIMELINE_SCORE_MAP.get(timeline or "", 0)

    service = normalized_facts.get("service_interest", "")
    if service and len(service.strip()) > 2:
        score += 1

    if score >= 4:
        label = "qualified"
    elif score >= 2:
        label = "warm"
    elif score >= 1:
        label = "low_priority"
    else:
        label = "unqualified"

    return score, label


async def execute(action: str, user_id: UUID) -> dict:
    if action == "calculate_score":
        profile = await Database.get_or_create_profile(user_id)
        facts = normalize_facts(profile.get("facts", {}))
        score, label = _score_from_facts(facts)
        await Database.update_lead_score(user_id, score)
        return {
            "score": score,
            "label": label,
            "breakdown": {
                "budget_band": facts.get("budget_band", "not provided"),
                "timeline_band": facts.get("timeline_band", "not provided"),
                "service": facts.get("service_interest", "not provided"),
            },
            "should_handoff": score >= 4,
        }

    if action == "export_lead":
        if not settings.lead_export_webhook_url:
            return {"skipped": True, "reason": "No export webhook configured."}
        profile = await Database.get_or_create_profile(user_id)
        user = await Database.get_user_by_id(user_id)
        if not user:
            return {
                "exported": False,
                "error": f"User not found for internal id {user_id}",
            }
        payload = {
            "user_id": str(user_id),
            "channel": user.get("channel"),
            "display_name": user.get("display_name"),
            "score": profile.get("score"),
            "score_label": profile.get("score_label"),
            "facts": normalize_facts(profile.get("facts", {})),
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(settings.lead_export_webhook_url, json=payload)
            return {"exported": True, "status_code": resp.status_code}
        except Exception as exc:
            log.warning("lead_export_failed", error=str(exc))
            return {"exported": False, "error": str(exc)}

    return {"error": f"Unknown action: {action}"}
