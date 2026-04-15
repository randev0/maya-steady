from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


LEAD_STATE_FIELDS = (
    "business_type",
    "pain_point",
    "message_volume_band",
    "current_process",
    "service_interest",
    "budget_band",
    "timeline_band",
    "intent_stage",
    "qualification_stage",
    "lead_status",
    "human_handoff_requested",
    "handoff_reason",
    "follow_up_stage",
    "follow_up_count",
    "next_follow_up_at",
    "opt_out",
)

BUDGET_BAND_MAP = {
    "below_rm2k": "low",
    "low": "low",
    "small": "low",
    "starter": "low",
    "rm2k_rm5k": "mid",
    "mid": "mid",
    "medium": "mid",
    "rm5k_rm10k": "high",
    "rm10k_plus": "high",
    "high": "high",
    "premium": "high",
}

TIMELINE_BAND_MAP = {
    "immediately": "immediate",
    "within_1_month": "immediate",
    "immediate": "immediate",
    "asap": "immediate",
    "urgent": "immediate",
    "1_3_months": "soon",
    "this_month": "soon",
    "3_6_months": "exploring",
    "soon": "soon",
    "flexible": "exploring",
    "exploring": "exploring",
    "later": "exploring",
    "researching": "exploring",
}

INTENT_STAGE_MAP = {
    "cold": "low",
    "low": "low",
    "new": "low",
    "warm": "medium",
    "medium": "medium",
    "interested": "medium",
    "qualified": "high",
    "high": "high",
    "hot": "high",
    "hot_lead": "high",
}

QUALIFICATION_STAGE_MAP = {
    "new": "new",
    "cold": "new",
    "discovery": "qualifying",
    "qualifying": "qualifying",
    "warming": "qualifying",
    "interested": "interested",
    "warm": "interested",
    "qualified": "interested",
    "hot": "hot",
    "sales_ready": "hot",
    "handoff": "handoff",
    "escalated": "handoff",
    "closed": "closed",
    "won": "closed",
    "lost": "closed",
}

LEAD_STATUS_MAP = {
    "active": "active",
    "open": "active",
    "new": "active",
    "paused": "paused",
    "parked": "paused",
    "on_hold": "paused",
    "handed_off": "handed_off",
    "handoff": "handed_off",
    "escalated": "handed_off",
    "closed": "closed",
    "completed": "closed",
    "disqualified": "disqualified",
    "opt_out": "disqualified",
    "opted_out": "disqualified",
    "do_not_contact": "disqualified",
}

INTENT_STAGE_VALUES = {"low", "medium", "high"}
QUALIFICATION_STAGE_VALUES = {"new", "qualifying", "interested", "hot", "handoff", "closed"}
LEAD_STATUS_VALUES = {"active", "paused", "handed_off", "closed", "disqualified"}
FOLLOW_UP_STAGE_VALUES = {"initial", "nudge", "final"}

FOLLOW_UP_TYPE_TO_STAGE = {
    "30min": "initial",
    "few_hours": "nudge",
    "next_day": "final",
}


