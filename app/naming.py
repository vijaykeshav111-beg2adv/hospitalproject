"""Small display-name helpers shared by the API, documents and notification layer.

Doctor records store the full name **with** the "Dr" honorific ("Dr Arjun Mehra"),
so every place that prints a doctor must avoid adding a second "Dr".
"""
from __future__ import annotations

import re

_HONORIFIC = re.compile(r"^\s*(dr|doctor)\.?\s+", re.IGNORECASE)


def doctor_label(name: str | None, *, with_prefix: bool = True) -> str:
    """Return a doctor display name with exactly one honorific."""
    raw = (name or "").strip()
    if not raw:
        return "our doctor"
    bare = _HONORIFIC.sub("", raw).strip()
    if not bare:
        return raw
    return f"Dr {bare}" if with_prefix else bare


def person_label(name: str | None, fallback: str = "the patient") -> str:
    raw = " ".join((name or "").split())
    return raw or fallback
