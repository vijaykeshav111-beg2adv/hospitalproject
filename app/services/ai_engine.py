"""AI front-desk engine.

Pipeline:   patient message -> safety scan -> intent detection -> concern
extraction -> structured concern -> specialty matching -> doctor matching ->
availability -> slots -> patient choice -> booking.

Safety rules are enforced *before* anything else:
  * no medical advice, no diagnosis, no prescriptions - only routing
  * emergency phrases immediately escalate to reception + doctor and show 108
  * uncertainty is admitted, never invented
  * every message, tool call and routing decision is persisted for audit
"""
from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import time
import urllib.request
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..security import utcnow
from ..naming import doctor_label as doc_label
from . import ai_tools, medical_language, nlp_signals, notify, slots as slot_service
from .ai_tools import ToolContext

log = logging.getLogger("vvh.ai")

# ==========================================================================
# SAFETY
# ==========================================================================
EMERGENCY_PATTERNS = [
    r"\b(chest pain|severe chest|heart attack|crushing chest)\b",
    r"\b(can'?t breathe|cannot breathe|breathless|shortness of breath|saans nahi)\b",
    r"\b(unconscious|fainted|collapsed|seizure|fits|convulsion)\b",
    r"\b(heavy bleeding|bleeding a lot|blood vomit|vomiting blood|blood in stool)\b",
    r"\b(stroke|paralysis|slurred speech|face droop)\b",
    r"\b(accident|trauma|road accident|fracture with bleeding)\b",
    r"\b(suicide|self harm|overdose|poisoning)\b",
    r"\b(pregnancy bleeding|labour pain|no fetal movement)\b",
    r"\b(high fever with rash|stiff neck|confusion and fever)\b",
]
URGENT_PATTERNS = [
    r"\b(high fever|104|103|105)\b", r"\b(vomiting since|diarrhoea since|dehydration)\b",
    r"\b(severe pain|unbearable pain|pain 9|pain 10)\b", r"\b(asthma attack|wheezing badly)\b",
    r"\b(injured|deep cut|swelling suddenly)\b",
]
ADVICE_REQUEST_PATTERNS = [
    r"\b(what medicine|which medicine|medicine for|dawa|tablet for)\b",
    r"\b(diagnos\w*|what disease|do i have|kya bimari)\b",
    r"\b(should i take|can i take|dosage of|dose of|kitni dawa)\b",
    r"\b(treat(ment)? for|how to cure|home remedy|remedy for)\b",
    r"\b(antibiotic|painkiller|injection)\b.*\b(for|le\s?lu)\b",
]
PRESCRIPTION_REQUEST_PATTERNS = [
    r"\b(prescribe|write me a prescription|give me a prescription|medicine likh)\b",
]

DEFAULT_EMERGENCY_REPLY = (
    "This sounds like a **medical emergency**. Please do not wait for an online booking.\n\n"
    "* Call **108** (ambulance) or go to the nearest emergency department immediately.\n"
    "* If you are already nearby, our reception desk is being alerted right now.\n\n"
    "I can book an urgent slot for you *after* you have spoken to the emergency team. "
    "Please take care - your safety comes first."
)

SAFETY_DISCLAIMER = (
    "I can only help with appointments, records, reports and billing - "
    "I am not able to give medical advice, a diagnosis or medicines. "
    "That part is always done by our doctors in person."
)

# System prompt used by the optional Groq "polish" layer (AI_MODE=hybrid).
# The deterministic engine already knows the facts; this prompt only controls
# the wording, and it is forbidden from adding or changing any of them.
SYSTEM_PROMPT = (
    "You are the front-desk assistant of Vijay Vargiya Group of Hospitals. "
    "Rewrite the given factual reply in warm, clear, concise English (or in the "
    "same language the patient used). Keep every fact, name, doctor, specialty, "
    "date, time, fee, patient code, number and option index exactly as given. "
    "Never add medical advice, a diagnosis, a medicine or a promise that is not "
    "in the text. Never remove the option numbers or the question that the "
    "patient has to answer. Return only the rewritten reply."
)


def scan_safety(text: str) -> dict:
    """Local, authoritative safety scan. Runs BEFORE any AI interpretation.

    Hindi / Hinglish emergencies are checked first (medical_language), because
    patients in this catchment describe emergencies in Hindi, and because the
    safety layer must never depend on an LLM being reachable. Ambiguity is
    resolved towards caution: if a phrase *might* be an emergency we ask or
    escalate rather than quietly routing it as a normal complaint.
    """
    low = (text or "").lower()

    hindi_emergency = medical_language.has_emergency_term(low)
    if hindi_emergency:
        return {"level": "EMERGENCY", "flag": "EMERGENCY_DETECTED",
                "matched": hindi_emergency, "escalate": "EMERGENCY",
                "language": "hi"}

    for pattern in EMERGENCY_PATTERNS:
        if re.search(pattern, low):
            return {"level": "EMERGENCY", "flag": "EMERGENCY_DETECTED",
                    "matched": re.search(pattern, low).group(0), "escalate": "EMERGENCY"}

    hindi_urgent = medical_language.has_urgent_term(low)
    if hindi_urgent:
        return {"level": "HIGH", "flag": "URGENT_SYMPTOM",
                "matched": hindi_urgent, "escalate": "DOCTOR", "language": "hi"}

    for pattern in URGENT_PATTERNS:
        if re.search(pattern, low):
            return {"level": "HIGH", "flag": "URGENT_SYMPTOM",
                    "matched": re.search(pattern, low).group(0), "escalate": "DOCTOR"}
    for pattern in PRESCRIPTION_REQUEST_PATTERNS:
        if re.search(pattern, low):
            return {"level": "RESTRICTED", "flag": "PRESCRIPTION_RESTRICTION",
                    "matched": re.search(pattern, low).group(0), "escalate": None}
    for pattern in ADVICE_REQUEST_PATTERNS:
        if re.search(pattern, low):
            return {"level": "RESTRICTED", "flag": "MEDICAL_ADVICE_RESTRICTION",
                    "matched": re.search(pattern, low).group(0), "escalate": None}
    return {"level": "NORMAL", "flag": None, "matched": None, "escalate": None}


# ==========================================================================
# INTENT DETECTION
# ==========================================================================
INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    ("EMERGENCY", [r"\b(emergency|108|ambulance|accident|unconscious|heart attack)\b"]),
    ("HUMAN_ESCALATION", [r"\b(human|reception|talk to someone|agent|staff|call me|manager|complaint)\b"]),
    ("CANCEL_APPOINTMENT", [r"\b(cancel|call off|nahi aa paunga|cancel kar)\b"]),
    ("RESCHEDULE_APPOINTMENT", [r"\b(reschedule|postpone|change (my )?appointment|shift my appointment|prepone|aage badha)\b"]),
    ("APPOINTMENT_STATUS", [r"\b(my appointment|appointment status|booking status|token|when is my|upcoming appointment)\b"]),
    ("PRESCRIPTION_LOOKUP", [r"\b(prescription|parcha|medicine list|dawai list)\b"]),
    ("MEDICAL_FILE_LOOKUP", [r"\b(report|lab report|scan|x-?ray|mri|ct|blood test|files?|document)\b"]),
    ("PAYMENT_STATUS", [r"\b(payment status|paid|pending payment|balance|due amount|refund)\b"]),
    ("INVOICE_LOOKUP", [r"\b(invoice|bill|receipt|charges|fees?)\b"]),
    ("NOTIFICATION_REQUEST", [r"\b(send|whatsapp|email|sms|notify|remind me|message me)\b"]),
    ("REVIEW", [r"\b(review|feedback|rating|complain about doctor)\b"]),
    ("DOCTOR_SEARCH", [r"\b(which doctor|best doctor|available doctor|doctor for|specialist|specialty|department)\b"]),
    ("BOOK_APPOINTMENT", [r"\b(book|appointment|slot|schedule (a )?visit|milna|dikhana|consult)\b"]),
    ("GREETING", [r"^\s*(hi|hello|hey|namaste|namaskar|good (morning|evening|afternoon))\b"]),
    ("THANKS", [r"\b(thanks|thank you|dhanyavad|shukriya)\b"]),
    ("AFFIRM", [r"^\s*(yes|y|haan|ha|ok|okay|confirm|sure|please do|book it|thik hai)\b"]),
    ("DENY", [r"^\s*(no|n|nahi|nahin|cancel it|not now|no thanks)\b"]),
]

SELECTION_RE = re.compile(r"^\s*(?:option\s*)?(\d{1,2})\s*$")


def detect_intent(message: str, stage: str, pending: dict | None) -> tuple[str, float]:
    low = (message or "").lower().strip()
    pending = pending or {}

    # numeric selection inside a flow
    if SELECTION_RE.match(low) and stage in {"AWAIT_DOCTOR_CHOICE", "AWAIT_SLOT_CHOICE", "AWAIT_SPECIALTY_CHOICE",
                                            "AWAIT_APPOINTMENT_CHOICE"}:
        return "SELECT_OPTION", 0.95

    for intent, patterns in INTENT_PATTERNS:
        for pat in patterns:
            if re.search(pat, low):
                if intent == "AFFIRM" and stage == "AWAIT_CONFIRMATION":
                    return "CONFIRM_BOOKING", 0.95
                if intent == "DENY" and stage in {"AWAIT_CONFIRMATION", "AWAIT_SLOT_CHOICE"}:
                    return "DECLINE", 0.9
                return intent, 0.8 if intent in {"BOOK_APPOINTMENT", "GREETING"} else 0.9

    if stage == "AWAIT_IDENTITY_NAME":
        if looks_like_concern(message):
            return "PROVIDE_CONCERN", 0.75
        if plausible_name(message):
            return "PROVIDE_NAME", 0.85
    if stage == "AWAIT_IDENTITY_PHONE":
        digits = re.sub(r"\D", "", low)
        if len(digits) >= 10:
            return "PROVIDE_PHONE", 0.9
    if stage in {"AWAIT_CONCERN", "IDLE"}:
        return "PROVIDE_CONCERN", 0.7
    return "GENERAL_QUERY", 0.4


# ==========================================================================
# NAME / CONCERN DISAMBIGUATION
# ==========================================================================
CONCERN_HINTS = {
    "fever", "pain", "ache", "cough", "cold", "rash", "vomit", "diarrh", "nausea", "bleed",
    "injur", "sore", "infection", "swelling", "burning", "discharge", "dizzy", "breath",
    "asthma", "sugar", "bp", "pressure", "migraine", "ulcer", "pregnan", "period", "itch",
    "allergy", "weakness", "fatigue", "chest", "stomach", "head", "throat", "ear", "eye",
    "skin", "knee", "joint", "back", "tooth", "toothache", "emergency", "problem", "symptom",
    "since", "suffering", "dawa", "bimari", "dard", "bukhar", "khansi", "chakkar",
}
CONCERN_LABELS = {"fever and cold", "knee pain", "skin rash", "book appointment",
                  "book urgent slot", "show my reports", "payment status", "talk to reception",
                  "show my records", "find a specialist", "remind me about my appointment",
                  "call 108", "notify my family", "no, other slots"}


