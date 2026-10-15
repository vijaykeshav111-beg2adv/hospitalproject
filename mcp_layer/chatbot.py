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

import difflib
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

# "do i have an appointment today?" is a paperwork question, not a request
# for a diagnosis. If one of these words is present we do not treat
# "do i have" as medical advice.
PAPERWORK_WORDS = ("appointment", "booking", "invoice", "bill", "prescription",
                   "report", "slot", "payment", "queue", "token")

GREETING_WORDS = ("hello", "hi", "hii", "hey", "namaste", "namaskar")
HELP_WORDS = ("help", "what can you do", "commands", "kya kar sakte", "option", "options")


def _contains(text: str, phrases) -> bool:
    return any(p in text for p in phrases)


def _has_word(text: str, words, exact: bool = False) -> bool:
    """True if any word/phrase appears as a WHOLE word.

    The old code used plain `in`, so "hi " matched "delhi ", "lab" matched
    "available" and "file" matched "profile".  By default a word may have a
    longer ending ("doctor" matches "doctors"); exact=True turns that off.
    """
    for word in words:
        ending = "" if exact else r"\w*"
        if re.search(r"\b" + re.escape(word) + ending + r"\b", text):
            return True
    return False


def _is_smalltalk(text: str) -> bool:
    return _has_word(text, GREETING_WORDS, exact=True) or _has_word(text, HELP_WORDS, exact=True)


# --------------------------------------------------------------------------
# Specialty / symptom vocabulary (same names as app/seed.py SPECIALTIES)
# --------------------------------------------------------------------------
SPECIALTY_KEYWORDS: dict[str, list[str]] = {
    "General Medicine": ["general medicine", "internal medicine", "family medicine", "general physician", "physician", "fever", "cold", "cough",
                         "flu", "weakness", "body ache", "typhoid", "infection", "diabetes", "sugar",
                         "blood pressure", "bp", "fatigue", "vomiting", "loose motion", "diarrhoea",
                         "diarrhea", "bukhar", "khansi"],
    "Cardiology": ["cardiology", "cardiologist", "cardio", "heart", "palpitations", "cholesterol", "ecg"],
    "Orthopaedics": ["orthopaedics", "orthopedics", "orthopaedic", "orthopedic", "ortho", "bone", "joint",
                     "knee", "back pain", "sprain", "shoulder", "neck pain", "ghutna", "kamar dard"],
    "Dermatology": ["dermatology", "dermatologist", "derma", "skin", "rash", "itching", "acne", "pimple",
                    "hair fall", "eczema", "fungal", "allergy", "khujli"],
    "Paediatrics": ["paediatrics", "pediatrics", "pediatrician", "paediatrician", "child", "children",
                    "baby", "infant", "vaccination", "newborn", "bacche"],
    "Obstetrics & Gynaecology": ["gynaecology", "gynecology", "gynecologist", "gynae", "pregnancy",
                                 "pregnant", "periods", "menstrual", "pcos"],
    "ENT": ["ent", "ear", "nose", "throat", "sinus", "tonsil", "hearing", "kaan", "naak"],
    "Neurology": ["neurology", "neurologist", "neuro", "headache", "migraine", "seizure", "dizziness",
                  "numbness", "vertigo", "sir dard", "chakkar"],
    "Gastroenterology": ["gastroenterology", "gastroenterologist", "gastro", "stomach", "acidity", "gas",
                         "ulcer", "liver", "jaundice", "constipation", "pet dard",
                         # common wrong spellings people actually type
                         "gastronomy", "gastrology"],
    "Pulmonology": ["pulmonology", "pulmonologist", "pulmo", "asthma", "breathing", "wheezing",
                    "tuberculosis", "bronchitis", "saans"],
    "Ophthalmology": ["ophthalmology", "ophthalmologist", "eye", "vision", "cataract", "aankh"],
    "Psychiatry": ["psychiatry", "psychiatrist", "stress", "anxiety", "depression", "insomnia"],
}

# Phrases are matched longest-first, so "back pain" wins over "pain".
_PHRASES: list[tuple[str, str]] = sorted(
    ((word, name) for name, words in SPECIALTY_KEYWORDS.items() for word in words),
    key=lambda pair: len(pair[0]), reverse=True,
)

# Single words used for typo matching.  "general" alone is only used here
# (never as an exact match) so "general doctor" and "gernal medicine" both work.
_TYPO_WORDS: dict[str, str] = {w: name for w, name in _PHRASES if " " not in w and len(w) >= 5}
_TYPO_WORDS["general"] = "General Medicine"


def _remove_department_medicine(m: str) -> str:
    """Remove 'general medicine' / 'internal medicine' (even misspelt) from the text."""
    def fix(match):
        word = match.group(1)
        close = difflib.get_close_matches(word, ["general", "internal", "family"], n=1, cutoff=0.7)
        return " " if close else match.group(0)

    return re.sub(r"\b([a-z]{5,8}) medicine\b", fix, m)


