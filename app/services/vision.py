"""AI reading of an uploaded document (prescription / report / X-ray / wound photo).

The patient photographs a paper and sends it in the chat. This module asks the
hospital's own AI provider (Groq) to *read* it - never to diagnose:

  * what kind of document it looks like (prescription, lab report, X-ray, photo)
  * the text that is visible on it (handwriting included, best effort)
  * the medicine names / doctor name / date, when they are written on it
  * which department the content points at (validated later against the
    hospital's own vocabulary - see ``ai_engine._rescue_with_terms``)

Rules baked in here, matching the project's safety contract:

  * it is forbidden to diagnose, prescribe, or state a finding as fact. Every
    output is phrased as an observation and every UI surface says *"doctor
    verify karega"*.
  * anything the model returns is a **suggestion**; the caller validates the
    canonical terms against the database before they can influence routing.
  * if the provider/key/model is unavailable, this module returns ``None`` and
    the file is still stored - the doctor simply reads it himself.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger("app.vision")

# Groq serves vision through its multimodal models. The old Llama 4 Scout was
# retired on 17 Jul 2026; Qwen 3.8 27B is the current successor (Qwen 3.6 27B
# is deprecated but still online), so both are tried in that order.
DEFAULT_VISION_MODEL = "qwen/qwen3.8-27b"
FALLBACK_VISION_MODEL = "qwen/qwen3.6-27b"

# Groq refuses base64 images above 4 MB, so bigger photos are stored but not read.
MAX_VISION_BYTES = 4 * 1024 * 1024

IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}

DOCUMENT_TYPES = (
    "PRESCRIPTION",     # doctor's prescription / medicine slip
    "LAB_REPORT",       # blood test / pathology report
    "SCAN",             # X-ray, MRI, CT, ultrasound film or printout
    "WOUND_PHOTO",      # a photo of an injury, rash, wound or swelling
    "DISCHARGE",        # discharge summary / hospital paper
    "BILL",             # hospital bill / receipt
    "REPORT",           # any other medical paper
    "OTHER",
)

SYSTEM_PROMPT = """You read documents that patients photograph and send to a hospital's chat desk.
You are NOT a doctor. You must never diagnose, never prescribe, never say what the illness is,
and never state a finding as certain. You only report what is visible/legible on the document.

Return ONE JSON object, nothing else, in this exact shape:
{
  "document_type": "PRESCRIPTION|LAB_REPORT|SCAN|WOUND_PHOTO|DISCHARGE|BILL|REPORT|OTHER",
  "summary": "one or two neutral sentences describing what the document appears to contain",
  "visible_text": "the text you can read, verbatim, as plain lines (best effort)",
  "medicines": ["names exactly as written, empty list if none"],
  "doctor_name": "name written on it, or null",
  "document_date": "date written on it (YYYY-MM-DD if possible), or null",
  "body_area": "body area the document/photo is about, or null",
  "canonical_terms": ["plain English medical words for what it shows, chosen only from this list: {vocabulary}"],
  "urgency_hint": "LOW|MEDIUM|HIGH",
  "language": "en|hi|hinglish",
  "confidence": 0.0,
  "readable": true
}
Rules:
* If the image is blurry or you cannot read it, set "readable": false, confidence below 0.3 and say so in summary.
* "canonical_terms" must contain ONLY words from the list above. They are used to find the right
  department for the patient; anything else is ignored by the system.
* For a wound/injury photo, describe only what is visible (e.g. "cut", "swelling", "redness in eye"),
  never the likely cause or severity of injury.