def looks_like_concern(text: str) -> bool:
    """True when the sentence describes a health problem rather than a person's name."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if low in CONCERN_LABELS:
        return True
    if any(word in low for word in CONCERN_HINTS):
        return True
    if re.search(r"\b(have|has|having|feeling|feel|suffering from)\b", low):
        return True
    if re.search(r"\b(since|for)\s+\d+", low):
        return True
    if re.search(r"\b(doctor|appointment|slot|booking|report|bill|medicine)\b", low):
        return True
    # REMOVED: `return len(low.split()) >= 6`
    # That rule classified any long sentence as a medical concern, so replies
    # such as "no, please show me the other available slots" were captured as
    # symptoms and derailed the booking flow. A sentence being long is not
    # evidence of illness - only the vocabulary above (now including Hindi
    # terms via is_new_medical_concern) may claim a concern.
    return False


def is_new_medical_concern(text: str) -> bool:
    """True only when the message states an actual health problem.

    Used to (a) avoid treating operational replies as symptoms and (b) let a
    genuinely new complaint interrupt an in-progress flow instead of being
    answered with "please reply with a number".

    Deliberately conservative: it returns True only for real symptom/body-area
    vocabulary (English, Hindi, Hinglish or Devanagari), never for length,
    politeness or booking words.
    """
    t = (text or "").strip()
    if not t:
        return False
    low = " ".join(t.lower().split())

    # Commands, selections and short answers are never new concerns.
    if medical_language.is_operational(low):
        return False
    if len(low.split()) <= 2 and not any(ch.isdigit() for ch in low):
        # "yes", "ok", "1", "haan", "no thanks" etc. - never a fresh concern,
        # unless the local dictionary recognises it ("bukhar" -> fever).
        if not medical_language.normalise_medical_text(low)["understood"]:
            return False

    # Hindi / Hinglish / regional vocabulary ("bukhar", "ghutne mein dard").
    local = medical_language.normalise_medical_text(low)
    if local["understood"]:
        return True

    return any(word in low for word in CONCERN_HINTS)


def _looks_ambiguous(text: str) -> bool:
    """Terse input we could not understand: treat with caution.

    A short unrecognised message ("tavda lg gya") could be a local expression for
    something serious. When we cannot tell, we ask - and we remind the patient of
    the emergency number rather than staying silent about it.
    """
    return len((text or "").split()) <= 4


def plausible_name(text: str) -> bool:
    """A name is short, alphabetic and free of symptom vocabulary."""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    words = t.split()
    if len(words) > 5:
        return False
    digits = sum(ch.isdigit() for ch in t)
    if digits > 2:
        return False
    letters = re.sub(r"[^A-Za-z\u0900-\u097F]", "", t)
    if len(letters) < 2:
        return False
    if re.search(r"\b(my name is|i am|i'm|this is)\b", t.lower()):
        t = re.sub(r"\b(my name is|i am|i'm|this is)\b", " ", t, flags=re.IGNORECASE).strip()
        return plausible_name(t)
    if looks_like_concern(t) or is_new_medical_concern(t):
        return False
    return True


def clean_name(text: str) -> str:
    t = re.sub(r"\b(my name is|i am|i'm|this is)\b", " ", text or "", flags=re.IGNORECASE)
    return " ".join(t.split()).strip(" .,-").title() or (text or "").strip().title()


# ==========================================================================
# CONCERN EXTRACTION  ->  structured concern
# ==========================================================================
DURATION_RE = re.compile(
    r"(\d+\s*(?:day|days|week|weeks|month|months|year|years|hour|hours|din|hafte|mahine|saal))",
    re.IGNORECASE,
)
SEVERITY_WORDS = {
    "HIGH": ["severe", "unbearable", "extreme", "very bad", "worst", "tez"],
    "LOW": ["mild", "slight", "little", "halka"],
}


def extract_concern(db: Session, text: str) -> dict:
    """Turn free text into a structured concern + matched specialty.

    Hindi / Hinglish / regional wording is normalised locally first
    (`medical_language`), so "ghutne mein dard" scores Orthopaedics exactly like
    "knee pain" would, without spending a single Groq token. The patient's own
    words are always preserved in ``concern_text`` for the doctor and the audit
    trail; only the matching copy is normalised.
    """
    original = (text or "").strip()
    local = medical_language.normalise_medical_text(original)
    # keywords are matched against the normalised copy (original + canonical terms)
    low = local["normalized"] or original.lower()

    duration_match = DURATION_RE.search(original.lower()) or DURATION_RE.search(low)
    severity = "MODERATE"
    for level, words in SEVERITY_WORDS.items():
        if any(w in low for w in words):
            severity = level
            break

    keyword_rows = db.scalars(select(models.SpecialtyConcern)).all()
    scores: dict[int, int] = {}
    matched_keywords: list[str] = []
    for row in keyword_rows:
        if row.keyword.lower() in low:
            scores[row.specialty_id] = scores.get(row.specialty_id, 0) + (row.weight or 1)
            matched_keywords.append(row.keyword)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top = []
    for specialty_id, score in ranked[:3]:
        sp = db.get(models.Specialty, specialty_id)
        if sp:
            top.append({"specialty_id": sp.id, "specialty_name": sp.name, "score": score,
                        "fee": float(sp.consultation_fee or 0)})

    urgency = "LOW"
    if any(w in low for w in ["severe", "can't bear", "since 1 week", "getting worse"]):
        urgency = "MEDIUM"

    return {
        "concern_text": original,
        "keywords": sorted(set(matched_keywords)),
        "duration_text": duration_match.group(0) if duration_match else None,
        "severity": severity,
        "urgency": urgency,
        "candidate_specialties": top,
        # Audit trail for the local language layer. Both naming styles are
        # emitted ("canonicals"/"canonical_terms") because the routing helpers
        # and the earlier response contract read different keys - a mismatch here
        # silently zeroes the local confidence and loses the body-area fallback.
        "local": {
            "matched": local["matched"],
            "matched_terms": local["matched"],
            "canonicals": local["canonicals"],
            "canonical_terms": local["canonicals"],
            "understood": local["understood"],
            "fuzzy": local["fuzzy"],
            "language": local["language"],
        },
    }


# Body areas the hospital's specialty_concerns table has no keyword for. Without
# this, "pair me kheechav" (leg strain) had nothing to match on and fell through
# to the generic department. Body area -> the department that treats it.
BODY_AREA_SPECIALTY: dict[str, str] = {
    "leg": "Orthopaedics", "foot": "Orthopaedics", "heel": "Orthopaedics",
    "elbow": "Orthopaedics", "wrist": "Orthopaedics", "hip": "Orthopaedics",
    "joint": "Orthopaedics", "fracture": "Orthopaedics", "injury": "Orthopaedics",
    "eye": "Ophthalmology", "blurred": "Ophthalmology", "vision": "Ophthalmology",
    "cataract": "Ophthalmology",
    "ear": "ENT", "nose": "ENT", "sinus": "ENT", "hearing": "ENT", "snoring": "ENT",
    "tooth": "ENT", "toothache": "ENT", "mouth": "ENT", "bad breath": "ENT",
    "throat": "ENT", "cough": "General Medicine", "cold": "General Medicine",
    "skin": "Dermatology", "acne": "Dermatology", "eczema": "Dermatology",
    "fungal": "Dermatology", "hair fall": "Dermatology", "dandruff": "Dermatology",
    "allergy": "Dermatology", "wound": "Dermatology", "burn": "Dermatology",
    "mole": "Dermatology", "itching": "Dermatology", "rash": "Dermatology",
    "urine": "General Medicine", "thirst": "General Medicine", "weight": "General Medicine",
    "appetite": "General Medicine", "anemia": "General Medicine", "thyroid": "General Medicine",
    "infection": "General Medicine", "malaria": "General Medicine", "dengue": "General Medicine",
    "typhoid": "General Medicine", "flu": "General Medicine", "checkup": "General Medicine",
    "child": "Paediatrics", "baby": "Paediatrics", "infant": "Paediatrics",
    "teething": "Paediatrics", "vaccination": "Paediatrics",
    "pregnancy": "Obstetrics & Gynaecology", "periods": "Obstetrics & Gynaecology",
    "pcos": "Obstetrics & Gynaecology", "white discharge": "Obstetrics & Gynaecology",
    "breast": "Obstetrics & Gynaecology", "infertility": "Obstetrics & Gynaecology",
    "stress": "Psychiatry", "anxiety": "Psychiatry", "insomnia": "Psychiatry",
    "depression": "Psychiatry",
    "heart": "Cardiology", "palpitations": "Cardiology", "cholesterol": "Cardiology",
    "breathing": "Pulmonology", "breathless": "Pulmonology", "asthma": "Pulmonology",
    "wheezing": "Pulmonology", "TB": "Pulmonology", "smoking": "Pulmonology",
    "headache": "Neurology", "migraine": "Neurology", "dizziness": "Neurology",
    "vertigo": "Neurology", "seizure": "Neurology", "numbness": "Neurology",
    "paralysis": "Neurology", "sciatica": "Orthopaedics", "memory": "Neurology",
    "fainted": "Neurology", "unconscious": "Neurology",
}


def body_area_specialty(db: Session, canonicals: list[str]) -> dict | None:
    """Route a recognised body area / symptom when no DB keyword matched."""
    for term in canonicals:
        name = BODY_AREA_SPECIALTY.get(term)
        if not name:
            continue
        sp = db.scalar(select(models.Specialty).where(models.Specialty.name == name))
        if sp:
            return {"specialty_id": sp.id, "specialty_name": sp.name, "score": 1,
                    "fee": float(sp.consultation_fee or 0), "matched_by": "body_area"}
    return None


# Canonicals that outrank plain keyword scoring, in priority order. A child with
# fever is a Paediatrics case even though "fever" also scores General Medicine,
# and a pregnancy complaint belongs to Obstetrics & Gynaecology.
PRIORITY_CANONICALS: tuple[tuple[str, str], ...] = (
    ("pregnancy", "Obstetrics & Gynaecology"),
    ("womb", "Obstetrics & Gynaecology"),
    ("white discharge", "Obstetrics & Gynaecology"),
    ("periods", "Obstetrics & Gynaecology"),
    ("pcos", "Obstetrics & Gynaecology"),
    ("infertility", "Obstetrics & Gynaecology"),
    ("child", "Paediatrics"),
    ("baby", "Paediatrics"),
    ("infant", "Paediatrics"),
    ("teething", "Paediatrics"),
)


def priority_specialty(db: Session, canonicals: list[str]) -> dict | None:
    for canonical, name in PRIORITY_CANONICALS:
        if canonical in canonicals:
            sp = db.scalar(select(models.Specialty).where(models.Specialty.name == name))
            if sp:
                return {"specialty_id": sp.id, "specialty_name": sp.name, "score": 3,
                        "fee": float(sp.consultation_fee or 0), "matched_by": f"priority:{canonical}"}
    return None


def _bookable_doctor_count(db: Session, specialty_id: int) -> int:
    return int(db.scalar(
        select(func.count(models.Doctor.id)).where(
            models.Doctor.specialty_id == specialty_id,
            models.Doctor.is_active.is_(True),
            models.Doctor.is_available_for_ai_booking.is_(True),
        )
    ) or 0)


def alternate_specialty(tool_ctx: ToolContext, concern: dict, specialty: dict) -> dict | None:
    """The nearest department that actually has a bookable doctor.

    A master row can be the *correct* department and still have nobody attached
    to it yet (Pulmonology, Ophthalmology, ... in a fresh install). Replying
    "tell me more about the problem" there is wrong - the problem *was*
    understood. Offer the closest department that can really be booked and say
    so, instead of dropping the patient back into clarification.
    """
    db = tool_ctx.db
    wanted = specialty.get("specialty_id")
    given = specialty.get("specialty_name") or "that department"
    tried: set[int] = {wanted} if wanted else set()

    def note_for(name: str) -> str:
        return (f"*{given}* has no doctor available for online booking right now, so the closest "
                f"department I can book for you is *{name}*.")

    # 1. another department that matched the very same words
    for cand in concern.get("candidate_specialties") or []:
        cid = cand.get("specialty_id")
        if not cid or cid in tried:
            continue
        tried.add(cid)
        if _bookable_doctor_count(db, cid):
            return {**cand, "matched_by": "alternate", "note": note_for(cand.get("specialty_name"))}

    # 2. a general physician, who can refer onward
    for hint in ("General Medicine", "General Physician", "Family Medicine", "Internal Medicine"):
        row = db.scalar(select(models.Specialty).where(
            models.Specialty.name == hint, models.Specialty.is_active.is_(True)))
        if row and row.id not in tried and _bookable_doctor_count(db, row.id):
            return {"specialty_id": row.id, "specialty_name": row.name, "score": specialist_score(specialty),
                    "fee": float(row.consultation_fee or 0), "matched_by": "alternate",
                    "note": note_for(row.name)}

    # 3. anything at all that has a bookable doctor (keeps the patient moving)
    row = db.scalar(
        select(models.Specialty).join(models.Doctor, models.Doctor.specialty_id == models.Specialty.id)
        .where(models.Specialty.is_active.is_(True), models.Doctor.is_active.is_(True),
               models.Doctor.is_available_for_ai_booking.is_(True))
        .order_by(models.Specialty.id).limit(1)
    )
    if row and row.id not in tried:
        return {"specialty_id": row.id, "specialty_name": row.name, "score": specialist_score(specialty),
                "fee": float(row.consultation_fee or 0), "matched_by": "alternate",
                "note": note_for(row.name)}
    return None


def specialist_score(specialty: dict) -> int:
    try:
        return int(specialty.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def route_to_specialty(tool_ctx: ToolContext, concern: dict, conversation_id: int | None) -> dict | None:
    canonicals = (concern.get("local") or {}).get("canonicals") or []
    priority = priority_specialty(tool_ctx.db, canonicals)
    if priority:
        return priority
    candidates = concern.get("candidate_specialties") or []
    # the local layer recognised a body area (and may need to decide a tie)
    area = body_area_specialty(tool_ctx.db, canonicals)
    if area:
        if not candidates:
            return area
        # Several departments can share a keyword - "throat" is both ENT and
        # General Medicine, and the DB order used to decide that tie by accident.
        # When the scores are level, the recognised body area decides.
        if candidates[0]["specialty_id"] != area["specialty_id"] and candidates[0]["score"] <= 2:
            return area
    if candidates:
        return candidates[0]
    # fall back to keyword search over specialty metadata
    result = ai_tools.search_specialties(tool_ctx.db, tool_ctx, query=concern.get("concern_text", ""), limit=3)
    for sp in result.get("specialties", []):
        return {"specialty_id": sp["id"], "specialty_name": sp["name"], "score": 1,
                "fee": sp.get("consultation_fee", 0), "matched_by": "search"}
    return None


# ---------------------------------------------------------------------------
# HYBRID SEMANTIC FALLBACK
#
#   step 1  local dictionary + specialty keywords   (0 Groq tokens)
#   step 2  Groq semantic understanding             (only when step 1 is unsure)
#   step 3  clarification question                  (when neither is confident)
#
# Step 2 is never allowed to produce a diagnosis or a prescription - it may only
# answer "which body area / which symptoms did the patient mean?", and its answer
# is validated against the hospital's own vocabulary before it is used.
# ---------------------------------------------------------------------------
def known_medical_terms(db: Session) -> set[str]:
    """Every symptom/body-area term the hospital can route on."""
    try:
        db_terms = {k.lower() for k in db.scalars(select(models.SpecialtyConcern.keyword)).all() if k}
    except Exception:  # noqa: BLE001 - never block a patient on a lookup
        db_terms = set()
    return db_terms | medical_language.SYMPTOM_TERMS | set(medical_language.LOCAL_MEDICAL_TERMS.values())


# How confident the local layer must be before we answer without asking the AI.
LOCAL_CONFIDENCE_FLOOR = 0.6


def routing_mode(local: dict, text: str) -> str:
    """Who should understand this message: "local", "semantic" or "ask".

    This is the switch that keeps the balance the project wants - not zero AI
    usage, but AI where it actually helps:

      * commands and selections ("1", "yes", "cancel appointment")  -> local,
        never sent anywhere, zero tokens.
      * a clear local match (word OR typo, including Hindi/Hinglish) -> local.
      * a natural sentence the dictionary only partly understands, or a
        sentence-shaped message it does not understand at all -> semantic
        (Groq), because that is exactly where an LLM is worth its tokens.
      * a short unrecognised fragment -> ask the patient.
    """
    if medical_language.is_operational(text):
        return "local"

    confidence = medical_language.language_confidence(local)
    if confidence >= LOCAL_CONFIDENCE_FLOOR:
        return "local"

    if local.get("canonicals"):
        # Some vocabulary matched but not enough to be sure -> let the AI finish
        # the reading instead of guessing from a fragment.
        return "semantic"

    words = [w for w in (text or "").split() if w]
    if len(words) >= 3 or local.get("language") in {"hi", "hinglish"}:
        return "semantic"

    return "ask"


def understand_concern(db: Session, text: str, *, allow_llm: bool = True) -> dict:
    """Resolve a patient message into a structured concern.

    Order (see the module docstring): local vocabulary -> optional embeddings
    hint -> Groq semantic understanding -> clarification.

    Returns the same dict as ``extract_concern`` plus:

        "source"              "local" | "groq" | "embeddings" | "unknown"
        "needs_clarification" True when no confident routing was possible
        "route"               the routing_mode() decision for this message
    """
    concern = extract_concern(db, text)
    local = concern.get("local") or {}
    confident = bool(local.get("understood")) and bool(concern.get("candidate_specialties"))

    if confident:
        concern["source"] = "local"
        concern["route"] = "local"
        concern["needs_clarification"] = False
        return concern

    route = routing_mode(local, text)
    concern["route"] = route
    operational = medical_language.is_operational(text)

    # --- semantic fallback: natural sentences, partial matches, unknown slang ---
    if (allow_llm and not operational and route in {"semantic", "ask"}
            and settings.ai_mode in {"hybrid", "groq"}):
        try:
            from . import groq_engine
        except Exception:  # noqa: BLE001
            groq_engine = None  # type: ignore[assignment]
        if groq_engine is not None and groq_engine.is_available():
            try:
                interpretation = groq_engine.analyze_message(text)
            except Exception as exc:  # noqa: BLE001 - an LLM outage must never lose a patient
                # One line, not a traceback: an outage/timeout is a normal event,
                # and the patient must simply be asked instead of guessed at.
                log.warning("Semantic fallback unavailable (%s: %s); asking the patient instead",
                            type(exc).__name__, exc)
                interpretation = None
            rescued = _rescue_with_terms(db, text, interpretation, source="groq")
            if rescued is not None:
                return rescued

    # --- last resort: word-vector hint (off unless NLP_EMBEDDINGS=1) ---
    if not operational and route == "semantic":
        unknown = [t for t in re.findall(r"[A-Za-z][A-Za-z']+", text.lower())
                   if t not in medical_language.LOCAL_MEDICAL_TERMS
                   and len(t) >= 4 and t not in medical_language.CONTEXT_WORDS]
        hints = nlp_signals.embedding_suggest(unknown, medical_language.SYMPTOM_TERMS)
        if hints:
            rescued = _rescue_with_terms(db, text, {
                "canonical_terms": list(hints.values()),
                "concern": text.strip()[:120],
                "confidence": 0.6,
                "symptoms": list(hints.values()),
            }, source="embeddings")
            if rescued is not None:
                return rescued

    # --- step 3: we are not confident; ask instead of guessing ---
    concern["source"] = "local" if local.get("canonicals") else "unknown"
    concern["needs_clarification"] = not concern.get("candidate_specialties")
    return concern


def _rescue_with_terms(db: Session, text: str, interpretation: dict | None, *, source: str) -> dict | None:
    """Route a message using canonical terms supplied by an external reader.

    Every term is (a) normalised through the local dictionary and (b) validated
    against the hospital's own vocabulary before it is allowed to influence
    routing, so a hallucinated or clinical-sounding term can never become a
    department. Returns None when nothing usable came back.
    """
    if not interpretation:
        return None
    confidence = float(interpretation.get("confidence") or 0)
    if confidence < 0.4:                      # below this we would be guessing
        return None

    vocabulary = known_medical_terms(db)
    accepted: list[str] = []
    rejected: list[str] = []
    for term in (interpretation.get("canonical_terms") or [])[:10]:
        clean = str(term).strip().lower()
        if not clean:
            continue
        # normalise the model's wording through our own dictionary first
        for token in re.split(r"[^a-z\u0900-\u097F]+", clean):
            if token and token in medical_language.LOCAL_MEDICAL_TERMS:
                clean = medical_language.LOCAL_MEDICAL_TERMS[token]
                break
        if clean in vocabulary:
            accepted.append(clean)
        else:
            rejected.append(clean)

    if not accepted:
        log.info("Semantic fallback returned no usable terms for %r (rejected: %s)",
                 text[:60], rejected[:5])
        return None

    augmented = f"{text} {' '.join(accepted)}"
    rescue = extract_concern(db, augmented)
    rescue["concern_text"] = text.strip()          # keep the patient's own words
    rescue["semantic"] = {
        "concern": interpretation.get("concern") or interpretation.get("summary"),
        "body_area": interpretation.get("body_area"),
        "symptoms": interpretation.get("symptoms"),
        "language": interpretation.get("language"),
        "urgency_hint": interpretation.get("urgency_hint"),
        "confidence": confidence,
        "accepted_terms": accepted,
        "rejected_terms": rejected,
        "reader": source,
        "model": getattr(settings, "groq_model", None) if source == "groq" else "glove-embeddings",
    }
    if rescue.get("candidate_specialties"):
        rescue["source"] = source
        rescue["needs_clarification"] = False
        log.info("%s routed %r -> %s", source, text[:80],
                 rescue["candidate_specialties"][0]["specialty_name"])
        return rescue
    return None


# ==========================================================================
# CONVERSATION PLUMBING
# ==========================================================================
def get_or_create_conversation(db: Session, *, conversation_id: int | None, session_id: str | None,
                               channel: str, user_id: int | None) -> models.AIConversation:
    conv = None
    if conversation_id:
        conv = db.get(models.AIConversation, conversation_id)
    if not conv and session_id:
        conv = db.scalar(select(models.AIConversation).where(models.AIConversation.session_id == session_id))
    if not conv:
        conv = models.AIConversation(
            session_id=session_id or secrets.token_urlsafe(18),
            channel=channel,
            user_id=user_id,
            status="ACTIVE",
            stage="IDLE",
            intent_history=[],
            started_at=utcnow(),
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


def resolve_patient(db: Session, conv: models.AIConversation, *, user_id: int | None,
                    phone: str | None, name: str | None) -> models.Patient | None:
    if conv.patient_id:
        return db.get(models.Patient, conv.patient_id)
    if user_id:
        patient = db.scalar(select(models.Patient).where(models.Patient.user_id == user_id))
        if patient:
            _link_patient(db, conv, patient, "EXISTING")
            return patient
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        patient = db.scalar(
            select(models.Patient).where(models.Patient.phone.like(f"%{digits}"))
            .order_by(models.Patient.created_at.desc()).limit(1)
        ) if digits else None
        if patient:
            _link_patient(db, conv, patient, "EXISTING")
            return patient
    return None


def _link_patient(db: Session, conv: models.AIConversation, patient: models.Patient, ptype: str) -> None:
    conv.patient_id = patient.id
    conv.patient_type = ptype
    db.commit()
    # a patient who uploaded documents before telling us who they are must not
    # lose them: attach every pending chat upload to the patient record now
    try:
        from . import chat_documents          # local import: avoids a service cycle
        chat_documents.link_pending(db, conv, patient)
    except Exception:                          # never break a chat reply because of this
        log.exception("Could not link pending chat uploads to %s", getattr(patient, "patient_code", "?"))
    return None


def create_patient_from_chat(db: Session, name: str, phone: str, age: int | None = None,
                             gender: str | None = None) -> models.Patient:
    from . import patients as patient_service  # local import (service bootstrapping)

    return patient_service.create_patient_record(
        db, full_name=name, phone=phone, age=age, gender=gender, source="AI",
    )


def add_message(db: Session, conv: models.AIConversation, role: str, content: str, *,
                intent: str | None = None, confidence: float | None = None,
                tool_calls: list | None = None, structured: dict | None = None,
                latency_ms: int | None = None, safety_flag: str | None = None) -> models.AIMessage:
    msg = models.AIMessage(
        conversation_id=conv.id, role=role, content=content, intent=intent,
        confidence=confidence, tool_calls=tool_calls, structured=structured,
        latency_ms=latency_ms, safety_flag=safety_flag,
    )
    db.add(msg)
    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = utcnow()
    if role == "user" and intent:
        history = list(conv.intent_history or [])
        history.append({"intent": intent, "at": utcnow().isoformat()})
        conv.intent_history = history[-25:]
        conv.primary_intent = intent
    db.commit()
    db.refresh(msg)
    return msg


def log_tool_calls(db: Session, conv: models.AIConversation, calls: list[dict]) -> None:
    if calls:
        conv.tool_call_count = (conv.tool_call_count or 0) + len(calls)
        db.commit()


def log_routing(db: Session, conv: models.AIConversation, concern: dict, specialty: dict | None,
                doctors: list[dict], selected_doctor: dict | None = None) -> None:
    db.add(models.AIRoutingLog(
        conversation_id=conv.id,
        candidate_specialties=concern.get("candidate_specialties", []),
        selected_specialty_id=(specialty or {}).get("specialty_id"),
        candidate_doctors=[{"id": d.get("id"), "name": d.get("name")} for d in doctors],
        selected_doctor_id=(selected_doctor or {}).get("id"),
        match_score=(specialty or {}).get("score", 0),
        decision=f"{concern.get('concern_text', '')[:120]} -> {(specialty or {}).get('specialty_name', 'unmatched')}",
    ))
    db.commit()


def escalate(db: Session, conv: models.AIConversation, level: str, reason: str, detail: str | None = None,
             patient: models.Patient | None = None) -> models.AIEscalation:
    esc = models.AIEscalation(
        conversation_id=conv.id, patient_id=patient.id if patient else conv.patient_id,
        level=level, reason=reason[:255], detail=detail, status="OPEN",
    )
    db.add(esc)
    conv.escalation_count = (conv.escalation_count or 0) + 1
    conv.status = "ESCALATED"
    if level in {"EMERGENCY", "DOCTOR"}:
        conv.stage = "ESCALATED_DOCTOR"
    else:
        conv.stage = "ESCALATED_RECEPTION"
    db.commit()
    db.refresh(esc)

    template = {"EMERGENCY": "EMERGENCY_ALERT", "DOCTOR": "ESCALATION_DOCTOR"}.get(level, "ESCALATION_RECEPTION")
    context = {
        "patient_name": patient.full_name if patient else "Unknown caller",
        "phone": patient.phone if patient else "unknown",
        "reason": reason,
    }
    if patient:
        for user in db.scalars(select(models.User).where(models.User.role.has(name="RECEPTIONIST"))).all():
            if user.phone:
                notify.send_whatsapp(db, to_number=user.phone, template=template,
                                     body=notify.render(template, context), patient_id=patient.id)
        notify.queue_notification(db, template=template, context=context, channel="IN_APP", patient=patient)
    db.add(models.AuditLog(action="AI_ESCALATION", resource="ai_escalation", resource_id=str(esc.id),
                           user_role="AI", new_value={"level": level, "reason": reason}))
    db.commit()
    return esc


# ==========================================================================
# PATIENT CONTEXT SUMMARY  (AI memory / context retrieval)
# ==========================================================================
def build_patient_context_summary(db: Session, patient: models.Patient) -> dict:
    appts = db.scalars(
        select(models.Appointment).where(models.Appointment.patient_id == patient.id)
        .order_by(models.Appointment.appointment_date.desc()).limit(5)
    ).all()
    upcoming = [a for a in appts if a.appointment_date >= date.today() and a.status in {"PENDING", "CONFIRMED"}]
    concerns = db.scalars(
        select(models.AIConcern).where(models.AIConcern.patient_id == patient.id)
        .order_by(models.AIConcern.created_at.desc()).limit(5)
    ).all()
    consultations = db.scalars(
        select(models.Consultation).where(models.Consultation.patient_id == patient.id)
        .order_by(models.Consultation.created_at.desc()).limit(3)
    ).all()
    prescriptions = db.scalars(
        select(models.Prescription).where(models.Prescription.patient_id == patient.id)
        .order_by(models.Prescription.issued_at.desc()).limit(2)
    ).all()
    files = db.scalars(
        select(models.MedicalFile).where(models.MedicalFile.patient_id == patient.id)
        .order_by(models.MedicalFile.created_at.desc()).limit(3)
    ).all()
    outstanding = db.scalar(
        select(func.coalesce(func.sum(models.Invoice.balance_amount), 0)).where(
            models.Invoice.patient_id == patient.id, models.Invoice.status.in_(["UNPAID", "PARTIAL"]))
    ) or 0

    summary = {
        "patient": {"id": patient.id, "code": patient.patient_code, "name": patient.full_name,
                    "age": patient.age, "gender": patient.gender, "phone": patient.phone,
                    "allergies": patient.allergies, "chronic_conditions": patient.chronic_conditions},
        "current_concerns": [{"concern": c.concern_text, "urgency": c.urgency,
                              "specialty_id": c.specialty_id, "status": c.status} for c in concerns],
        "previous_appointments": [{"date": a.appointment_date.isoformat(), "doctor_id": a.doctor_id,
                                   "status": a.status, "reason": a.reason} for a in appts],
        "upcoming_appointments": [{"id": a.id, "code": a.appointment_code, "date": a.appointment_date.isoformat(),
                                   "time": a.start_time.strftime("%H:%M"),
                                   "doctor": a.doctor.full_name if a.doctor else None} for a in upcoming],
        "recent_consultations": [{"date": c.completed_at.isoformat() if c.completed_at else None,
                                  "diagnosis": c.diagnosis, "follow_up_date":
                                  c.follow_up_date.isoformat() if c.follow_up_date else None} for c in consultations],
        "recent_prescriptions": [{"code": p.prescription_code,
                                  "issued_at": p.issued_at.isoformat()} for p in prescriptions],
        "files": [{"title": f.title, "category": f.category} for f in files],
        "outstanding_amount": float(outstanding),
    }
    text_summary = (
        f"{patient.full_name} ({patient.patient_code}), {patient.age or '-'}y {patient.gender or ''}. "
        f"Allergies: {patient.allergies or 'none recorded'}. "
        f"Active concerns: {', '.join(c.concern_text[:60] for c in concerns) or 'none'}. "
        f"Upcoming: {len(upcoming)} appointment(s). Outstanding: Rs {float(outstanding):.2f}."
    )
    summary["text_summary"] = text_summary
    return summary


# ==========================================================================
# RESPONSE COMPOSITION
# ==========================================================================
def _slots_to_payload(slot_rows: list[dict]) -> list[dict]:
    return [{
        "slot_id": r["slot_id"], "doctor_id": r["doctor_id"], "doctor_name": r["doctor_name"],
        "specialty_name": r.get("specialty_name"), "label": r["label"],
        "slot_date": r["slot_date"], "start_time": r["start_time"], "end_time": r["end_time"],
        "fee": r["fee"],
    } for r in slot_rows]


def _numbered_list(items: list[str]) -> str:
    return "\n".join(f"*{i + 1}.* {text}" for i, text in enumerate(items))


def _llm_polish(system: str, factual_reply: str, patient_context: str) -> str | None:
    """Optional natural-language rewrite of a deterministic reply.

    Only active in AI_MODE=hybrid. The facts (names, times, fees, option
    numbers, patient codes) always come from the deterministic engine - Groq
    only makes the wording friendlier. Any provider problem returns None and
    the original reply is used, so the assistant never breaks.
    """
    if settings.ai_mode != "hybrid" or not settings.ai_polish:
        return None
    try:
        from . import groq_engine

        if not groq_engine.is_available():
            return None
        return groq_engine.polish_reply(factual_reply, context=patient_context)
    except Exception:  # noqa: BLE001
        log.exception("Groq reply polish failed; keeping the deterministic reply")
        return None


# ==========================================================================
# MAIN HANDLER
# ==========================================================================
def _doctor_list_reply(concern: dict, routed_specialty: dict, specialty: dict,
                       routing_note: str | None, doctors: list[dict]) -> str:
    """Build the doctor list after explicit patient consent.

    All doctor facts and review text come from the database. Review comments
    are displayed exactly as returned by the tool and are never generated by AI.
    """
    lines = []
    for d in doctors:
        line = (
            f"{d['name']} - {d['qualifications'] or d['specialty']}, "
            f"{d['experience_years'] or 0} yrs, Rs {d['fee']:.0f}"
        )
        if d.get("rating_count"):
            line += f", ⭐ {d.get('rating', 0):.1f}/5 ({d['rating_count']} ratings)"
        elif d.get("rating"):
            line += f", ⭐ {d.get('rating', 0):.1f}/5"
        if d.get("next_available"):
            line += f", next: {d['next_available']}"

        reviews = d.get("reviews") or []
        if reviews:
            review_lines = []
            for review in reviews[:3]:
                stars = "⭐" * int(review.get("rating") or 0)
                comment = review.get("comment", "").strip()
                if comment:
                    review_lines.append(f"   • {stars} {comment}")
            if review_lines:
                line += "\n   Verified patient reviews:\n" + "\n".join(review_lines)
        else:
            line += "\n   Written reviews: No verified written reviews available."

        lines.append(line)

    intro = (routing_note + "\n\n") if routing_note else ""
    if routing_note:
        seen_by = (f"This is normally seen by *{routed_specialty['specialty_name']}*, and I can book "
                   f"you with *{specialty['specialty_name']}*. ")
    else:
        seen_by = f"This is normally seen by *{specialty['specialty_name']}*. "

    return (
        intro
        + seen_by
        + f"Available doctors:\n\n{_numbered_list(lines)}\n\n"
        "Reply with the number of the doctor you would like to see. "
        "(A consultation with the doctor will confirm the diagnosis - I only help with the booking.)"
    )


def handle_message(db: Session, payload, *, user=None) -> dict:
    started = time.perf_counter()
    text = (payload.message or "").strip()
    conv = get_or_create_conversation(
        db, conversation_id=payload.conversation_id, session_id=payload.session_id,
        channel=payload.channel, user_id=getattr(user, "id", None),
    )
    patient = resolve_patient(db, conv, user_id=getattr(user, "id", None),
                              phone=payload.phone, name=payload.name)
    pending = copy.deepcopy(conv.pending_json or {})   # private copy (JSON mutation safety)
    if payload.name and not pending.get("draft_name"):
        pending["draft_name"] = payload.name
    if payload.phone:
        pending["draft_phone"] = payload.phone

    safety = scan_safety(text)

    # ------------------------------------------------------------------
    # LLM DISPATCH
    # The deterministic pipeline below is the authoritative front desk: it
    # owns the safety layer, intent detection, concern extraction, specialty
    # routing, the slot engine and booking, and it never invents data.
    # It therefore always runs, with or without an API key.
    #
    #   AI_MODE=local   deterministic engine only            (no key needed)
    #   AI_MODE=hybrid  deterministic flow + Groq re-phrases  (default w/ key)
    #   AI_MODE=groq    full Groq tool-calling conversation   (opt-in)
    # ------------------------------------------------------------------
    if settings.ai_mode == "groq":
        from . import groq_engine

        if groq_engine.is_available():
            try:
                return groq_engine.chat(
                    db, conv, patient, text,
                    user_id=getattr(user, "id", None),
                    channel=payload.channel,
                    safety=safety,
                    add_message=add_message,
                    escalate=escalate,
                )
            except Exception:  # noqa: BLE001 - never lose the front desk
                log.exception("Groq turn failed; falling back to the deterministic engine")
        else:
            log.warning("AI_MODE=groq but the Groq client/API key is unavailable - "
                        "using the deterministic engine")

    intent, confidence = detect_intent(text, conv.stage, pending)

    tool_ctx = ToolContext(db, patient_id=conv.patient_id, conversation_id=conv.id,
                           user_id=getattr(user, "id", None), channel=payload.channel)

    user_msg = add_message(db, conv, "user", text, intent=intent, confidence=confidence,
                           safety_flag=safety.get("flag"))
    tools_used: list[str] = []

    def call_tool(name: str, **kwargs):
        tools_used.append(name)
        return ai_tools.invoke_tool(db, tool_ctx, name, kwargs)

    response: dict = {
        "conversation_id": conv.id,
        "session_id": conv.session_id,
        "reply": "",
        "intent": intent,
        "confidence": confidence,
        "stage": conv.stage,
        "patient": None,
        "concern": None,
        "specialty": None,
        "doctors": [],
        "slots": [],
        "appointment": None,
        "prescriptions": [],
        "files": [],
        "payments": [],
        "escalation": None,
        "safety": safety,
        "tools_used": tools_used,
        "quick_replies": [],
    }

    # ------------------------------------------------------------------
    # 1. SAFETY FIRST
    # ------------------------------------------------------------------
    if safety["level"] == "EMERGENCY":
        esc = escalate(db, conv, "EMERGENCY", safety.get("matched") or "emergency keywords detected",
                       detail=text, patient=patient)
        # Informational inbox copy for the reception dashboard. escalate() has
        # already alerted the human team; this is a belt-and-braces extra and
        # must never be able to break the emergency path.
        if patient:
            try:
                call_tool("send_notification", template="EMERGENCY_ALERT", channel="IN_APP",
                          message=(f"EMERGENCY: {patient.full_name}"
                                   f"{' (' + patient.phone + ')' if patient.phone else ''} - {text[:120]}"),
                          patient_id=patient.id)
            except Exception:  # noqa: BLE001
                log.exception("Emergency inbox copy failed (escalation already recorded)")
        pending["draft_concern"] = text.strip()
        if patient:
            pending.pop("draft_concern", None)
            conv.stage = "AWAIT_CONCERN"
        else:
            conv.stage = "AWAIT_IDENTITY_NAME"
        conv.pending_json = dict(pending)
        db.commit()
        tail = ("" if patient else
                "\n\nIf you would also like a priority appointment kept ready at the hospital, "
                "reply with your *full name* and I will hold the next available slot.")
        response.update({
            "reply": DEFAULT_EMERGENCY_REPLY + tail,
            "stage": conv.stage,
            "escalation": {"level": "EMERGENCY", "id": esc.id, "reason": esc.reason},
            "quick_replies": ["Call 108", "Notify my family", "Book urgent slot"],
            "safety": {**safety, "action": "emergency_escalated"},
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if safety["level"] == "RESTRICTED":
        if patient:
            call_tool("send_notification", template="CUSTOM", channel="IN_APP",
                      message=f"Patient asked: {text[:150]}. Please review.", patient_id=patient.id)
        response.update({
            "reply": (
                f"{SAFETY_DISCLAIMER}\n\n"
                "What I *can* do right now:\n"
                "* find the right specialist for your symptom\n"
                "* book the earliest available slot\n"
                "* pull up your reports, prescriptions or bills"
            ),
            "stage": "AWAIT_CONCERN" if not patient else conv.stage,
            "quick_replies": ["Find a specialist", "Book appointment", "Show my reports"],
            "safety": {**safety, "action": "restricted_refused"},
        })
        if safety.get("escalate") == "DOCTOR" and patient:
            escalate(db, conv, "DOCTOR", safety.get("matched") or "urgent symptom", detail=text, patient=patient)
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 1b. URGENT (not emergency): alert a doctor, then still help the patient
    # ------------------------------------------------------------------
    if safety["level"] == "HIGH":
        esc = escalate(db, conv, "DOCTOR", safety.get("matched") or "urgent symptom",
                       detail=text, patient=patient)
        response["escalation"] = {"level": "DOCTOR", "id": esc.id, "reason": esc.reason}
        response["safety"] = {**safety, "action": "urgent_escalated"}
        log.info("Urgent symptom escalated to a doctor: %r", (safety.get("matched") or "")[:60])

    # ------------------------------------------------------------------
    # 2. HUMAN / RECEPTION ESCALATION
    # ------------------------------------------------------------------
    if intent == "HUMAN_ESCALATION":
        esc = escalate(db, conv, "RECEPTION", "patient requested human assistance", detail=text, patient=patient)
        response.update({
            "reply": ("Of course - I am connecting you with our reception team. "
                      "They will reach out shortly. If this is urgent, please call the hospital desk directly."),
            "escalation": {"level": "RECEPTION", "id": esc.id, "reason": esc.reason},
            "stage": conv.stage,
            "quick_replies": ["Book appointment anyway", "Show my appointment"],
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 3. STATE MACHINE
    # ------------------------------------------------------------------
    if conv.stage in {"AWAIT_IDENTITY_NAME"}:
        if is_new_medical_concern(text) or not plausible_name(text):
            # the patient described the problem (again) instead of giving a name - keep the concern
            if is_new_medical_concern(text):
                pending["draft_concern"] = text.strip()
                concern = understand_concern(db, text)
                pending["concern"] = concern
                response["concern"] = {
                    "concern_text": concern["concern_text"], "keywords": concern["keywords"],
                    "specialty_id": None, "specialty_name": None,
                    "urgency": concern["urgency"], "severity": concern["severity"],
                }
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_IDENTITY_NAME"
            db.commit()
            noted = (f"I have noted your concern: \u201c{pending['draft_concern']}\u201d.\n\n"
                     if pending.get("draft_concern") else "")
            response.update({
                "reply": (f"{noted}Before I can book a slot I need to identify you in our records - "
                          "could you please share your *full name* (only the name, for example "
                          "'Sunil Kumar')."),
                "stage": conv.stage,
                "quick_replies": [],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response
        pending["draft_name"] = clean_name(text)
        pending["draft_concern"] = pending.get("draft_concern") or ""
        conv.pending_json = dict(pending)
        conv.stage = "AWAIT_IDENTITY_PHONE"
        db.commit()
        response.update({
            "reply": f"Nice to meet you, {pending['draft_name']}. What is your 10-digit mobile number? "
                     "It lets me find your records and send WhatsApp confirmations.",
            "stage": conv.stage,
            "quick_replies": [],
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if conv.stage == "AWAIT_IDENTITY_PHONE":
        digits = re.sub(r"\D", "", text)[-10:] if len(re.sub(r"\D", "", text)) > 10 else re.sub(r"\D", "", text)
        if len(re.sub(r"\D", "", text)) == 11 and re.sub(r"\D", "", text).startswith("0"):
            digits = re.sub(r"\D", "", text)[1:]
        if len(digits) < 10:
            response.update({
                "reply": "That does not look like a complete mobile number. Please share a 10-digit number, "
                         "for example 9876543210.",
                "stage": conv.stage,
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response
        phone = digits[-10:]
        existing = db.scalar(select(models.Patient).where(models.Patient.phone.like(f"%{phone}"))
                             .order_by(models.Patient.created_at.desc()).limit(1))
        if existing:
            _link_patient(db, conv, existing, "EXISTING")
            patient = existing
            tool_ctx.patient_id = patient.id
            response["patient"] = {"id": patient.id, "name": patient.full_name,
                                   "patient_code": patient.patient_code, "type": "EXISTING"}
            response["reply"] = (f"Welcome back, {patient.full_name}! I found your records "
                                 f"({patient.patient_code}). How can I help you today?")
        else:
            patient = create_patient_from_chat(db, pending.get("draft_name", "Patient"), phone)
            _link_patient(db, conv, patient, "NEW")
            tool_ctx.patient_id = patient.id
            response["patient"] = {"id": patient.id, "name": patient.full_name,
                                   "patient_code": patient.patient_code, "type": "NEW"}
            response["reply"] = (f"Thank you! I created your patient ID {patient.patient_code} - "
                                 f"welcome to Vijay Vargiya Group of Hospitals. "
                                 "Please tell me your health concern in your own words.")
        conv.stage = "AWAIT_CONCERN"
        db.commit()
        response["stage"] = conv.stage
        response["quick_replies"] = ["Book appointment", "Fever and cold", "Show my records"]

        draft = (pending.get("draft_concern") or "").strip()
        if draft:
            # the patient already described the problem before identification - route it now
            text = draft
            intent, confidence = detect_intent(text, conv.stage, pending)
            response["intent"] = intent
            response["confidence"] = confidence
            response["_note"] = (f"Thank you, {patient.full_name}. I have noted your concern: "
                                 f"\u201c{draft}\u201d.")
        else:
            response["reply"] = (f"Thank you! I created your patient ID {patient.patient_code} - "
                                 f"welcome to Vijay Vargiya Group of Hospitals. "
                                 "Please tell me your health concern in your own words.")
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

    # ------------------------------------------------------------------
    # 4. IDENTITY NEEDED FOR PATIENT-SPECIFIC TASKS
    # ------------------------------------------------------------------
    patient_specific = {
        "APPOINTMENT_STATUS", "PRESCRIPTION_LOOKUP", "MEDICAL_FILE_LOOKUP", "PAYMENT_STATUS",
        "INVOICE_LOOKUP", "CANCEL_APPOINTMENT", "RESCHEDULE_APPOINTMENT",
    }
    if intent in patient_specific and not patient:
        conv.stage = "AWAIT_IDENTITY_NAME"
        db.commit()
        response.update({
            "reply": ("I can pull that up - I just need to identify you first. "
                      "May I have your full name, please?"),
            "stage": conv.stage,
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 4b. FLOW: patient answers "Should I show the doctors?"
    # ------------------------------------------------------------------
    if conv.stage == "AWAIT_DOCTOR_CONSENT":
        said_yes = intent == "AFFIRM"
        said_no = intent in {"DENY", "DECLINE"}
        new_problem = (not said_yes) and (not said_no) and is_new_medical_concern(text)

        if not said_yes and not said_no and not new_problem and intent in {"DOCTOR_SEARCH", "BOOK_APPOINTMENT"}:
            said_yes = True          # "show doctors" / "book appointment" also means yes

        if said_yes:
            doctors = pending.get("doctors", [])
            specialty = pending.get("specialty") or {}
            routed_specialty = pending.get("routed_specialty") or specialty
            conv.stage = "AWAIT_DOCTOR_CHOICE"
            db.commit()
            response.update({
                "reply": _doctor_list_reply(pending.get("concern") or {}, routed_specialty, specialty,
                                            pending.get("routing_note"), doctors),
                "stage": conv.stage,
                "doctors": doctors,
                "quick_replies": [str(i + 1) for i in range(min(3, len(doctors)))],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        if said_no:
            for key in ("doctors", "specialty", "routed_specialty", "routing_note", "concern", "draft_concern"):
                pending.pop(key, None)
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_CONCERN"
            db.commit()
            response.update({
                "reply": "No problem. Tell me whenever you want a doctor suggestion, or ask me anything else.",
                "stage": conv.stage,
                "quick_replies": ["Book appointment", "Show my reports", "Payment status"],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        if new_problem:
            # a different problem was typed, so forget the old routing and route the new one
            for key in ("doctors", "specialty", "routed_specialty", "routing_note", "concern", "draft_concern"):
                pending.pop(key, None)
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_CONCERN"
            db.commit()
        else:
            response.update({
                "reply": "Would you like me to show the available doctors? Please reply *yes* or *no*.",
                "stage": conv.stage,
                "quick_replies": ["Yes, show doctors", "No, not now"],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

    # ------------------------------------------------------------------
    # 5. FLOW: doctor selection
    # ------------------------------------------------------------------
    doctors = pending.get("doctors", [])
    selected = None                      # only set by a genuine doctor choice this turn
    if conv.stage == "AWAIT_DOCTOR_CHOICE" and intent in {"SELECT_OPTION", "GENERAL_QUERY", "PROVIDE_CONCERN",
                                                          "DOCTOR_SEARCH", "BOOK_APPOINTMENT"}:
        if intent == "SELECT_OPTION":
            index = int(SELECTION_RE.match(text).group(1)) - 1
            if 0 <= index < len(doctors):
                selected = doctors[index]
        else:
            low = text.lower()
            selected = next((d for d in doctors if d["name"].lower() in low), None)

        if not selected and is_new_medical_concern(text):
            # A NEW COMPLAINT INTERRUPTS THE DOCTOR SELECTION.
            # Until now this reply ("I have knee pain since yesterday") was
            # answered with "please reply with the number of the doctor", which
            # trapped patients whose problem had changed or who had answered the
            # wrong question. The new concern wins: clear the pending routing and
            # fall through to section 9 to route the fresh concern properly.
            for key in ("doctors", "specialty", "concern", "selected_doctor", "slots_cache",
                        "slot_id", "hold_token", "chosen_slot_label", "draft_concern"):
                pending.pop(key, None)
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_CONCERN"
            db.commit()
            log.info("New concern interrupted AWAIT_DOCTOR_CHOICE: %r", text[:80])
        elif not selected:
            response.update({
                "reply": "Please reply with the number of the doctor you would like, e.g. *1*.",
                "stage": conv.stage,
                "doctors": doctors,
                "quick_replies": [str(i + 1) for i in range(min(3, len(doctors)))],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

    if selected:
        pending["selected_doctor"] = selected
        conv.pending_json = dict(pending)
        conv.stage = "AWAIT_SLOT_CHOICE"
        db.commit()
        slot_result = call_tool("get_available_slots", doctor_id=selected["id"], days_ahead=14, limit=6)
        slot_rows = slot_result.get("slots", [])
        response["doctors"] = doctors
        response["slots"] = _slots_to_payload([{
            **s, "slot_date": date.fromisoformat(s["slot_date"]),
            "start_time": datetime.strptime(s["start_time"], "%H:%M").time(),
            "end_time": datetime.strptime(s["end_time"], "%H:%M").time(),
        } for s in slot_rows])
        response["stage"] = conv.stage
        if slot_rows:
            lines = [f"{s['label']} - Rs {s['fee']:.0f}" for s in slot_rows[:5]]
            response["reply"] = (f"{selected['name']} ({selected.get('specialty')}) has these open slots:\n\n"
                                 f"{_numbered_list(lines)}\n\nReply with the number of a slot to hold it "
                                 f"for {settings.slot_hold_minutes} minutes.")
            response["quick_replies"] = [str(i + 1) for i in range(min(3, len(slot_rows)))]
        else:
            response["reply"] = (f"{selected['name']} has no open slots in the next two weeks. "
                                 "Would you like another doctor from the same specialty?")
            conv.stage = "AWAIT_DOCTOR_CHOICE"
            db.commit()
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 6. FLOW: slot selection + hold
    # ------------------------------------------------------------------
    if conv.stage == "AWAIT_SLOT_CHOICE" and intent in {"SELECT_OPTION", "GENERAL_QUERY",
                                                        "PROVIDE_CONCERN", "BOOK_APPOINTMENT", "CONFIRM_BOOKING"}:
        slot_rows = pending.get("slots_cache") or []
        if not slot_rows:
            doctor = pending.get("selected_doctor") or {}
            slot_result = call_tool("get_available_slots", doctor_id=doctor.get("id"), days_ahead=14, limit=6)
            slot_rows = [{
                **s, "slot_date": date.fromisoformat(s["slot_date"]),
                "start_time": datetime.strptime(s["start_time"], "%H:%M").time(),
                "end_time": datetime.strptime(s["end_time"], "%H:%M").time(),
            } for s in slot_result.get("slots", [])]
            pending["slots_cache"] = [{
                **s, "slot_date": s["slot_date"].isoformat(),
                "start_time": s["start_time"].strftime("%H:%M"),
                "end_time": s["end_time"].strftime("%H:%M"),
            } for s in slot_rows]
            conv.pending_json = dict(pending)
            db.commit()

        chosen = None
        if payload.selected_slot_id:
            chosen = next((s for s in slot_rows if s["slot_id"] == payload.selected_slot_id), None)
        if not chosen and intent == "SELECT_OPTION":
            index = int(SELECTION_RE.match(text).group(1)) - 1
            if 0 <= index < len(slot_rows):
                chosen = slot_rows[index]
        if not chosen:
            for s in slot_rows:
                if s["start_time"].strftime("%H:%M") in text or s["label"].lower() in text.lower():
                    chosen = s
                    break
        if not chosen:
            response.update({
                "reply": "Which slot works for you? Reply with the slot number.",
                "stage": conv.stage,
                "slots": _slots_to_payload(slot_rows),
                "quick_replies": [str(i + 1) for i in range(min(3, len(slot_rows)))],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        if not patient:
            conv.stage = "AWAIT_IDENTITY_NAME"
            db.commit()
            response.update({"reply": "Great choice! To confirm the booking I need your full name first.",
                             "stage": conv.stage})
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        hold = call_tool("hold_slot", slot_id=chosen["slot_id"], minutes=settings.slot_hold_minutes)
        if not hold.get("success"):
            response.update({
                "reply": f"Sorry, that slot was just taken ({hold.get('error')}). Here are other options:",
                "stage": conv.stage,
                "slots": _slots_to_payload(hold.get("alternatives") or []),
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        pending["hold_token"] = hold["hold_token"]
        pending["slot_id"] = chosen["slot_id"]
        pending["chosen_slot_label"] = hold["label"]
        conv.pending_json = dict(pending)
        conv.stage = "AWAIT_CONFIRMATION"
        db.commit()
        response.update({
            "reply": (f"I have held *{hold['label']}* with {doc_label(chosen['doctor_name'])} for you "
                      f"(expires in {settings.slot_hold_minutes} minutes).\n\n"
                      f"Consultation fee: Rs {chosen['fee']:.0f}.\n\n"
                      "Shall I confirm the booking? Reply *yes* to confirm or *no* to pick another slot."),
            "stage": conv.stage,
            "quick_replies": ["Yes, confirm", "No, other slots"],
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 7. FLOW: confirmation -> booking
    # ------------------------------------------------------------------
    if conv.stage == "AWAIT_CONFIRMATION":
        if intent in {"CONFIRM_BOOKING", "AFFIRM"}:
            concern = pending.get("concern") or {}
            conv.booking_attempts = (conv.booking_attempts or 0) + 1
            db.commit()
            result = call_tool(
                "book_appointment",
                slot_id=pending.get("slot_id"),
                patient_id=patient.id if patient else None,
                reason=concern.get("concern_text"),
                concern_summary=concern.get("concern_text"),
                hold_token=pending.get("hold_token"),
            )
            if result.get("success"):
                conv.successful_bookings = (conv.successful_bookings or 0) + 1
                conv.stage = "POST_BOOK"
                conv.status = "ACTIVE"
                patient = db.get(models.Patient, patient.id) if patient else patient
                response.update({
                    "reply": (
                        f"Your appointment is confirmed.\n\n"
                        f"* Appointment ID: {result['appointment_code']}\n"
                        f"* Doctor: {result['doctor']} ({result.get('specialty') or ''})\n"
                        f"* Date: {datetime.fromisoformat(result['date']).strftime('%d %b %Y')}\n"
                        f"* Time: {result['time']}\n"
                        f"* Token: {result['token']}\n"
                        f"* Fee: Rs {result['fee']:.0f} - invoice {result['invoice_number']}\n\n"
                        "A WhatsApp confirmation has been sent. Please arrive 10 minutes early with your "
                        "previous reports. Anything else I can help with?"
                    ),
                    "appointment": result,
                    "stage": conv.stage,
                    "quick_replies": ["Show my appointments", "Payment options", "Book another"],
                })
                if concern.get("concern_id"):
                    c = db.get(models.AIConcern, concern["concern_id"])
                    if c:
                        c.status = "ADDRESSED"
                        c.appointment_id = result["appointment_id"]
                        db.commit()
            else:
                conv.failed_bookings = (conv.failed_bookings or 0) + 1
                conv.stage = "AWAIT_SLOT_CHOICE"
                db.commit()
                response.update({
                    "reply": f"I could not complete that booking: {result.get('error')}. Here are other slots:",
                    "slots": _slots_to_payload(result.get("alternatives") or []),
                    "stage": conv.stage,
                })
            pending.pop("hold_token", None)
            conv.pending_json = dict(pending)
            db.commit()
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response
        if intent in {"DECLINE", "DENY"}:
            call_tool("release_slot", slot_id=pending.get("slot_id"))
            conv.stage = "AWAIT_SLOT_CHOICE"
            pending.pop("hold_token", None)
            conv.pending_json = dict(pending)
            db.commit()
            response.update({"reply": "No problem - the slot is released. Tell me another time that suits you.",
                             "stage": conv.stage})
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

    # ------------------------------------------------------------------
    # 8. PATIENT TASKS
    # ------------------------------------------------------------------
    if intent == "APPOINTMENT_STATUS" and patient:
        upcoming = call_tool("get_patient_appointments", patient_id=patient.id, upcoming_only=True, limit=5)
        history = call_tool("get_patient_appointments", patient_id=patient.id, limit=3)
        rows = upcoming.get("appointments", [])
        if rows:
            a = rows[0]
            response.update({
                "reply": (f"Your next appointment is on *{a['date']} at {a['time']}* with "
                          f"{a['doctor']} ({a['specialty']}). Token {a['token']}, status {a['status']}."),
                "appointment": {"upcoming": rows, "recent": history.get("appointments", [])},
                "quick_replies": ["Reschedule", "Cancel appointment", "Show prescriptions"],
            })
        else:
            response.update({
                "reply": "I could not find any upcoming appointment. Would you like to book one?",
                "appointment": {"upcoming": [], "recent": history.get("appointments", [])},
                "quick_replies": ["Book appointment"],
            })
        conv.stage = "POST_BOOK"
        db.commit()
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "CANCEL_APPOINTMENT" and patient:
        upcoming = db.scalars(
            select(models.Appointment).where(
                models.Appointment.patient_id == patient.id,
                models.Appointment.appointment_date >= date.today(),
                models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
            ).order_by(models.Appointment.appointment_date)
        ).all()
        if not upcoming:
            response.update({"reply": "I do not see any upcoming appointment to cancel.",
                             "quick_replies": ["Book appointment"]})
        elif len(upcoming) == 1 or intent == "CANCEL_APPOINTMENT" and "cancel" in text.lower() and "confirm" in text.lower():
            target = upcoming[0]
            result = call_tool("cancel_appointment", appointment_id=target.id,
                               reason="Patient requested via AI assistant")
            response.update({
                "reply": (f"Cancelled appointment {result.get('appointment_code')} "
                          f"({target.appointment_date.strftime('%d %b')} {target.start_time.strftime('%H:%M')}). "
                          "The slot is released and the patient has been notified. Would you like to rebook?"),
                "appointment": result,
                "quick_replies": ["Book again", "Show my appointments"],
            })
        else:
            lines = [f"{a.appointment_date.strftime('%d %b')} {a.start_time.strftime('%H:%M')} - "
                     f"{a.doctor.full_name if a.doctor else ''} ({a.appointment_code})" for a in upcoming]
            pending["pending_action"] = "cancel"
            pending["appointment_ids"] = [a.id for a in upcoming]
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_APPOINTMENT_CHOICE"
            db.commit()
            response.update({
                "reply": f"Which appointment should I cancel?\n\n{_numbered_list(lines)}\n\nReply with the number.",
                "stage": conv.stage,
                "quick_replies": [str(i + 1) for i in range(min(3, len(upcoming)))],
            })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "RESCHEDULE_APPOINTMENT" and patient:
        upcoming = db.scalars(
            select(models.Appointment).where(
                models.Appointment.patient_id == patient.id,
                models.Appointment.appointment_date >= date.today(),
                models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
            ).order_by(models.Appointment.appointment_date)
        ).all()
        if not upcoming:
            response.update({"reply": "There is no upcoming appointment to reschedule. Shall I book a new one?",
                             "quick_replies": ["Book appointment"]})
        else:
            target = upcoming[0]
            pending["reschedule_appointment_id"] = target.id
            pending["selected_doctor"] = {"id": target.doctor_id,
                                          "name": target.doctor.full_name if target.doctor else "Doctor"}
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_SLOT_CHOICE"
            db.commit()
            slot_result = call_tool("get_available_slots", doctor_id=target.doctor_id, days_ahead=14, limit=6)
            slot_rows = [{
                **s, "slot_date": date.fromisoformat(s["slot_date"]),
                "start_time": datetime.strptime(s["start_time"], "%H:%M").time(),
                "end_time": datetime.strptime(s["end_time"], "%H:%M").time(),
            } for s in slot_result.get("slots", [])]
            pending["slots_cache"] = [{
                **s, "slot_date": s["slot_date"].isoformat(),
                "start_time": s["start_time"].strftime("%H:%M"),
                "end_time": s["end_time"].strftime("%H:%M"),
            } for s in slot_rows]
            conv.pending_json = dict(pending)
            db.commit()
            response.update({
                "reply": (f"Current appointment: {target.appointment_date.strftime('%d %b')} "
                          f"{target.start_time.strftime('%H:%M')} with "
                          f"{target.doctor.full_name if target.doctor else 'the doctor'}.\n\n"
                          "Pick a new slot:\n\n"
                          + _numbered_list([s["label"] for s in slot_rows[:5]])),
                "slots": _slots_to_payload(slot_rows),
                "stage": conv.stage,
                "quick_replies": [str(i + 1) for i in range(min(3, len(slot_rows)))],
            })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if conv.stage == "AWAIT_APPOINTMENT_CHOICE" and intent == "SELECT_OPTION" and patient:
        ids = pending.get("appointment_ids", [])
        index = int(SELECTION_RE.match(text).group(1)) - 1
        if 0 <= index < len(ids):
            action = pending.get("pending_action")
            if action == "cancel":
                result = call_tool("cancel_appointment", appointment_id=ids[index],
                                   reason="Patient selected via AI assistant")
            else:
                result = call_tool("reschedule_appointment", appointment_id=ids[index],
                                   new_slot_id=pending.get("slot_id"))
            conv.stage = "POST_BOOK"
            db.commit()
            response.update({"reply": f"Done. {result.get('message', 'Your appointment has been updated.')}",
                             "appointment": result, "stage": conv.stage})
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "PRESCRIPTION_LOOKUP" and patient:
        result = call_tool("get_patient_prescriptions", patient_id=patient.id, limit=3)
        items = result.get("prescriptions", [])
        if items:
            top = items[0]
            meds = ", ".join(i["medicine"] for i in top.get("items", [])[:4]) or "no medicines listed"
            response.update({
                "reply": (f"Your latest prescription {top['code']} was issued on "
                          f"{(top.get('issued_at') or '')[:10]} by {top.get('doctor')}.\n"
                          f"Medicines: {meds}.\n\nYou can download the PDF from your patient portal."),
                "prescriptions": items,
                "quick_replies": ["Download prescription", "Show reports"],
            })
        else:
            response.update({"reply": "I could not find any prescription on your record yet.",
                             "quick_replies": ["Book appointment"]})
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "MEDICAL_FILE_LOOKUP" and patient:
        result = call_tool("get_patient_files", patient_id=patient.id, limit=6)
        files = result.get("files", [])
        if files:
            lines = [f"{f['title']} ({f['category']}, {f['uploaded_at'][:10]})" for f in files[:5]]
            response.update({
                "reply": f"I found {len(files)} file(s) on your record:\n\n{_numbered_list(lines)}\n\n"
                         "Open the patient portal to view or download them.",
                "files": files,
                "quick_replies": ["Show prescriptions", "Book appointment"],
            })
        else:
            response.update({"reply": "There are no reports uploaded to your record yet. "
                                      "You can upload them from the patient portal.",
                             "quick_replies": ["Book appointment"]})
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent in {"PAYMENT_STATUS", "INVOICE_LOOKUP"} and patient:
        invoice = call_tool("get_invoice", patient_id=patient.id)
        payments = call_tool("get_payment_status", patient_id=patient.id)
        if invoice.get("found"):
            response.update({
                "reply": (f"Invoice {invoice['invoice_number']}: total Rs {invoice['total']:.2f}, "
                          f"paid Rs {invoice['paid']:.2f}, balance Rs {invoice['balance']:.2f} "
                          f"(status {invoice['status']}).\n"
                          f"Outstanding across all invoices: Rs {payments['outstanding_amount']:.2f}."),
                "payments": payments.get("payments", []),
                "quick_replies": ["Payment options", "Show invoices"],
            })
        else:
            response.update({"reply": "No invoice found on your record.", "quick_replies": ["Book appointment"]})
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "NOTIFICATION_REQUEST" and patient:
        result = call_tool("send_notification", template="CUSTOM", channel="WHATSAPP",
                           message=f"Hello {patient.full_name}, this is a message from Vijay Vargiya "
                                   f"Group of Hospital as requested.", patient_id=patient.id)
        response.update({
            "reply": "Done - I have sent you a WhatsApp message. Did you want a reminder for a specific "
                     "appointment instead?",
            "quick_replies": ["Remind me about my appointment", "Book appointment"],
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    # ------------------------------------------------------------------
    # 9. CONCERN CAPTURE + SPECIALTY ROUTING + DOCTOR MATCHING
    # ------------------------------------------------------------------
    non_concern = {"GREETING", "THANKS", "REVIEW", "EMERGENCY", "HUMAN_ESCALATION", "AFFIRM", "DENY",
                   "DECLINE", "CONFIRM_BOOKING", "CANCEL_APPOINTMENT", "RESCHEDULE_APPOINTMENT",
                   "APPOINTMENT_STATUS", "PRESCRIPTION_LOOKUP", "MEDICAL_FILE_LOOKUP", "PAYMENT_STATUS",
                   "INVOICE_LOOKUP", "NOTIFICATION_REQUEST"}
    if intent in {"PROVIDE_CONCERN", "BOOK_APPOINTMENT", "DOCTOR_SEARCH", "GENERAL_QUERY", "PROVIDE_NAME"} \
            or (conv.stage in {"AWAIT_CONCERN", "IDLE"} and intent not in non_concern):
        # local vocabulary first (0 tokens) -> embeddings hint -> Groq semantics
        concern = understand_concern(db, text)
        route = concern.get("route") or "local"
        recognisable = bool((concern.get("local") or {}).get("understood")) or is_new_medical_concern(text)
        # A terse fragment nobody could read ("tavda lg gya") is asked about;
        # a sentence-shaped message may still use the fuzzy specialty search,
        # because "I could not name the department" is not the same as "this is
        # not a health complaint".
        # A reader (Groq / embeddings) that actually supplied usable terms counts
        # as understanding; otherwise an unrecognisable message must be asked
        # about rather than handed to the fuzzy specialty search - that fuzzy
        # rescue is what used to turn "tavda lg gya" into a confident, wrong
        # department.
        reader_supplied_terms = concern.get("source") in {"groq", "embeddings"}
        ask_instead = not reader_supplied_terms and (route == "ask" or not recognisable)
        understanding = {
            "route": route,
            "source": concern.get("source"),
            "language": (concern.get("local") or {}).get("language"),
            "confidence": medical_language.language_confidence(concern.get("local") or {}),
            "local_terms": (concern.get("local") or {}).get("canonicals"),
            "typos_corrected": (concern.get("local") or {}).get("fuzzy") or {},
            "semantic": concern.get("semantic"),
        }
        sentiment = nlp_signals.sentiment_hint(text)       # supporting signal only
        if sentiment:
            understanding["sentiment"] = sentiment

        if concern.get("needs_clarification") and ask_instead:
            # Neither the local dictionary, the embeddings hint nor the semantic
            # fallback could map this message to a department. We ask - we never
            # guess, and we never let an ambiguous phrase travel as understood.
            pending["draft_concern"] = text.strip()
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_CONCERN"
            db.commit()
            ambiguity = bool(medical_language.has_emergency_term(text)) or _looks_ambiguous(text)
            response.update({
                "reply": (
                    "I want to make sure I pass you to the right department, so let me ask rather than guess.\n\n"
                    "Could you tell me in a few more words what the problem is - for example "
                    "*\u201cfever since 3 days\u201d*, *\u201cknee pain while climbing stairs\u201d* or "
                    "*\u201claal laal dane aur khujli\u201d*?\n\n"
                    "If you would rather speak to a person, reply *talk to reception*."
                    + ("\n\nIf this is an emergency, please call *108* right now." if ambiguity else "")
                ),
                "stage": conv.stage,
                "concern": {
                    "concern_text": concern["concern_text"], "keywords": concern["keywords"],
                    "specialty_id": None, "specialty_name": None,
                    "urgency": concern["urgency"], "severity": concern["severity"],
                },
                "understanding": {**understanding, "action": "clarification_requested"},
                "quick_replies": ["Fever and cold", "Knee pain", "Skin rash", "Talk to reception"],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        specialty = route_to_specialty(tool_ctx, concern, conv.id)

        if concern.get("needs_clarification") and not specialty:
            # The message was recognisably about health, but neither the keyword
            # table nor the fuzzy search could name a department. Ask, never guess.
            pending["draft_concern"] = text.strip()
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_CONCERN"
            db.commit()
            ambiguity = bool(medical_language.has_emergency_term(text)) or _looks_ambiguous(text)
            response.update({
                "reply": (
                    "I want to make sure I pass you to the right department, so let me ask rather than guess.\n\n"
                    "Could you tell me in a few more words what the problem is - for example "
                    "*\u201cfever since 3 days\u201d*, *\u201cknee pain while climbing stairs\u201d* or "
                    "*\u201claal laal dane aur khujli\u201d*?\n\n"
                    "If you would rather speak to a person, reply *talk to reception*."
                    + ("\n\nIf this is an emergency, please call *108* right now." if ambiguity else "")
                ),
                "stage": conv.stage,
                "concern": {
                    "concern_text": concern["concern_text"], "keywords": concern["keywords"],
                    "specialty_id": None, "specialty_name": None,
                    "urgency": concern["urgency"], "severity": concern["severity"],
                },
                "understanding": {**understanding, "action": "clarification_requested"},
                "quick_replies": ["Fever and cold", "Knee pain", "Skin rash", "Talk to reception"],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

        doctors_result = ai_tools.search_doctors(
            db, tool_ctx, specialty_id=(specialty or {}).get("specialty_id"), limit=3)
        doctors = doctors_result.get("doctors", [])

        # Correct department, nobody bookable in it yet -> offer the nearest one
        # that can really be booked, and keep the concern in the conversation.
        routed_specialty = specialty          # the department the patient's words point to
        routing_note = None
        if specialty and not doctors:
            alternate = alternate_specialty(tool_ctx, concern, specialty)
            if alternate:
                routing_note = alternate.pop("note", None)
                specialty = alternate         # the department we can actually book with
                doctors_result = ai_tools.search_doctors(
                    db, tool_ctx, specialty_id=specialty.get("specialty_id"), limit=3)
                doctors = doctors_result.get("doctors", [])

        if patient:
            record = models.AIConcern(
                patient_id=patient.id, conversation_id=conv.id,
                specialty_id=(routed_specialty or {}).get("specialty_id"),
                concern_text=concern["concern_text"], keywords=concern["keywords"],
                duration_text=concern["duration_text"], severity=concern["severity"],
                urgency=concern["urgency"], status="OPEN",
                history_json=[{"at": utcnow().isoformat(), "text": concern["concern_text"]}],
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            concern["concern_id"] = record.id
            if not conv.concern_summary:
                conv.concern_summary = concern["concern_text"][:400]

        log_routing(db, conv, concern, routed_specialty, doctors)

        pending["concern"] = concern
        pending["doctors"] = doctors
        pending["specialty"] = specialty
        conv.pending_json = dict(pending)

        response["concern"] = {
            "concern_text": concern["concern_text"], "keywords": concern["keywords"],
            "specialty_id": (routed_specialty or {}).get("specialty_id"),
            "specialty_name": (routed_specialty or {}).get("specialty_name"),
            "urgency": concern["urgency"], "severity": concern["severity"],
        }
        response["specialty"] = dict(routed_specialty) if routed_specialty else None
        if response["specialty"] is not None and routing_note:
            # the words point here; the online booking happens with the alternate
            response["specialty"]["booking_specialty_id"] = (specialty or {}).get("specialty_id")
            response["specialty"]["booking_specialty_name"] = (specialty or {}).get("specialty_name")
        response["doctors"] = doctors
        response["understanding"] = {
            **understanding,
            "action": "routed",
            "specialty": (specialty or {}).get("specialty_name"),
            # route = who was asked to read the message ("local" | "semantic" | "ask")
            # source = who actually decided ("local" | "groq" | "embeddings" | "unknown")
            "route": route,
            "source": concern.get("source") or understanding.get("source") or "unknown",
        }
        if routing_note:
            response["understanding"]["routing_note"] = routing_note
            response["understanding"]["alternate_specialty"] = True

        if not specialty or not doctors:
            if doctors:
                pass
            else:
                conv.stage = "AWAIT_CONCERN"
                db.commit()
                if specialty:
                    response.update({
                        "reply": (f"*{(routed_specialty or specialty)['specialty_name']}* is the department "
                                  "for what you "
                                  "described, but no doctor there is accepting online bookings at the "
                                  "moment.\n\nReply *talk to reception* and our staff will call you "
                                  "back with the next available appointment."),
                        "stage": conv.stage,
                        "quick_replies": ["Talk to reception", "Book with General Medicine"],
                    })
                else:
                    response.update({
                        "reply": ("I want to route you correctly - could you tell me a little more about the "
                                  "problem, for example 'chest burning since 3 days' or 'knee pain'?"),
                        "stage": conv.stage,
                        "quick_replies": ["Fever and cold", "Knee pain", "Skin rash", "Book appointment"],
                    })
                _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
                return response
        else:
            if not patient:
                pending["draft_concern"] = text
                conv.pending_json = dict(pending)
                conv.stage = "AWAIT_IDENTITY_NAME"
                db.commit()
                if routing_note:
                    lead = routing_note + "\n\n"
                else:
                    lead = (f"Based on what you described, *{specialty['specialty_name']}* looks like the "
                            "right department for you.\n\n")
                response.update({
                    "reply": (lead + "To book a slot I need to identify you first - "
                                     "may I have your full name?"),
                    "stage": conv.stage,
                    "quick_replies": [],
                })
                _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
                return response

            # Do NOT show doctors yet. First tell the patient which department fits
            # and ask for permission. The doctors are kept in pending until they say yes.
            pending["routed_specialty"] = routed_specialty
            pending["routing_note"] = routing_note
            conv.pending_json = dict(pending)
            conv.stage = "AWAIT_DOCTOR_CONSENT"
            db.commit()
            shown_name = (routed_specialty or specialty)["specialty_name"]
            response["doctors"] = []          # nothing is shown until the patient agrees
            response.update({
                "reply": (
                    f"I understand - {concern['concern_text'][:160]}"
                    + (f" ({concern['duration_text']})" if concern["duration_text"] else "")
                    + f".\n\n*{shown_name}* is the suitable department for this. "
                      "Would you like me to show the available doctors?"
                ),
                "stage": conv.stage,
                "quick_replies": ["Yes, show doctors", "No, not now"],
            })
            _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
            return response

    # ------------------------------------------------------------------
    # 10. GREETING / THANKS / FALLBACK
    # ------------------------------------------------------------------
    if intent == "GREETING":
        if patient:
            summary = build_patient_context_summary(db, patient)
            upcoming = summary["upcoming_appointments"]
            extra = (f"Your next appointment is {upcoming[0]['date']} at {upcoming[0]['time']} "
                     f"with {upcoming[0]['doctor']}.\n\n") if upcoming else ""
            response.update({
                "reply": (f"Namaste {patient.full_name}! Welcome back to Vijay Vargiya Group of Hospitals.\n\n"
                          f"{extra}I can book an appointment, share reports or check your bill. "
                          "What would you like?"),
                "quick_replies": ["Book appointment", "Show my reports", "Payment status"],
            })
        else:
            conv.stage = conv.stage if conv.stage != "IDLE" else "IDLE"
            db.commit()
            response.update({
                "reply": ("Namaste! Welcome to *Vijay Vargiya Group of Hospitals*.\n\n"
                          "I am your AI front-desk assistant. Tell me the problem in your own words "
                          "(for example 'fever since 3 days') and I will find the right specialist, "
                          "show live slots and book the appointment.\n\n"
                          "You can also ask for your reports, prescriptions or bills."),
                "quick_replies": ["Book appointment", "Fever and cold", "Knee pain", "Payment status"],
            })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "THANKS":
        response["reply"] = "Happy to help! Wishing you good health. Take care."
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    if intent == "REVIEW":
        response.update({
            "reply": ("Thank you for the feedback! It means a lot to our doctors and staff. "
                      "After your visit you will receive a review link on WhatsApp - "
                      "you can also rate us from the patient portal."),
            "quick_replies": ["Book appointment"],
        })
        _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
        return response

    response.update({
        "reply": ("I can help with appointments, doctor availability, reports, prescriptions, bills and "
                  "directions at Vijay Vargiya Group of Hospitals.\n\n"
                  "Tell me your health concern in your own words and I will find the right specialist. "
                  "If you would prefer a human, just say *reception*."),
        "quick_replies": ["Book appointment", "Show my reports", "Payment status", "Talk to reception"],
    })
    _finish(db, conv, response, text, intent, confidence, user_msg, tools_used, started)
    return response


def _finish(db: Session, conv: models.AIConversation, response: dict, text: str, intent: str,
            confidence: float, user_msg: models.AIMessage, tools_used: list[str],
            started: float) -> None:
    """Persist the assistant reply, tool list, timing and AI summary."""
    latency_ms = int((time.perf_counter() - started) * 1000)
    note = response.pop("_note", None)
    if note and not response["reply"].startswith(note):
        response["reply"] = f"{note}\n\n{response['reply']}"
    response["tools_used"] = tools_used
    response["latency_ms"] = latency_ms
    response["stage"] = conv.stage

    polished = None
    if (response.get("safety") or {}).get("level", "NORMAL") == "NORMAL":
        # Safety / emergency wording must stay exactly as written.
        polished = _llm_polish(SYSTEM_PROMPT, response["reply"],
                               (response.get("concern") or {}).get("concern_text", ""))
    if polished and polished.strip():
        response["reply"] = polished.strip()

    structured = {
        "stage": conv.stage,
        "concern": response.get("concern"),
        "specialty": response.get("specialty"),
        "doctors": response.get("doctors"),
        "slots": [{"slot_id": s["slot_id"], "label": s["label"]} for s in response.get("slots", [])],
        "appointment": response.get("appointment"),
        "escalation": response.get("escalation"),
        "safety": response.get("safety"),
    }
    add_message(db, conv, "assistant", response["reply"], intent=intent, confidence=confidence,
                tool_calls=tools_used, structured=structured, latency_ms=latency_ms,
                safety_flag=(response.get("safety") or {}).get("flag"))

    if tools_used:
        conv.tool_call_count = (conv.tool_call_count or 0) + len(tools_used)

    total = (conv.avg_response_ms or 0) * max(0, (conv.message_count or 2) - 2)
    conv.avg_response_ms = int((total + latency_ms) / max(1, (conv.message_count or 2) - 1))
    conv.last_message_at = utcnow()
    if conv.primary_intent == "EMERGENCY":
        pass
    db.commit()

    # refresh a rolling AI summary of the conversation (AI memory)
    if conv.patient_id and (conv.message_count or 0) % 4 == 0:
        conversation_summary(db, conv)
    db.refresh(conv)
    response["conversation_id"] = conv.id
    response["session_id"] = conv.session_id
    if response.get("patient") is None and conv.patient_id:
        p = db.get(models.Patient, conv.patient_id)
        if p:
            response["patient"] = {"id": p.id, "name": p.full_name, "patient_code": p.patient_code,
                                   "type": conv.patient_type}


def conversation_summary(db: Session, conv: models.AIConversation) -> models.AISummary | None:
    """Summarise the conversation for AI memory / context retrieval."""
    if not conv.patient_id:
        return None
    messages = db.scalars(
        select(models.AIMessage).where(models.AIMessage.conversation_id == conv.id)
        .order_by(models.AIMessage.id.desc()).limit(10)
    ).all()
    patient = db.get(models.Patient, conv.patient_id)
    concerns = db.scalars(
        select(models.AIConcern).where(models.AIConcern.conversation_id == conv.id)
        .order_by(models.AIConcern.created_at.desc()).limit(3)
    ).all()
    lines = [f"{m.role}: {m.content[:160]}" for m in reversed(messages)]
    summary_text = (
        f"Patient {patient.full_name if patient else conv.patient_id} ({conv.patient_type}). "
        f"Primary intent: {conv.primary_intent}. Stage: {conv.stage}. "
        f"Concerns: {'; '.join(c.concern_text[:80] for c in concerns) or 'none'}. "
        f"Bookings: {conv.successful_bookings or 0} success / {conv.failed_bookings or 0} failed. "
        f"Escalations: {conv.escalation_count or 0}. Last exchange: {' | '.join(lines[-4:])}"
    )
    summary = models.AISummary(
        patient_id=conv.patient_id, conversation_id=conv.id, summary_type="CONTEXT",
        summary=summary_text[:4000],
        context_json={"stage": conv.stage, "intent": conv.primary_intent,
                      "concerns": [c.concern_text for c in concerns]},
        token_estimate=len(summary_text) // 4,
    )
    db.add(summary)
    conv.summary = summary_text[:1500]
    db.commit()
    db.refresh(summary)
    return summary


def quick_slot_search(db: Session, *, concern_text: str, days_ahead: int = 7, limit: int = 5) -> dict:
    """Stateless routing helper used by the /ai/routing endpoint."""
    ctx = ToolContext(db, caller="routing")
    concern = extract_concern(db, concern_text)
    specialty = route_to_specialty(ctx, concern, None)
    doctors = ai_tools.search_doctors(db, ctx, specialty_id=(specialty or {}).get("specialty_id"), limit=3)
    slot_rows = ai_tools.get_available_slots(
        db, ctx, specialty_id=(specialty or {}).get("specialty_id"), days_ahead=days_ahead, limit=limit)
    return {"concern": concern, "specialty": specialty, "doctors": doctors.get("doctors", []),
            "slots": slot_rows.get("slots", [])}
