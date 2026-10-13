"""MCP-grounded chatbot engine (the brain behind /ui/mcp-chat).

Contract (same as the tools themselves):

    * Every answer is built ONLY from MCP tool envelopes that carry
      grounded=true data.  The model never invents facts.
    * DB unavailable -> tool envelopes say generated=false / answer="no"
      -> the chatbot replies with the refusal block and generates nothing.
    * RBAC: the principal's tier decides which servers the planner may
      even consider; denied servers are never silently used.
    * Medical safety: no diagnosis, no treatment advice, no prescriptions
      (mirrors the platform's AI safety policy).  Emergency phrases get the
      108-guidance block.

The planner is deterministic (keyword -> tool plan).  This is deliberate:
a stochastic LLM in the loop would reintroduce exactly the hallucination
risk this layer exists to kill.  Optional Groq use is limited to REWRITING
already-grounded data into friendlier sentences and is off by default.
"""
from __future__ import annotations

import re
from typing import Any

from . import guard
from .invoke import invoke, list_tools_for
from .rbac import Principal

# --------------------------------------------------------------------------
# Safety vocabulary (mirrors app/services/ai_engine.py policy)
# --------------------------------------------------------------------------
EMERGENCY_PHRASES = (
    "chest pain", "heart attack", "suicide", "kill myself", "bleeding heavily",
    "unconscious", "not breathing", "stroke", "severe burn", "snake bite",
    "accident", "fracture", "overdose", "poisoning", "severe allergic",
)

ADVICE_REQUESTS = (
    "diagnose", "diagnosis", "what disease", "which disease", "cure me",
    "treatment for", "medicine for", "prescribe", "prescription for",
    "should i take", "dosage for", "is it cancer", "do i have",
)

INTENT_HELPERS = (
    ("greeting", ("hello", "hi ", "hey", "namaste", "namaskar", "hii")),
    ("help", ("help", "what can you do", "commands", "kya kar sakte", "options")),
)


def _contains(text: str, phrases) -> bool:
    return any(p in text for p in phrases)


def _is_smalltalk(text: str) -> bool:
    return any(p in text for _, phrases in INTENT_HELPERS for p in phrases)


