"""Groq-backed AI front desk using the hospital's 14 controlled backend tools."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# The `groq` package is an OPTIONAL dependency: the hospital front desk must
# keep working when it is not installed. Import it lazily / defensively so a
# missing package can never crash the API with an ImportError.
try:  # pragma: no cover - depends on the environment
    from groq import Groq
except Exception:  # noqa: BLE001 - ImportError or a broken install
    Groq = None  # type: ignore[assignment]

from .. import models
from ..config import settings
from . import ai_tools
from .ai_tools import ToolContext

log = logging.getLogger("vvh.ai.groq")

SYSTEM_PROMPT = """You are the front-desk AI assistant for Vijay Vargiya Group of Hospitals.

You help only with hospital operations: appointment discovery and booking, patient profile and appointment history, doctor/specialty availability, invoices, payment status, and notifications.

You MUST NOT diagnose, prescribe medicines, recommend treatment, or provide medical advice. If the patient asks for medical advice or describes an emergency, do not use booking tools to delay care. Tell them to seek urgent medical care and escalate when the backend safety layer requires it.

Use the provided hospital tools for factual data. Never invent doctors, specialties, slots, prices, invoices, payments, or appointment status.

The authenticated patient context is authoritative. Never try to access another patient's records.

When booking: first identify the patient, find appropriate specialty/doctor/availability/slots, hold the selected slot, then book it. If a booking fails, explain the actual backend result and offer available alternatives.