def _find_specialty(m: str, allow_typos: bool = False) -> str:
    """Return the specialty name this message is about, or '' if none."""
    for phrase, name in _PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"s?\b", m):
            return name

    if allow_typos:
        for token in re.findall(r"[a-z]{5,}", m):
            close = difflib.get_close_matches(token, list(_TYPO_WORDS), n=1, cutoff=0.7)
            if close:
                return _TYPO_WORDS[close[0]]
    return ""


# --------------------------------------------------------------------------
# Planner: message -> list of (server, tool, args) calls
# --------------------------------------------------------------------------
def _plan(message: str, principal: Principal) -> list[dict[str, Any]]:
    """Turn a message into tool calls.

    Returns [] when nothing matched.  The caller then shows help or asks
    a question.  We NEVER guess an admin tool for a normal user.
    """
    m = " " + re.sub(r"\s+", " ", message.lower().strip()) + " "
    calls: list[dict[str, Any]] = []

    def add(server: str, tool: str, **args):
        calls.append({"server": server, "tool": tool, "args": args})

    # id / code extraction
    # A code needs a digit and may have hyphens, e.g. INV-202610-050400.
    # (The old pattern matched the plain word "INVOICES" and missed hyphenated codes.)
    code = re.search(r"\b((?:APT|INV|PRX|RX|PAT)-?\d[A-Z0-9-]*)\b", message.upper())
    code_val = code.group(1) if code else ""
    num = None if code_val else re.search(r"\b(\d{1,6})\b", m)   # digits inside a code are not an id

    # "General Medicine" is a department, not a tablet question.
    # This also covers a typo like "gernal medicine".
    m_no_dept = _remove_department_medicine(m)
    specialty_exact = _find_specialty(m)

    # ---- billing ---------------------------------------------------------
    if _has_word(m, ("invoice", "bill", "payment", "paisa", "amount due", "outstanding", "balance")):
        if code_val.startswith("INV") or (num and _has_word(m, ("invoice", "bill"))):
            add("billing", "get_invoice", invoice_number=code_val,
                invoice_id=int(num.group(1)) if num and code_val == "" else None)
        if _has_word(m, ("outstanding", "due", "balance")):
            add("billing", "outstanding_invoices")
        elif not calls:
            add("billing", "get_payment_status", patient_id=int(num.group(1)) if num else None)
        return calls

    # ---- prescriptions / medicines ---------------------------------------
    if _has_word(m_no_dept, ("prescription", "medicine", "dawai", "tablet")) or _has_word(m_no_dept, ("rx",), exact=True):
        if code_val or num:
            add("prescriptions", "get_prescription",
                prescription_code=code_val, prescription_id=int(num.group(1)) if num else None)
        elif _has_word(m_no_dept, ("medicine", "dawai")):
            q = m_no_dept.split("medicine")[-1].replace("for", "").strip()[:40]
            add("prescriptions", "list_medicines", query=q)
        else:
            add("prescriptions", "list_prescriptions", patient_id=None)
        return calls

    # ---- appointments ----------------------------------------------------
    if _has_word(m, ("appointment", "booking", "book", "slot", "cancel", "reschedule", "queue", "token")):
        if "cancel" in m and num:
            add("appointments", "cancel_appointment", appointment_id=int(num.group(1)), reason="via chatbot")
            return calls
        if _has_word(m, ("queue", "waiting")):
            add("appointments", "queue_board")
            return calls
        if code_val.startswith("APT") or _has_word(m, ("my appointment", "appointments", "status")):
            add("appointments", "list_appointments", patient_id=int(num.group(1)) if num else None)
            return calls
        if _has_word(m, ("slot", "available", "book")):
            if specialty_exact:
                add("doctors", "search_doctors", query=specialty_exact)
            add("appointments", "get_available_slots")
            return calls
        add("appointments", "list_appointments", patient_id=int(num.group(1)) if num else None)
        return calls

    # ---- doctors / specialties -------------------------------------------
    if _has_word(m, ("doctor", "specialist", "specialty", "specialties", "speciality", "department")) \
            or _has_word(m, ("dr",), exact=True):
        specialty = _find_specialty(m, allow_typos=True)
        if specialty:
            add("doctors", "search_doctors", query=specialty)
        elif _has_word(m, ("specialty", "specialties", "speciality", "department")):
            add("doctors", "list_specialties")
        else:
            name = re.search(r"\bdr\.?\s+([a-z]+(?: [a-z]+)?)", m)
            add("doctors", "search_doctors", query=name.group(1) if name else "")
        return calls

    # ---- patients --------------------------------------------------------
    if _has_word(m, ("patient", "rogi", "history", "timeline")) or _has_word(m, ("pt",), exact=True):
        if code_val.startswith("PAT") or num:
            add("patients", "get_patient", patient_id=int(num.group(1)) if num else None)
        else:
            add("patients", "search_patients", query=message.strip()[:40])
        return calls

    # ---- ADMIN tier intents ----------------------------------------------
    # (checked before "report" so "revenue report" is analytics, not lab reports)
    if _has_word(m, ("revenue", "collection", "analytics", "growth", "dashboard", "kpi", "monitor")):
        add("analytics", "daily_dashboard")
        add("analytics", "revenue_report")
        return calls
    if _has_word(m, ("notification", "whatsapp")) or _contains(m, ("email sent", "message sent")):
        add("notifications", "list_notifications")
        return calls
    if _has_word(m, ("review", "rating", "feedback")):
        add("reviews", "list_reviews")
        add("reviews", "doctor_rating_summary")
        return calls

    # ---- patient records -------------------------------------------------
    if _has_word(m, ("record", "report", "lab", "file", "consultation")):
        add("prescriptions", "list_medical_records", patient_id=None)
        return calls

    # ---- SYSTEM tier intents ---------------------------------------------
    if _has_word(m, ("audit", "session")) or _contains(m, ("login activity", "who changed")):
        add("security", "audit_log")
        return calls
    if _contains(m, ("users list", "list users", "role matrix", "permission", "rbac")):
        add("security", "role_matrix")
        return calls
    if _contains(m, ("system status", "system health", "db status", "database status",
                     "mcp status", "background jobs", "job status")):
        add("ops", "mcp_status")
        add("ops", "db_health")
        return calls

    # ---- greeting / help: no tools needed --------------------------------
    if _is_smalltalk(m):
        return []

    # ---- symptom or misspelt department ("fever", "gestronomy") ----------
    specialty = _find_specialty(m, allow_typos=True)
    if specialty:
        add("doctors", "search_doctors", query=specialty)
        return calls

    # ---- default ---------------------------------------------------------
    # Only staff with ADMIN tier or higher get the dashboard.  A patient's
    # unknown message used to be sent to this ADMIN tool and always came
    # back as "rbac_denied".  Now they get a clarifying question instead.
    if principal.can_call("ADMIN"):
        add("analytics", "daily_dashboard")
    return calls


