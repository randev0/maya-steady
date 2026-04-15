"""
skill_retriever tool
Hermes-inspired skill memory: retrieve proven tactics and record conversation outcomes.
"""
from uuid import UUID
from typing import Optional
from database.dal import Database

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "skill_retriever",
        "description": (
            "Retrieve proven conversation tactics for the current situation, or record a conversation outcome. "
            "Call get_relevant_skills every turn AFTER get_facts — it returns tactics that have worked "
            "in similar past conversations. Use them to guide your reply. "
            "Call record_outcome once when a conversation ends (handoff, dropped, or exploring)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_relevant_skills", "record_outcome"],
                    "description": "Operation to perform.",
                },
                "situation_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tags describing the current situation (for get_relevant_skills). "
                        "Choose all that apply from: "
                        "product_seller, service_seller, "
                        "passive_user, engaged_user, short_reply, "
                        "high_volume, low_volume, "
                        "no_name_yet, gave_name, "
                        "no_business_yet, business_known, "
                        "no_pain_yet, pain_identified, "
                        "budget_sensitive, urgent_timeline, flexible_timeline, "
                        "first_message, returning_user"
                    ),
                },
                "outcome": {
                    "type": "string",
                    "enum": ["converted", "dropped", "exploring"],
                    "description": "Outcome of the conversation (for record_outcome only).",
                },
                "fields_collected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of fact fields successfully collected before conversation ended "
                        "(for record_outcome only). "
                        "Example: ['contact_name', 'business_type', 'pain_point']"
                    ),
                },
                "total_turns": {
                    "type": "integer",
                    "description": "Total number of message exchanges in this conversation (for record_outcome).",
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
    situation_tags: Optional[list] = None,
    outcome: Optional[str] = None,
    fields_collected: Optional[list] = None,
    total_turns: int = 0,
) -> dict:
    if action == "get_relevant_skills":
        tags = situation_tags or []
        skills = await Database.get_relevant_skills(tags, limit=3)
        if not skills:
            return {"skills": [], "note": "No skills yet — building from experience."}
        for skill in skills:
            await Database.increment_skill_use(skill["id"])
        return {
            "_skill_ids": [str(s["id"]) for s in skills],
            "skills": [
                {
                    "situation": s["situation_summary"],
                    "approach": s["approach"],
                    "worked": s["outcome"],
                    "success_rate": f"{round(s['success_count'] / max(s['use_count'], 1) * 100)}%",
                }
                for s in skills
            ]
        }

    if action == "record_outcome":
        if not conversation_id:
            return {"error": "conversation_id required for record_outcome"}
        facts = []
        if fields_collected:
            facts = fields_collected
        # Identify what was missing (drop_off_field)
        required = ["contact_name", "business_type", "pain_point"]
        drop_off = next((f for f in required if f not in facts), None)
        await Database.upsert_conversation_outcome(
            conversation_id=conversation_id,
            outcome=outcome or "dropped",
            fields_collected=facts,
            drop_off_field=drop_off,
            total_turns=total_turns,
            converted_at_turn=total_turns if outcome == "converted" else None,
        )
        return {"recorded": True, "outcome": outcome, "drop_off_field": drop_off}

    return {"error": f"Unknown action: {action}"}