Keep responses concise and clear. Ask a focused follow-up question when required information is missing.
"""

# The 18 controlled hospital functions exposed to Groq (same order as
# ai_tools.TOOLS, enforced by tests/groq_tools_check.py).
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_patient_profile", "description": "Get the authenticated patient's profile.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_patient_summary", "description": "Get the patient's full AI context: profile, concerns, visits, prescriptions, files and outstanding balance.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_patient_concerns", "description": "List the authenticated patient's recorded concerns and their history.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}, "limit": {"type": "integer", "default": 10}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_patient_appointments", "description": "Get the authenticated patient's past or upcoming appointments.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}, "upcoming_only": {"type": "boolean", "default": False}, "limit": {"type": "integer", "default": 10}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_patient_prescriptions", "description": "Get the authenticated patient's recent prescriptions and their medicines.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}, "limit": {"type": "integer", "default": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_patient_files", "description": "Get the authenticated patient's lab reports, scans and other medical files.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}, "category": {"type": ["string", "null"]}, "limit": {"type": "integer", "default": 10}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_specialties", "description": "Search active hospital specialties by name, description, or mapped concern keywords.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 6}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_doctors", "description": "Search available doctors by specialty or name.", "parameters": {"type": "object", "properties": {"specialty_id": {"type": ["integer", "null"]}, "query": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 6}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_doctor_availability", "description": "Get a doctor's working days, hours, breaks, leave and holidays.", "parameters": {"type": "object", "properties": {"doctor_id": {"type": "integer"}, "days_ahead": {"type": "integer", "default": 14}}, "required": ["doctor_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_available_slots", "description": "Find live bookable slots for a doctor or specialty.", "parameters": {"type": "object", "properties": {"doctor_id": {"type": ["integer", "null"]}, "specialty_id": {"type": ["integer", "null"]}, "days_ahead": {"type": "integer", "default": 14}, "limit": {"type": "integer", "default": 10}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "hold_slot", "description": "Temporarily hold a selected slot while the patient confirms booking.", "parameters": {"type": "object", "properties": {"slot_id": {"type": "integer"}, "minutes": {"type": "integer", "default": 10}}, "required": ["slot_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "release_slot", "description": "Release a slot held by this booking conversation.", "parameters": {"type": "object", "properties": {"slot_id": {"type": "integer"}}, "required": ["slot_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "book_appointment", "description": "Book a held or available slot for the authenticated patient.", "parameters": {"type": "object", "properties": {"slot_id": {"type": "integer"}, "patient_id": {"type": ["integer", "null"]}, "reason": {"type": ["string", "null"]}, "hold_token": {"type": ["string", "null"]}, "notify_patient": {"type": "boolean", "default": True}}, "required": ["slot_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cancel_appointment", "description": "Cancel one of the authenticated patient's appointments.", "parameters": {"type": "object", "properties": {"appointment_id": {"type": "integer"}, "reason": {"type": ["string", "null"]}}, "required": ["appointment_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "reschedule_appointment", "description": "Move one of the authenticated patient's appointments to a new available slot.", "parameters": {"type": "object", "properties": {"appointment_id": {"type": "integer"}, "new_slot_id": {"type": "integer"}, "reason": {"type": ["string", "null"]}}, "required": ["appointment_id", "new_slot_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_invoice", "description": "Get the authenticated patient's invoice, totals, paid amount and balance.", "parameters": {"type": "object", "properties": {"invoice_id": {"type": ["integer", "null"]}, "patient_id": {"type": ["integer", "null"]}, "appointment_id": {"type": ["integer", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_payment_status", "description": "Get the authenticated patient's payment history and outstanding balance.", "parameters": {"type": "object", "properties": {"patient_id": {"type": ["integer", "null"]}, "invoice_id": {"type": ["integer", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "send_notification", "description": "Send an allowed hospital notification to the authenticated patient.", "parameters": {"type": "object", "properties": {"template": {"type": "string"}, "message": {"type": ["string", "null"]}, "channel": {"type": "string", "enum": ["WHATSAPP", "EMAIL", "IN_APP"]}, "patient_id": {"type": ["integer", "null"]}}, "required": ["template", "channel"], "additionalProperties": False}}},
]

TOOL_NAMES = {x["function"]["name"] for x in TOOL_DEFINITIONS}


def is_available() -> bool:
    """True when the Groq client and an API key are both usable."""
    return bool(Groq is not None and settings.groq_api_key)


def _groq_request(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if Groq is None:
        raise RuntimeError("The 'groq' package is not installed (pip install groq)")
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    try:
        client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.groq_timeout_seconds,
        )

        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=settings.groq_max_tokens,
        )

        return response.model_dump()

    except Exception as exc:
        log.exception("Groq API request failed")
        raise RuntimeError(f"Groq API request failed: {exc}") from exc


def polish_reply(factual_reply: str, *, context: str = "") -> str | None:
    """Re-phrase an already-computed deterministic reply with Groq.

    Used by ai_engine in AI_MODE=hybrid: the deterministic engine still decides
    every fact, booking and safety message, and Groq only improves the wording.
    Returns None when Groq is unavailable or the rewrite looks unsafe, so the
    caller can fall back to the original text.
    """
    if not is_available() or not factual_reply.strip():
        return None

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"Patient context: {context[:600]}"})
    messages.append({
        "role": "user",
        "content": (
            "Rewrite this reply for the patient. Keep every fact, number, name, "
            "time, fee and option index identical.\n\n"
            f"---\n{factual_reply}\n---"
        ),
    })

    try:
        client = Groq(api_key=settings.groq_api_key, timeout=settings.groq_timeout_seconds)  # type: ignore[misc]
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            temperature=0.3,
            max_tokens=settings.groq_max_tokens,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        log.exception("Groq polish request failed")
        return None

    # Cheap guardrail: never let the rewrite drop the important tokens.
    for token in _critical_tokens(factual_reply):
        if token not in text:
            log.warning("Groq polish dropped '%s'; keeping the deterministic reply", token)
            return None
    return text or None


def _critical_tokens(reply: str) -> list[str]:
    """Numbers / codes / option markers that a rewrite must not lose."""
    import re

    tokens = re.findall(r"(?:Rs\.?\s?[\d,]+(?:\.\d{2})?|\bAPT-[A-Z0-9-]+|\bVVH\d+\b|\*\d+\.\*)", reply)
    return list(dict.fromkeys(tokens))[:12]


# ---------------------------------------------------------------------------
# SEMANTIC UNDERSTANDING (hybrid fallback)
# ---------------------------------------------------------------------------
def _routing_vocabulary() -> str:
    """The canonical terms the model may return, taken from the hospital's own
    local dictionary so the two layers always speak the same language."""
    try:
        from . import medical_language

        terms = sorted(medical_language.SYMPTOM_TERMS)
    except Exception:  # noqa: BLE001 - never block the module import
        terms = ["fever", "pain", "cough", "cold", "itching", "rash", "knee", "eye", "stomach"]
    return ", ".join(terms)


SEMANTIC_PROMPT = """You convert a patient's sentence into structured routing \
information for a hospital front desk.

