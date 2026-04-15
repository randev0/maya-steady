from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from lead_state import FOLLOW_UP_TYPE_TO_STAGE, LeadState


OPT_OUT_TERMS = (
    "stop",
    "tak nak",
    "xnak",
    "no thanks",
    "not interested",
    "jangan mesej",
    "don't message",
    "unsubscribe",
)

HUMAN_REQUEST_TERMS = (
    "human",
    "orang",
    "call me",
    "speak to someone",
    "agent manusia",
)

BUYING_INTENT_TERMS = (
    "demo",
    "setup",
    "bayar",
    "payment",
    "proceed",
    "sign up",
    "subscribe",
    "nak try",
    "nak beli",
    "nak proceed",
)

FRUSTRATION_TERMS = (
    "confused",
    "tak faham",
    "frustrated",
    "annoying",
    "useless",
)

WINDING_DOWN_TERMS = {
    "ok",
    "ok ok",
    "okay",
    "nanti",
    "tengok dulu",
    "maybe",
    "thanks",
    "thank you",
}

GREETING_TERMS = {"hi", "hello", "hey", "salam", "assalamualaikum"}

QUESTION_ORDER = [
    "business_type",
    "pain_point",
    "current_process",
    "message_volume_band",
    "service_interest",
    "timeline_band",
    "budget_band",
]

QUESTION_PROMPTS = {
    "business_type": "Ask what type of business they run.",
    "pain_point": "Ask what part of handling leads or replies feels hardest right now.",
    "current_process": "Ask how they currently handle customer replies or lead follow-up.",
    "message_volume_band": "Ask roughly how many customer messages or leads come in per day.",
    "service_interest": "Ask whether they want a simple, standard, or full setup.",
    "timeline_band": "Ask whether they want to start immediately, soon, or are still exploring.",
    "budget_band": "Ask whether they want to start simple, mid-range, or invest in a higher-tier setup.",
}

QUALIFICATION_REQUIRED_FIELDS = ("business_type", "pain_point", "current_process", "message_volume_band")
HOT_LEAD_REQUIRED_FIELDS = ("business_type", "pain_point", "service_interest")


@dataclass
class PolicyDecision:
    state_updates: dict = field(default_factory=dict)
    handoff_required: bool = False
    handoff_reason: Optional[str] = None
    handoff_notes: Optional[str] = None
    opt_out: bool = False
    follow_up_type: Optional[str] = None
    next_field: Optional[str] = None
    next_prompt: Optional[str] = None


def _contains_any(message: str, terms: tuple[str, ...] | set[str]) -> bool:
    text = message.lower()
    return any(term in text for term in terms)


def _has_all_fields(state: LeadState, field_names: tuple[str, ...]) -> bool:
    return all(getattr(state, field_name) for field_name in field_names)


def _detect_intent_stage(state: LeadState, message: str) -> str:
    if _contains_any(message, BUYING_INTENT_TERMS) or state.score >= 4:
        return "high"
    if state.service_interest or state.budget_band or state.timeline_band or "price" in message.lower() or "harga" in message.lower():
        return "medium"
    return "low"


def _detect_qualification_stage(state: LeadState, intent_stage: str) -> str:
    if state.opt_out:
        return "closed"
    if state.human_handoff_requested:
        return "handoff"
    if intent_stage == "high" and _has_all_fields(state, HOT_LEAD_REQUIRED_FIELDS):
        return "hot"
    if state.service_interest or state.budget_band or state.timeline_band:
        return "interested"
    if any(getattr(state, field_name) for field_name in QUALIFICATION_REQUIRED_FIELDS):
        return "qualifying"
    return "new"


def _detect_lead_status(state: LeadState, qualification_stage: str) -> str:
    if state.opt_out:
        return "disqualified"
    if state.lead_status == "paused":
        return "paused"
    if qualification_stage == "handoff":
        return "handed_off"
    if qualification_stage == "closed":
        return "closed"
    return "active"


def _next_missing_field(state: LeadState) -> Optional[str]:
    if state.opt_out or state.human_handoff_requested or state.lead_status in {"handed_off", "disqualified", "closed"}:
        return None
    for field_name in QUESTION_ORDER:
        if not getattr(state, field_name):
            return field_name
    return None