* Never invent a medicine, a doctor's name or a date that is not written on the document.
"""


def _vocabulary_for_prompt() -> str:
    from . import medical_language

    terms = sorted({c for c in medical_language.LOCAL_MEDICAL_TERMS.values() if " " not in c or len(c) < 24})
    return ", ".join(terms[:150])


def vision_model() -> str:
    return getattr(settings, "groq_vision_model", "") or DEFAULT_VISION_MODEL


def enabled() -> bool:
    """True when a Groq key is present, so documents can actually be read."""
    return bool((settings.groq_api_key or "").strip())


def _is_image(mime: str | None, path: Path) -> bool:
    if mime and mime.lower() in IMAGE_MIMES:
        return True
    guessed, _ = mimetypes.guess_type(str(path))
    return bool(guessed and guessed in IMAGE_MIMES)


def _maybe_downscale(path: Path) -> tuple[bytes, str]:
    """Return (raw_bytes, mime). Uses Pillow only if it happens to be installed."""
    raw = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if len(raw) <= MAX_VISION_BYTES or mime is None:
        return raw, mime or "image/jpeg"
    try:  # optional - the core install does not need Pillow
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((1600, 1600))
            out = path.with_suffix(".ai.jpg")
            img.save(out, "JPEG", quality=80)
            log.info("vision: downscaled %s for the AI reader", path.name)
            data = out.read_bytes()
            out.unlink(missing_ok=True)
            return data, "image/jpeg"
    except Exception:  # noqa: BLE001 - Pillow missing or unreadable image
        return raw, mime


def _extract_json(raw: str, parse) -> dict[str, Any] | None:
    data = parse(raw)
    if not isinstance(data, dict):
        return None
    return data


def read_document(file_path: str | Path, *, mime_type: str | None = None,
                  hint_text: str | None = None) -> dict[str, Any] | None:
    """Ask the AI provider to read a photographed document. Never raises.

    Returns the structured reading (see ``SYSTEM_PROMPT``) or ``None`` when the
    document cannot be read (no key, not an image, too large, provider error).
    """
    path = Path(file_path)
    if not path.exists():
        log.warning("vision: file missing %s", path)
        return None
    if not enabled():
        log.info("vision: no GROQ_API_KEY - storing the file without an AI reading")
        return None
    if not _is_image(mime_type, path):
        # PDFs and other documents need OCR; we do not fake it.
        log.info("vision: %s is not an image - skipped", path.name)
        return None

    data, resolved_mime = _maybe_downscale(path)
    if len(data) > MAX_VISION_BYTES:
        log.info("vision: %s is larger than the 4 MB image limit - skipped", path.name)
        return None

    try:
        from groq import Groq  # type: ignore
    except Exception:  # noqa: BLE001
        log.info("vision: groq package not installed")
        return None

    from .groq_engine import _parse_json_object

    b64 = base64.b64encode(data).decode()
    prompt = SYSTEM_PROMPT.replace("{vocabulary}", _vocabulary_for_prompt())
    user_text = "Read this document the patient sent to the hospital chat desk."
    if hint_text:
        user_text += f"\nThe patient added: {hint_text[:200]}"

    models = [vision_model()]
    fallback = getattr(settings, "groq_vision_fallback_model", "") or FALLBACK_VISION_MODEL
    if fallback and fallback not in models:
        models.append(fallback)

    client = Groq(api_key=settings.groq_api_key, timeout=settings.groq_timeout_seconds)
    for model in models:
        try:
            completion = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=900,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{resolved_mime};base64,{b64}"}},
                    ]},
                ],
            )
            raw = (completion.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - provider errors must not lose the upload
            log.warning("vision: %s failed (%s: %s)", model, type(exc).__name__, exc)
            continue

        reading = _extract_json(raw, _parse_json_object)
        if not reading:
            log.warning("vision: %s returned no usable JSON", model)
            continue
        reading = _clean(reading)
        reading["model"] = model
        log.info("vision: %s read as %s (confidence %.2f)",
                 path.name, reading.get("document_type"), float(reading.get("confidence") or 0))
        return reading
    return None


def _clean(reading: dict[str, Any]) -> dict[str, Any]:
    """Keep only what we expect, with sane types - the model is not trusted."""
    out: dict[str, Any] = {}
    dtype = str(reading.get("document_type") or "OTHER").upper().strip()
    out["document_type"] = dtype if dtype in DOCUMENT_TYPES else "OTHER"
    out["summary"] = str(reading.get("summary") or "")[:600]
    out["visible_text"] = str(reading.get("visible_text") or "")[:4000]
    medicines = reading.get("medicines") or []
    out["medicines"] = [str(m)[:80] for m in medicines if str(m).strip()][:15] if isinstance(medicines, list) else []
    out["doctor_name"] = _opt_str(reading.get("doctor_name"), 120)
    out["document_date"] = _opt_str(reading.get("document_date"), 32)
    out["body_area"] = _opt_str(reading.get("body_area"), 60)
    terms = reading.get("canonical_terms") or []
    out["canonical_terms"] = [str(t).strip().lower()[:60] for t in terms if str(t).strip()][:12] if isinstance(terms, list) else []
    urgency = str(reading.get("urgency_hint") or "LOW").upper()
    out["urgency_hint"] = urgency if urgency in {"LOW", "MEDIUM", "HIGH"} else "LOW"
    out["language"] = str(reading.get("language") or "en")[:12]
    try:
        out["confidence"] = max(0.0, min(1.0, float(reading.get("confidence") or 0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    out["readable"] = bool(reading.get("readable", True))
    return out


def _opt_str(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def reading_as_text(reading: dict[str, Any] | None) -> str:
    """A short, human line for the chat bubble / doctor card."""
    if not reading:
        return ""
    bits = [f"type: {reading.get('document_type')}"]
    if reading.get("summary"):
        bits.append(str(reading["summary"]))
    if reading.get("medicines"):
        bits.append("medicines: " + ", ".join(reading["medicines"][:6]))
    if reading.get("doctor_name"):
        bits.append("doctor on paper: " + str(reading["doctor_name"]))
    if reading.get("document_date"):
        bits.append("date: " + str(reading["document_date"]))
    return " | ".join(bits)[:500]