The patient may write in English, Hindi, Hinglish, a regional dialect, or with
heavy spelling mistakes, and may describe the problem in a long natural sentence
rather than a keyword. Examples: "bukhar 3 din se h", "ghutne mein dard",
"pith pe laal laal dane", "taav chadh gya", "tavda lg gya",
"subah uthne ke baad se meri aankh ajeeb si laal hai aur paani bhi aa raha hai",
"kal se chalne pe pair ajeeb sa khich raha hai".

Return ONLY a JSON object with these keys:
  "concern"        : short English summary of the complaint (max 12 words)
  "body_area"      : the body part in English, or null
  "symptoms"       : array of English symptom words, e.g. ["pain"], ["redness","watering"]
  "language"       : "en" | "hi" | "hinglish" | "other"
  "is_medical"     : true if this is a health complaint, false for booking/admin
                     questions ("show my reports", "cancel my appointment")
  "canonical_terms": array of terms from the hospital vocabulary below, best match
                     first. This is what the hospital routes on, so choose carefully.
                     Vocabulary: __VOCABULARY__
  "urgency_hint"   : "low" | "medium" | "high" - how urgent the patient sounds
  "confidence"     : number between 0 and 1 - your certainty about the ROUTING terms

Rules:
- NEVER give a diagnosis, a disease name as a conclusion, a medicine, a dose, or
  any treatment advice. Describe symptoms only, in the patient's own meaning.
- Use ONLY terms from the vocabulary above in "canonical_terms". It is better to
  return fewer terms than to invent one. Never invent a department name.
- If the sentence is unclear, regional or slangy, return an empty
  "canonical_terms" array and "confidence" below 0.4 rather than guessing.