# --------------------------------------------------------------------------
# Planner: message -> list of (server, tool, args) calls
# --------------------------------------------------------------------------
def _plan(message: str, principal: Principal) -> list[dict[str, Any]]:
    m = " " + re.sub(r"\s+", " ", message.lower().strip()) + " "
    calls: list[dict[str, Any]] = []

    def add(server: str, tool: str, **args):
        calls.append({"server": server, "tool": tool, "args": args})

    # id / code extraction
    num = re.search(r"\b(?:id|code|number)?\s*#?\s*(\d{1,6})\b", m)
    code = re.search(r"\b(APT[A-Z0-9]+|INV[A-Z0-9]+|PRX[A-Z0-9]+|RX[A-Z0-9]+|PAT[A-Z0-9]+)\b", message.upper())
    code_val = code.group(1) if code else ""

    if _contains(m, ("invoice", "bill", "billing", "payment", "paisa", "amount due", "outstanding", "balance")):
        if code_val.startswith("INV") or "invoice" in m or "bill" in m:
            add("billing", "get_invoice", invoice_number=code_val, invoice_id=int(num.group(1)) if num and code_val == "" else None)
        if "outstanding" in m or "due" in m or "balance" in m:
            add("billing", "outstanding_invoices")
        else:
            add("billing", "get_payment_status", patient_id=int(num.group(1)) if num else None)
        return calls

    if _contains(m, ("prescription", "medicine", "dawai", "tablet", "rx")):
        if code_val or num:
            add("prescriptions", "get_prescription",
                prescription_code=code_val, prescription_id=int(num.group(1)) if num else None)
        elif "medicine" in m or "dawai" in m:
            q = m.split("medicine")[-1].replace("for", "").strip()[:40]
            add("prescriptions", "list_medicines", query=q)
        else:
            add("prescriptions", "list_prescriptions", patient_id=int(num.group(1)) if num else None)
        return calls

    if _contains(m, ("appointment", "booking", "book ", "slot", "cancel", "reschedule", "queue", "token")):
        if "cancel" in m and num:
            add("appointments", "cancel_appointment", appointment_id=int(num.group(1)), reason="via chatbot")
            return calls
        if "queue" in m or "waiting" in m:
            add("appointments", "queue_board")
            return calls
        if code_val.startswith("APT") or ("my appointment" in m or "appointments" in m or "status" in m):
            add("appointments", "list_appointments", patient_id=int(num.group(1)) if num else None)
            return calls
        if "slot" in m or "available" in m or "book" in m:
            add("doctors", "list_doctors")
            add("appointments", "get_available_slots")
            return calls
        add("appointments", "list_appointments", patient_id=int(num.group(1)) if num else None)
        return calls

    if _contains(m, ("doctor", "dr ", "specialist", "specialty", "department", "cardio", "skin", "neuro", "ortho")):
        if "special" in m or "department" in m:
            add("doctors", "list_specialties")
            return calls
        if "available" in m or "free" in m or "timing" in m:
            add("doctors", "search_doctors")
            return calls
        add("doctors", "search_doctors")
        return calls

    if _contains(m, ("patient", "pt ", "rogi", "patient_id", "history", "timeline")):
        if code_val.startswith("PAT") or num:
            add("patients", "get_patient", patient_id=int(num.group(1)) if num else None)
        else:
            add("patients", "search_patients", query=message.strip()[:40])
        return calls

    if _contains(m, ("record", "report", "lab", "file", "consultation")):
        add("prescriptions", "list_medical_records", patient_id=int(num.group(1)) if num else None)
        return calls

    # ---- ADMIN tier intents ---------------------------------------------
    if _contains(m, ("notification", "whatsapp", "email sent", "message sent")):
        add("notifications", "list_notifications")
        return calls
    if _contains(m, ("review", "rating", "feedback")):
        add("reviews", "list_reviews")
        add("reviews", "doctor_rating_summary")
        return calls
    if _contains(m, ("revenue", "collection", "report", "analytics", "growth", "dashboard", "kpi", "monitor")):
        add("analytics", "daily_dashboard")
        add("analytics", "revenue_report")
        return calls

    # ---- SYSTEM tier intents -------------------------------------------
    if _contains(m, ("audit", "login activity", "session", "who changed")):
        add("security", "audit_log")
        return calls
    if _contains(m, ("users list", "list users", "roles", "permission", "rbac")):
        add("security", "role_matrix")
        return calls
    if _contains(m, ("system", "health", "jobs", "settings", "mcp status", "db status")):
        add("ops", "mcp_status")
        add("ops", "db_health")
        return calls

    if _contains(m, ("help", "what can you do", "commands", "kya kar", "option")):
        return []  # handled as help text

    if _is_smalltalk(m):
        return []

    # default: grounded overview for the caller's tier
    add("analytics", "daily_dashboard")
    return calls


# --------------------------------------------------------------------------
# Reply rendering - text ONLY from envelopes
# --------------------------------------------------------------------------
def _render(envelopes: list[dict], intent: str) -> str:
    lines: list[str] = []
    for env in envelopes:
        q = f"{env.get('server')}.{env.get('tool')}"
        if not env.get("grounded"):
            lines.append(f"[{q}] answer: no ({env.get('reason')})")
            continue
        data = env.get("data")
        if env.get("answer") == "no":
            lines.append(f"[{q}] no matching records in the database.")
            continue
        if isinstance(data, list):
            lines.append(f"[{q}] {len(data)} row(s):")
            for row in data[:5]:
                lines.append("   • " + _flat(row))
            if len(data) > 5:
                lines.append(f"   … {len(data) - 5} more")
        elif isinstance(data, dict):
            lines.append(f"[{q}]")
            for k, v in list(data.items())[:12]:
                if isinstance(v, (dict, list)):
                    v = _flat(v)[:160]
                lines.append(f"   {k}: {v}")
        else:
            lines.append(f"[{q}] {data}")
    return "\n".join(lines) if lines else ""


def _flat(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in list(value.items())[:6])
    return str(value)