def _should_handoff(state: LeadState, message: str, intent_stage: str) -> tuple[bool, Optional[str], Optional[str]]:
    text = message.lower()
    if state.opt_out or state.lead_status in {"disqualified", "closed"}:
        return False, None, None
    if state.human_handoff_requested or state.lead_status == "handed_off":
        return True, state.handoff_reason or "user_request", "A human handoff is already active for this lead."
    if _contains_any(text, HUMAN_REQUEST_TERMS):
        return True, "user_request", "Lead explicitly asked to speak with a person."
    if _contains_any(text, FRUSTRATION_TERMS):
        return True, "frustration", "Lead sounds confused or frustrated and needs human follow-up."
    if _contains_any(text, BUYING_INTENT_TERMS):
        return True, "user_request", "Lead is asking for demo, setup, payment, or a serious buying step."
    if intent_stage == "high" and _has_all_fields(state, HOT_LEAD_REQUIRED_FIELDS) and (state.budget_band or state.timeline_band):
        return True, "high_score", "Lead has high intent and enough qualification info for human follow-up."
    return False, None, None


def _should_schedule_followup(state: LeadState, message: str) -> Optional[str]:
    text = message.strip().lower()
    if state.opt_out or state.human_handoff_requested or state.lead_status in {"handed_off", "disqualified", "closed"}:
        return None
    if state.follow_up_count >= 3:
        return None
    if text in GREETING_TERMS:
        return None
    if text in WINDING_DOWN_TERMS or (len(text.split()) <= 2 and "?" not in text):
        return ["30min", "few_hours", "next_day"][state.follow_up_count]
    return None


def reconcile_state(state: LeadState, message: str) -> PolicyDecision:
    decision = PolicyDecision()
    text = message.lower()

    if _contains_any(text, OPT_OUT_TERMS):
        state.opt_out = True
        state.intent_stage = "low"
        state.qualification_stage = "closed"
        state.lead_status = "disqualified"
        state.human_handoff_requested = False
        state.handoff_reason = None
        state.follow_up_stage = None
        state.next_follow_up_at = None
        decision.opt_out = True
        decision.state_updates = {
            "opt_out": True,
            "intent_stage": state.intent_stage,
            "qualification_stage": state.qualification_stage,
            "lead_status": state.lead_status,
            "human_handoff_requested": state.human_handoff_requested,
            "handoff_reason": state.handoff_reason,
            "follow_up_stage": state.follow_up_stage,
            "next_follow_up_at": state.next_follow_up_at,
        }
        return decision

    state.intent_stage = _detect_intent_stage(state, message)
    handoff_required, handoff_reason, handoff_notes = _should_handoff(state, message, state.intent_stage)
    state.human_handoff_requested = handoff_required
    state.handoff_reason = handoff_reason
    state.qualification_stage = _detect_qualification_stage(state, state.intent_stage)
    state.lead_status = _detect_lead_status(state, state.qualification_stage)

    decision.handoff_required = handoff_required
    decision.handoff_reason = handoff_reason
    decision.handoff_notes = handoff_notes
    decision.follow_up_type = None if handoff_required else _should_schedule_followup(state, message)
    decision.next_field = _next_missing_field(state)
    decision.next_prompt = QUESTION_PROMPTS.get(decision.next_field)
    decision.state_updates = {
        "intent_stage": state.intent_stage,
        "qualification_stage": state.qualification_stage,
        "lead_status": state.lead_status,
        "human_handoff_requested": state.human_handoff_requested,
        "handoff_reason": state.handoff_reason,
        "opt_out": state.opt_out,
    }
    return decision


def build_policy_decision(state: LeadState, message: str) -> PolicyDecision:
    return reconcile_state(state, message)


def apply_follow_up_update(state: LeadState, follow_up_type: str, send_at: Optional[str]) -> dict:
    next_count = state.follow_up_count + 1
    return {
        "follow_up_stage": FOLLOW_UP_TYPE_TO_STAGE.get(follow_up_type),
        "follow_up_count": next_count,
        "next_follow_up_at": send_at,
    }


def build_policy_context(state: LeadState, decision: PolicyDecision) -> str:
    known = [name for name, value in state.known_fields().items() if value]
    missing = [name for name in QUESTION_ORDER if not getattr(state, name)]
    lines = [
        "Backend policy has already decided business-critical behavior for this turn.",
        f"Intent stage: {state.intent_stage}",
        f"Qualification stage: {state.qualification_stage}",
        f"Lead status: {state.lead_status}",
        f"Known fields: {', '.join(known) if known else 'none'}",
        f"Missing fields: {', '.join(missing) if missing else 'none'}",
    ]
    if decision.next_field:
        lines.append(f"Your next question focus is: {decision.next_field}. {decision.next_prompt}")
    else:
        lines.append("Do not ask a new qualification question unless the user asks for clarification.")
    if known:
        lines.append(f"Do not ask again about: {', '.join(known)}")
    if state.opt_out:
        lines.append("The user is opted out. Acknowledge and stop.")
    if state.human_handoff_requested:
        lines.append("A human handoff is already required or active. Do not improvise feature or payment details.")
    return "\n".join(lines)