# --------------------------------------------------------------------------
# Reply rendering - text ONLY from envelopes
# --------------------------------------------------------------------------
MAX_ROWS = 25   # tools already limit their rows, so this is only a safety limit


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
            lines.extend(_rows(q, data))
        elif isinstance(data, dict):
            lines.append(f"[{q}]")
            for key, value in list(data.items())[:12]:
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    lines.extend(_rows(f"   {key}", value))
                elif isinstance(value, (dict, list)):
                    lines.append(f"   {key}: {_flat(value)[:160]}")
                else:
                    lines.append(f"   {key}: {value}")
        else:
            lines.append(f"[{q}] {data}")
    return "\n".join(lines) if lines else ""


def _rows(title: str, rows: list) -> list[str]:
    """One line per row. Shows every row (up to MAX_ROWS)."""
    lines = [f"{title} - {len(rows)} row(s):"]
    for row in rows[:MAX_ROWS]:
        lines.append("   • " + _flat(row))
    if len(rows) > MAX_ROWS:
        lines.append(f"   … {len(rows) - MAX_ROWS} more (ask a narrower question to see them)")
    return lines


def _flat(value: Any) -> str:
    if isinstance(value, dict):
        # id columns are for the computer, not for the person reading
        items = [(k, v) for k, v in value.items() if k != "id" and not k.endswith("_id")]
        return ", ".join(f"{k}={v}" for k, v in items[:7])
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
# Help and clarify replies (no database needed)
# --------------------------------------------------------------------------
def _help_reply(principal: Principal) -> dict:
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
            "   • 'fever' or 'skin doctor'\n"
            "   • 'daily dashboard'       • 'mcp status'  (admin/system)"
        ),
        "generated": True,
        "grounded": True,
        "answer": "help",
        "sources": ["registry.list_tools"],
        "intent": "help",
        "plan": [],
    }


def _clarify_reply(text: str) -> dict:
    """Used when we cannot tell what the person wants. We ask, we do not guess."""
    bare_number = re.fullmatch(r"#?\s*\d{1,6}", text.strip())
    if bare_number:
        question = (f"I see the number {text.strip().lstrip('#')}, but I am not sure what it is. "
                    "Is it an appointment, an invoice, a prescription or a patient number?")
    else:
        question = ("I did not understand that. You can ask about doctors or a department "
                    "(for example 'fever' or 'skin doctor'), appointments and slots, "
                    "prescriptions or bills.")
    return {
        "reply": question + "\nType 'help' to see everything I can do.",
        "generated": True,
        "grounded": False,
        "answer": "clarify",
        "sources": [],
        "intent": "clarify",
        "plan": [],
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
        # "do i have an appointment?" is not a request for a diagnosis
        only_do_i_have = not _contains(low, tuple(a for a in ADVICE_REQUESTS if a != "do i have"))
        if not (only_do_i_have and _has_word(low, PAPERWORK_WORDS)):
            return _refusal("safety_medical_advice")

    # ---- plan (pure text logic, needs no database) ----------------------
    plan = _plan(text, principal)
    if not plan:
        if _is_smalltalk(low):
            return _help_reply(principal)
        return _clarify_reply(text)

    # ---- grounded DB pre-check ----------------------------------------
    health = guard.db_status()
    if not health["ok"]:
        return _refusal("db_unavailable", str(health.get("detail", "")))

    # ---- execute ---------------------------------------------------------
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
        f"{body}"
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