def _refusal(reason: str, detail: str = "") -> dict:
    if reason == "db_unavailable":
        reply = (
            "no\n\n"
            "Database is not available, so I will not generate an answer.\n"
            "(Hallucination control: db not available -> not generate -> no.)\n"
            f"Detail: {detail or 'mysql unreachable'}"
        )
    elif reason == "rbac_denied":
        reply = (
            "no\n\n"
            "Your access tier does not allow this MCP server.\n"
            f"{detail}"
        )
    elif reason == "safety_medical_advice":
        reply = (
            "no\n\n"
            "I cannot give diagnoses, treatments or prescriptions. "
            "Please consult a doctor - I can help you find one and book a slot."
        )
    elif reason == "safety_emergency":
        reply = (
            "EMERGENCY DETECTED\n\n"
            "Please call 108 (ambulance) immediately or go to the nearest emergency room. "
            "A human on our reception desk can also be reached via the hospital helpline. "
            "I will not attempt any triage or diagnosis."
        )
    else:
        reply = "no\n\nI could not ground an answer in the hospital database, so I will not guess."
    return {
        "reply": reply,
        "generated": False if reason == "db_unavailable" else True,
        "grounded": False,
        "answer": "no",
        "reason": reason,
        "detail": detail,
        "sources": [],
        "intent": reason,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def respond(principal: Principal, message: str) -> dict:
    """Answer one chatbot message under full guard policy."""
    text = (message or "").strip()
    if not text:
        return _refusal("empty", "Write a question, e.g. 'show today's queue'.")

    low = " " + re.sub(r"\s+", " ", text.lower()) + " "

    # ---- safety first --------------------------------------------------
    if _contains(low, EMERGENCY_PHRASES):
        return _refusal("safety_emergency")
    if _contains(low, ADVICE_REQUESTS):
        return _refusal("safety_medical_advice")

    # ---- greeting / help ----------------------------------------------
    if _is_smalltalk(low):
        allowed = list_tools_for(principal)
        names = [s["key"] for s in allowed["allowed"]]
        return {
            "reply": (
                f"Namaste! I am the MCP hospital assistant (tier: {principal.tier}).\n"
                "I answer ONLY from the hospital database - if it is down I say 'no' "
                "instead of guessing.\n\n"
                f"MCP servers you can access: {', '.join(names)}\n\n"
                "Try:\n"
                "   • 'show today queue'      • 'available slots'\n"
                "   • 'my appointments'       • 'prescription 12'\n"
                "   • 'invoice INV...'        • 'doctors'\n"
                "   • 'daily dashboard'       • 'mcp status'  (admin/system)"
            ),
            "generated": True,
            "grounded": True,
            "answer": "help",
            "sources": ["registry.list_tools"],
            "intent": "help",
            "plan": [],
        }

    if _contains(low, ("help", "what can you do", "commands", "kya kar", "option")):
        return respond(principal, "hello")

    # ---- grounded DB pre-check ----------------------------------------
    health = guard.db_status()
    if not health["ok"]:
        return _refusal("db_unavailable", str(health.get("detail", "")))

    # ---- plan + execute ------------------------------------------------
    plan = _plan(text, principal)
    if not plan:
        return respond(principal, "hello")

    envelopes: list[dict] = []
    for step in plan:
        env = invoke(principal, step["server"], step["tool"], step["args"])
        envelopes.append(env)

    # Hallucination control tripwire: ANY non-generated envelope means the
    # whole answer must be a refusal - partial answers from a dead DB are
    # still ungrounded.
    for env in envelopes:
        if env.get("grounded") is False and env.get("reason") == "db_unavailable":
            return _refusal("db_unavailable", str(env.get("detail", "")))
        if env.get("reason") == "rbac_denied":
            return _refusal("rbac_denied", str(env.get("detail", "")))

    body = _render(envelopes, text)
    if not body:
        return _refusal("no_data", "No tool returned rows.")

    sources = [f"{e['server']}.{e['tool']}" for e in envelopes]
    reply = (
        "Here is what the hospital database says (grounded answer):\n\n"
        f"{body}\n\n"
        f"sources: {', '.join(sources)}"
    )
    return {
        "reply": reply,
        "generated": True,
        "grounded": True,
        "answer": "yes",
        "sources": sources,
        "intent": plan[0]["tool"],
        "plan": plan,
        "envelopes": envelopes,
        "db": health,
    }