def normalize_budget_band(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return BUDGET_BAND_MAP.get(str(value).strip().lower())


def normalize_timeline_band(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return TIMELINE_BAND_MAP.get(str(value).strip().lower())


def normalize_message_volume_band(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"low", "medium", "high"}:
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        count = int(digits)
        if count >= 100:
            return "high"
        if count >= 20:
            return "medium"
        return "low"
    return text or None


def _normalize_enum(value: Optional[str], mapping: dict[str, str], allowed: set[str]) -> Optional[str]:
    if not value:
        return None
    normalized = mapping.get(str(value).strip().lower())
    if normalized in allowed:
        return normalized
    return None


def normalize_intent_stage(value: Optional[str]) -> Optional[str]:
    return _normalize_enum(value, INTENT_STAGE_MAP, INTENT_STAGE_VALUES)


def normalize_qualification_stage(value: Optional[str]) -> Optional[str]:
    return _normalize_enum(value, QUALIFICATION_STAGE_MAP, QUALIFICATION_STAGE_VALUES)


def normalize_lead_status(value: Optional[str]) -> Optional[str]:
    return _normalize_enum(value, LEAD_STATUS_MAP, LEAD_STATUS_VALUES)


def normalize_follow_up_stage(value: Optional[str], follow_up_type: Optional[str] = None) -> Optional[str]:
    stage = None
    if value:
        stage = str(value).strip().lower()
    if stage in FOLLOW_UP_STAGE_VALUES:
        return stage
    if follow_up_type:
        return FOLLOW_UP_TYPE_TO_STAGE.get(str(follow_up_type).strip().lower())
    return None


def normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def normalize_facts(facts: Optional[dict], partial: bool = False) -> dict:
    data = dict(facts or {})

    if "message_volume_band" in data:
        data["message_volume_band"] = normalize_message_volume_band(data.get("message_volume_band"))
    elif not partial:
        data["message_volume_band"] = normalize_message_volume_band(data.get("message_volume"))
    elif "message_volume" in data:
        data["message_volume_band"] = normalize_message_volume_band(data.get("message_volume"))

    if "current_process" not in data and not partial:
        data["current_process"] = data.get("current_tools")
    elif "current_process" not in data and "current_tools" in data:
        data["current_process"] = data.get("current_tools")

    if any(key in data for key in ("budget_band", "budget_range")):
        data["budget_band"] = normalize_budget_band(data.get("budget_band") or data.get("budget_range"))
    elif not partial:
        data["budget_band"] = None

    if any(key in data for key in ("timeline_band", "timeline")):
        data["timeline_band"] = normalize_timeline_band(data.get("timeline_band") or data.get("timeline"))
    elif not partial:
        data["timeline_band"] = None

    if "intent_stage" in data:
        data["intent_stage"] = normalize_intent_stage(data.get("intent_stage"))
    elif not partial:
        data["intent_stage"] = normalize_intent_stage(data.get("intent_stage"))

    if "qualification_stage" in data:
        data["qualification_stage"] = normalize_qualification_stage(data.get("qualification_stage"))
    elif not partial:
        data["qualification_stage"] = normalize_qualification_stage(data.get("qualification_stage"))

    if "lead_status" in data:
        data["lead_status"] = normalize_lead_status(data.get("lead_status"))
    elif not partial:
        data["lead_status"] = normalize_lead_status(data.get("lead_status"))

    if "follow_up_stage" in data or "follow_up_type" in data:
        data["follow_up_stage"] = normalize_follow_up_stage(
            data.get("follow_up_stage"),
            data.get("follow_up_type"),
        )
    elif not partial:
        data["follow_up_stage"] = normalize_follow_up_stage(data.get("follow_up_stage"))

    if "follow_up_count" in data:
        try:
            data["follow_up_count"] = max(int(data.get("follow_up_count") or 0), 0)
        except (TypeError, ValueError):
            data["follow_up_count"] = 0
    elif not partial:
        data["follow_up_count"] = 0

    if "human_handoff_requested" in data:
        data["human_handoff_requested"] = normalize_bool(data.get("human_handoff_requested"))
    elif not partial:
        data["human_handoff_requested"] = normalize_bool(data.get("human_handoff_requested"))

    if "opt_out" in data:
        data["opt_out"] = normalize_bool(data.get("opt_out"))
    elif not partial:
        data["opt_out"] = normalize_bool(data.get("opt_out"))

    return data


def normalize_lead_state_update(facts: Optional[dict]) -> dict:
    normalized = normalize_facts(facts, partial=True)
    return {key: value for key, value in normalized.items() if key in LEAD_STATE_FIELDS}


@dataclass
class LeadState:
    business_type: Optional[str] = None
    pain_point: Optional[str] = None
    message_volume_band: Optional[str] = None
    current_process: Optional[str] = None
    service_interest: Optional[str] = None
    budget_band: Optional[str] = None
    timeline_band: Optional[str] = None
    intent_stage: str = "low"
    qualification_stage: str = "new"
    lead_status: str = "active"
    human_handoff_requested: bool = False
    handoff_reason: Optional[str] = None
    follow_up_stage: Optional[str] = None
    follow_up_count: int = 0
    next_follow_up_at: Optional[str] = None
    opt_out: bool = False
    score: int = 0
    score_label: str = "unqualified"
    contact_name: Optional[str] = None
    display_name: Optional[str] = None

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "LeadState":
        facts = normalize_facts(snapshot.get("facts"))
        pending_followup = snapshot.get("pending_followup") or {}
        open_handoff = snapshot.get("open_handoff") or {}

        follow_up_stage = normalize_follow_up_stage(
            facts.get("follow_up_stage"),
            pending_followup.get("follow_up_type"),
        )
        next_follow_up_at = facts.get("next_follow_up_at")
        if not next_follow_up_at and pending_followup.get("scheduled_at"):
            next_follow_up_at = pending_followup["scheduled_at"].isoformat()

        lead_status = normalize_lead_status(facts.get("lead_status"))
        if snapshot.get("maya_paused"):
            lead_status = "paused"
        elif open_handoff or snapshot.get("conversation_status") == "handoff" or facts.get("human_handoff_requested"):
            lead_status = "handed_off"
        elif facts.get("opt_out"):
            lead_status = "disqualified"
        else:
            lead_status = lead_status or "active"

        qualification_stage = normalize_qualification_stage(facts.get("qualification_stage"))
        if lead_status == "handed_off":
            qualification_stage = "handoff"
        elif lead_status in {"closed", "disqualified"}:
            qualification_stage = "closed"
        else:
            qualification_stage = qualification_stage or "new"

        handoff_reason = facts.get("handoff_reason") or open_handoff.get("reason")

        return cls(
            business_type=facts.get("business_type"),
            pain_point=facts.get("pain_point"),
            message_volume_band=facts.get("message_volume_band"),
            current_process=facts.get("current_process"),
            service_interest=facts.get("service_interest"),
            budget_band=facts.get("budget_band"),
            timeline_band=facts.get("timeline_band"),
            intent_stage=normalize_intent_stage(facts.get("intent_stage")) or "low",
            qualification_stage=qualification_stage,
            lead_status=lead_status,
            human_handoff_requested=bool(open_handoff) or facts.get("human_handoff_requested", False),
            handoff_reason=handoff_reason,
            follow_up_stage=follow_up_stage,
            follow_up_count=max(int(facts.get("follow_up_count") or 0), 0),
            next_follow_up_at=next_follow_up_at,
            opt_out=facts.get("opt_out", False),
            score=int(snapshot.get("score") or 0),
            score_label=snapshot.get("score_label") or "unqualified",
            contact_name=facts.get("contact_name"),
            display_name=snapshot.get("display_name"),
        )

    def known_fields(self) -> dict:
        return {
            "business_type": self.business_type,
            "pain_point": self.pain_point,
            "message_volume_band": self.message_volume_band,
            "current_process": self.current_process,
            "service_interest": self.service_interest,
            "budget_band": self.budget_band,
            "timeline_band": self.timeline_band,
        }

    def to_facts_update(self) -> dict:
        return {
            "business_type": self.business_type,
            "pain_point": self.pain_point,
            "message_volume_band": self.message_volume_band,
            "current_process": self.current_process,
            "service_interest": self.service_interest,
            "budget_band": self.budget_band,
            "timeline_band": self.timeline_band,
            "intent_stage": self.intent_stage,
            "qualification_stage": self.qualification_stage,
            "lead_status": self.lead_status,
            "human_handoff_requested": self.human_handoff_requested,
            "handoff_reason": self.handoff_reason,
            "follow_up_stage": self.follow_up_stage,
            "follow_up_count": self.follow_up_count,
            "next_follow_up_at": self.next_follow_up_at,
            "opt_out": self.opt_out,
        }


def isoformat_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