- Output JSON only. No prose, no markdown fences.
""".replace("__VOCABULARY__", _routing_vocabulary())


def analyze_message(text: str) -> dict[str, Any] | None:
    """Read a patient message semantically and return ROUTING information only.

    Used by ``ai_engine.understand_concern`` for the messages the local layer
    could not fully read: long natural sentences, regional phrasing, unknown
    slang or fresh misspellings. A clear local match never reaches this function,
    so the dictionary still does the cheap work and the model does the language
    work - the balance this project wants.

    This function is a *reader*, never a decision maker:

      * it may not diagnose, prescribe or name a disease as a conclusion;
      * it may only return terms from the hospital's own vocabulary, and the
        caller re-validates every one of them against MySQL;
      * if it is unsure it must say so (low confidence), and the caller asks the
        patient instead of guessing.

    Returns a dict, or None when Groq is unavailable or the reply is unusable.
    """
    if not is_available() or not (text or "").strip():
        return None

    messages = [
        {"role": "system", "content": SEMANTIC_PROMPT},
        {"role": "user", "content": text.strip()[:400]},
    ]

    try:
        client = Groq(api_key=settings.groq_api_key, timeout=settings.groq_timeout_seconds)  # type: ignore[misc]
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            temperature=0.0,
            max_tokens=min(400, settings.groq_max_tokens),
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001 - the front desk must survive any API error
        log.exception("Groq semantic understanding failed")
        return None

    data = _parse_json_object(raw)
    if not data:
        return None

    def _list(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [str(v).strip()[:40] for v in value[:8] if str(v).strip()]

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    is_medical = data.get("is_medical")
    if isinstance(is_medical, str):
        is_medical = is_medical.strip().lower() in {"true", "yes", "1"}

    language = data.get("language")
    if isinstance(language, str):
        language = language.strip().lower()[:12] or None
    else:
        language = None

    urgency = data.get("urgency_hint")
    if isinstance(urgency, str):
        urgency = urgency.strip().lower()[:12] or None
    else:
        urgency = None

    return {
        "concern": (str(data.get("concern") or "")[:160] or None),
        "body_area": (str(data.get("body_area"))[:40] if data.get("body_area") else None),
        "symptoms": _list("symptoms"),
        "canonical_terms": [t.lower() for t in _list("canonical_terms")],
        "language": language,
        "is_medical": is_medical if isinstance(is_medical, bool) else None,
        "urgency_hint": urgency,
        "confidence": confidence,
    }


def semantic_understand(text: str) -> dict[str, Any] | None:
    """Backwards-compatible alias for :func:`analyze_message`.

    Kept because ``ai_engine`` and the test suite refer to this name; it applies
    the stricter "only return when we are reasonably sure" rule used by the
    original hybrid fallback.
    """
    result = analyze_message(text)
    if not result or float(result.get("confidence") or 0) < 0.5:
        if result:
            log.info("Semantic reader unsure (confidence %.2f)", result.get("confidence") or 0)
        return None
    return result


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object out of a model reply, tolerating code fences."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            log.warning("Groq semantic reply was not JSON: %r", raw[:120])
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            log.warning("Groq semantic reply was not parseable JSON: %r", raw[:120])
            return None
    return data if isinstance(data, dict) else None


def _history(db: Session, conversation_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(models.AIMessage)
        .where(models.AIMessage.conversation_id == conversation_id)
        .where(models.AIMessage.role.in_(["user", "assistant"]))
        .order_by(models.AIMessage.id.desc()).limit(limit)
    ).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


def _safe_args(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = dict(args or {})
    if ctx.patient_id:
        if name in {"get_patient_profile", "get_patient_summary", "get_patient_concerns",
                    "get_patient_appointments", "get_patient_prescriptions", "get_patient_files",
                    "get_invoice", "get_payment_status", "send_notification"}:
            # The authenticated patient is always authoritative (row-level scope).
            args["patient_id"] = ctx.patient_id
        if name == "book_appointment":
            args["patient_id"] = ctx.patient_id
        if name in {"cancel_appointment", "reschedule_appointment"}:
            appointment_id = args.get("appointment_id")
            appointment = ctx.db.get(models.Appointment, appointment_id) if appointment_id else None
            if not appointment or appointment.patient_id != ctx.patient_id:
                return {"__blocked__": "The requested appointment does not belong to the authenticated patient."}
        if name == "get_invoice" and args.get("invoice_id"):
            invoice = ctx.db.get(models.Invoice, args["invoice_id"])
            if not invoice or invoice.patient_id != ctx.patient_id:
                return {"__blocked__": "The requested invoice does not belong to the authenticated patient."}
        if name == "get_payment_status" and args.get("invoice_id"):
            invoice = ctx.db.get(models.Invoice, args["invoice_id"])
            if not invoice or invoice.patient_id != ctx.patient_id:
                return {"__blocked__": "The requested invoice does not belong to the authenticated patient."}
    return args


def chat(db: Session, conv: models.AIConversation, patient: models.Patient | None,
         user_text: str, *, user_id: int | None, channel: str, safety: dict[str, Any],
         add_message, escalate) -> dict[str, Any]:
    """Run one Groq conversation turn with controlled tool calling."""
    started = time.perf_counter()
    ctx = ToolContext(db, patient_id=conv.patient_id, conversation_id=conv.id,
                      user_id=user_id, caller="AI", channel=channel)

    intent = "GROQ"
    safety_flag = safety.get("flag")
    user_msg = add_message(db, conv, "user", user_text, intent=intent, confidence=1.0,
                           safety_flag=safety_flag)

    if safety.get("level") == "EMERGENCY":
        esc = escalate(db, conv, "EMERGENCY", "Emergency symptoms detected", safety.get("matched"), patient)
        reply = "This may be a medical emergency. Please call 108 or go to the nearest emergency department immediately. I have escalated this to the hospital team."
        add_message(db, conv, "assistant", reply, intent="EMERGENCY", confidence=1.0,
                    safety_flag=safety_flag)
        return _response(conv, reply, "EMERGENCY", patient, safety, ["emergency_escalation"], esc, started)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if patient:
        messages.append({"role": "system", "content": f"Authenticated patient context: patient_id={patient.id}, name={patient.full_name}. Use backend tools for all patient facts."})
    messages.extend(_history(db, conv.id, limit=20)[:-1])
    messages.append({"role": "user", "content": user_text})

    tools_used: list[str] = []
    final_reply = ""
    tool_payloads: list[dict[str, Any]] = []

    for _ in range(6):
        result = _groq_request(messages)
        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""

        if tool_calls:
            assistant_call_data = []
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                if name not in TOOL_NAMES:
                    tool_result = {"success": False, "error": f"Tool '{name}' is not allowed"}
                else:
                    safe_args = _safe_args(ctx, name, args)
                    if "__blocked__" in safe_args:
                        tool_result = {"success": False, "error": safe_args["__blocked__"]}
                    else:
                        tools_used.append(name)
                        tool_result = ai_tools.invoke_tool(db, ctx, name, safe_args)
                tool_payloads.append({"name": name, "arguments": args, "result": tool_result})
                assistant_call_data.append({"id": call.get("id"), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(tool_result, default=str)[:8000]})
            messages.insert(len(messages) - len(tool_calls), {"role": "assistant", "content": content, "tool_calls": assistant_call_data})
            continue

        final_reply = content.strip() or "I could not complete that request. Please try again or contact reception."
        break
    else:
        final_reply = "I could not complete that request within the allowed steps. Please contact reception."

    if safety.get("level") in {"RESTRICTED", "HIGH"}:
        final_reply += "\n\nI can help with hospital operations, but medical advice and prescriptions must come from a doctor."

    latency_ms = int((time.perf_counter() - started) * 1000)
    add_message(db, conv, "assistant", final_reply, intent="GROQ", confidence=1.0,
                tool_calls=tools_used, latency_ms=latency_ms, safety_flag=safety_flag)
    conv.stage = "IDLE"
    conv.avg_response_ms = latency_ms
    db.commit()
    return _response(conv, final_reply, "GROQ", patient, safety, tools_used, None, started, tool_payloads)


def _response(conv, reply, intent, patient, safety, tools_used, escalation, started, tool_payloads=None):
    """Response envelope.

    The key set must match the deterministic engine (ai_engine.handle_message)
    because ai_chat.html, the smoke test and the API clients all read the same
    fields. Anything the Groq path cannot fill is returned empty rather than
    omitted, so the UI never breaks with a KeyError.
    """
    return {
        "conversation_id": conv.id,
        "session_id": conv.session_id,
        "reply": reply,
        "intent": intent,
        "confidence": 1.0,
        "stage": conv.stage,
        "patient": {"id": patient.id, "name": patient.full_name, "patient_code": patient.patient_code} if patient else None,
        "tools_used": tools_used,
        "safety": safety,
        "escalation": {"id": escalation.id, "level": escalation.level, "reason": escalation.reason} if escalation else None,
        # --- contract fields shared with the deterministic engine ---
        "concern": None,
        "specialty": None,
        "doctors": [],
        "slots": [],
        "appointment": None,
        "prescriptions": [],
        "files": [],
        "payments": [],
        "quick_replies": [],
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "tool_results": tool_payloads or [],
    }
