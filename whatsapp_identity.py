from __future__ import annotations

from typing import Optional


_WA_SUFFIXES = (
    "@s.whatsapp.net",
    "@c.us",
    "@lid",
)


def normalize_whatsapp_id(value: Optional[str]) -> Optional[str]:
    """Return a canonical WhatsApp identity key for persistence and lookups."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    lowered = text.lower()
    for suffix in _WA_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def normalize_external_id(channel: str, external_id: Optional[str]) -> Optional[str]:
    """Normalize persisted external IDs by channel."""
    if external_id is None:
        return None
    if channel == "whatsapp":
        return normalize_whatsapp_id(external_id)
    text = str(external_id).strip()
    return text or None
