"""
user_profile_manager tool
Manages user profiles and structured qualification facts.
"""
from uuid import UUID

from database.dal import Database
from lead_state import normalize_facts


TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "user_profile_manager",
        "description": (
            "Manage the user's qualification profile. "
            "ALWAYS call get_facts at the start of EVERY turn before replying. "
            "Call update_facts immediately whenever the user reveals any new info. "
            "Track normalized lead-state facts such as business_type, pain_point, "
            "message_volume_band, current_process, service_interest, budget_band "
            "(low/mid/high), timeline_band (immediate/soon/exploring), "
            "contact_name, and contact_phone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_facts", "update_facts", "get_profile"],
                    "description": "The operation to perform.",
                },
                "facts": {
                    "type": "object",
                    "description": (
                        "Structured facts to store (required for update_facts). "
                        "Only include keys that have new or updated values. "
                        "Normalized values are preferred: budget_band values 'low', 'mid', 'high'; "
                        "timeline_band values 'immediate', 'soon', 'exploring'; "
                        "intent_stage values 'low', 'medium', 'high'; "
                        "qualification_stage values 'new', 'qualifying', 'interested', 'hot', 'handoff', 'closed'; "
                        "lead_status values 'active', 'paused', 'handed_off', 'closed', 'disqualified'. "
                        "Legacy values are normalized automatically."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
        },
    },
}


async def execute(action: str, user_id: UUID, facts: dict = None) -> dict:
    if action == "get_facts":
        profile = await Database.get_or_create_profile(user_id)
        return {"facts": normalize_facts(profile.get("facts", {})), "score": profile.get("score", 0)}

    if action == "get_profile":
        profile = await Database.get_or_create_profile(user_id)
        profile["facts"] = normalize_facts(profile.get("facts", {}))
        return profile

    if action == "update_facts":
        if not facts:
            return {"error": "No facts provided for update_facts action."}
        facts = normalize_facts(facts, partial=True)
        # Update display name if contact_name is being set
        if "contact_name" in facts:
            await Database.update_user_display_name(user_id, facts["contact_name"])
        profile = await Database.update_facts(user_id, facts, changed_by="agent")
        return {
            "success": True,
            "updated_facts": normalize_facts(profile.get("facts", {})),
            "current_score": profile.get("score", 0),
        }

    return {"error": f"Unknown action: {action}"}
