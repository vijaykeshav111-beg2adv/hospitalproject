"""Chat document flow: patient uploads -> AI reads -> doctor verifies.

Everything the chat upload needs lives here, so the HTTP layer
(``app/routers/ai_files.py``) stays thin and the AI engine can call the same
functions when a patient is identified mid-conversation.

No new database tables or columns: the upload is a normal ``MedicalFile`` row
(the same table the consultation uploads use) and the AI reading is kept in the
``AIMessage.structured`` JSON of the conversation, plus a compact state marker
in ``MedicalFile.notes``:

    AI:PRESCRIPTION|conf=0.82|verify=PENDING
    AI:PRESCRIPTION|conf=0.82|verify=CONFIRMED|Dr Arjun Mehra|2026-10-07T18:20|note

``notes`` is 255 chars, which is why the marker is short - the full reading
(visible text, medicines, terms) stays in the chat message JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..security import utcnow
from . import notify, vision
from .ai_tools import ToolContext

log = logging.getLogger("app.chat_documents")

# What the patient may send, mapped onto the MedicalFile categories.
CATEGORY_BY_TYPE = {
    "PRESCRIPTION": "PRESCRIPTION",
    "LAB_REPORT": "LAB_REPORT",
    "SCAN": "SCAN",
    "WOUND_PHOTO": "IMAGE",
    "DISCHARGE": "DISCHARGE",
    "BILL": "OTHER",
    "REPORT": "LAB_REPORT",
    "OTHER": "OTHER",
}

TYPE_LABELS = {
    "PRESCRIPTION": "prescription (dawai ka puraani parchi)",
    "LAB_REPORT": "lab report",
    "SCAN": "scan / X-ray",
    "WOUND_PHOTO": "photo (khaav / sujan / daane)",
    "DISCHARGE": "discharge summary",
    "BILL": "bill / receipt",
    "REPORT": "medical report",
    "OTHER": "document",
}

ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
    "application/pdf",
}
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".pdf"}

PENDING_DIR = "chat-pending"


# --------------------------------------------------------------------------
# state marker helpers (MedicalFile.notes)
# --------------------------------------------------------------------------
def _marker(document_type: str, confidence: float, status: str = "PENDING") -> str:
    return f"AI:{document_type}|conf={confidence:.2f}|verify={status}"


def parse_marker(notes: str | None) -> dict | None:
    """Read back the AI/verify marker from a file's notes."""
    if not notes or not notes.startswith("AI:"):
        return None
    head, _, rest = notes.partition("|")
    out: dict = {"document_type": head[3:] or "OTHER", "verify": "PENDING", "confidence": None,
                 "verified_by": None, "verified_at": None, "note": None}
    parts = rest.split("|")
    if parts and parts[0].startswith("conf="):
        try:
            out["confidence"] = float(parts[0][5:])
        except ValueError:
            out["confidence"] = None
        parts = parts[1:]
    for part in parts:
        if part.startswith("verify="):
            out["verify"] = part[7:] or "PENDING"
        elif out["verified_by"] is None:
            out["verified_by"] = part or None
        elif out["verified_at"] is None:
            out["verified_at"] = part or None
        elif out["note"] is None:
            out["note"] = part or None
    return out


def build_notes(document_type: str, confidence: float, *, verify: str = "PENDING",
                verified_by: str | None = None, verified_at: str | None = None,
                note: str | None = None) -> str:
    marker = _marker(document_type, confidence, verify)
    if verify != "PENDING":
        bits = [marker]
        bits.append((verified_by or "staff")[:40])
        bits.append((verified_at or utcnow().isoformat(timespec="minutes")))
        if note:
            bits.append(note.replace("|", "/")[:60])
        marker = "|".join(bits)
    return marker[:255]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def save_bytes(content: bytes, original_name: str, *, folder: str) -> tuple[Path, str, str]:
    """Write the upload to storage. Returns (path, stored_name, checksum)."""
    checksum = hashlib.sha256(content).hexdigest()
    safe_name = (original_name or "upload").replace(" ", "_")
    stored_name = f"{checksum[:12]}_{safe_name}"
    target_dir = settings.uploads_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored_name
    target.write_bytes(content)
    return target, stored_name, checksum


def validate(content: bytes, filename: str, content_type: str | None) -> str | None:
    """Return an error string, or None when the upload is acceptable."""
    if not content:
        return "File is empty"
    size_mb = len(content) / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        return f"File is {size_mb:.1f} MB - the limit is {settings.max_upload_mb} MB"
    if content_type and content_type.lower() not in ALLOWED_MIME:
        return f"Unsupported file type {content_type} (photo ya PDF bhejein)"
    ext = Path(filename or "").suffix.lower()
    if ext and ext not in ALLOWED_EXT:
        return f"Unsupported file extension {ext} (photo ya PDF bhejein)"
    return None


# --------------------------------------------------------------------------
# upload + AI reading
# --------------------------------------------------------------------------
def store_upload(db: Session, *, content: bytes, filename: str, content_type: str | None,
                 conversation: models.AIConversation, patient: models.Patient | None = None,
                 uploaded_by: int | None = None, note: str | None = None,
                 read_with_ai: bool = True) -> dict:
    """Store the file, ask the AI to read it, and record everything.

    Returns a dict with the file record, the AI reading, and the chat text to
    show the patient. The caller (router / ai_engine) decides the wording.
    """
    folder = patient.patient_code if patient else f"{PENDING_DIR}/conv-{conversation.id}"
    path, stored_name, checksum = save_bytes(content, filename, folder=folder)

    reading = None
    if read_with_ai:
        reading = vision.read_document(path, mime_type=content_type, hint_text=note)

    document_type = (reading or {}).get("document_type") or _guess_type(filename, content_type)
    confidence = float((reading or {}).get("confidence") or 0)
    category = CATEGORY_BY_TYPE.get(document_type, "OTHER")

    record = None
    if patient is not None:
        record = models.MedicalFile(
            patient_id=patient.id,
            uploaded_by=uploaded_by,
            category=category,
            title=_title(document_type, filename, reading),
            original_name=filename or stored_name,
            stored_name=stored_name,
            file_path=str(path),
            mime_type=content_type,
            size_bytes=len(content),
            checksum=checksum,
            access_level="PATIENT_VISIBLE",
            notes=build_notes(document_type, confidence, verify="PENDING"),
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    # the patient's message + the AI's reading are both kept in the transcript
    payload = {
        "chat_file": {
            "file_id": record.id if record else None,
            "stored_name": stored_name,
            "path": str(path),
            "original_name": filename,
            "mime_type": content_type,
            "size_bytes": len(content),
            "category": category,
            "document_type": document_type,
            "patient_linked": record is not None,
            "note": note,
        },
        "ai_reading": reading,
    }
    user_msg = _add_message(db, conversation, "user",
                            content=f"[file] {filename or stored_name}" + (f" - {note}" if note else ""),
                            intent="FILE_UPLOAD", structured=payload)
    reply = reply_text(record, document_type, reading, patient)
    bot_msg = _add_message(db, conversation, "assistant", content=reply, intent="FILE_REVIEW",
                           structured={"chat_file_id": record.id if record else None,
                                       "reading": reading, "action": "file_received"})

    return {
        "file": record,
        "file_id": record.id if record else None,
        "stored_name": stored_name,
        "path": str(path),
        "size_bytes": len(content),
        "category": category,
        "document_type": document_type,
        "ai_reading": reading,
        "reply": reply,
        "user_message_id": user_msg.id,
        "assistant_message_id": bot_msg.id,
        "patient_linked": record is not None,
    }


def _guess_type(filename: str, content_type: str | None) -> str:
    name = (filename or "").lower()
    if "presc" in name or "parchi" in name:
        return "PRESCRIPTION"
    if any(k in name for k in ("xray", "x-ray", "scan", "mri", "ct", "sono")):
        return "SCAN"
    if "report" in name or "lab" in name or "test" in name:
        return "LAB_REPORT"
    if content_type and content_type.lower().startswith("image/"):
        return "WOUND_PHOTO"
    return "OTHER"


def _title(document_type: str, filename: str, reading: dict | None) -> str:
    base = TYPE_LABELS.get(document_type, "document").split(" (")[0].title()
    if reading and reading.get("document_date"):
        base += f" ({reading['document_date']})"
    return f"{base} - {filename or 'upload'}"[:180]


def reply_text(record: models.MedicalFile | None, document_type: str,
               reading: dict | None, patient: models.Patient | None) -> str:
    """The honest, non-diagnostic chat reply after an upload."""
    label = TYPE_LABELS.get(document_type, "document")
    lines = [f"Aapki file mil gayi - ye *{label}* lag rahi hai."]

    if reading and reading.get("readable") and reading.get("summary"):
        lines.append("")
        lines.append("AI ne itna padha (ye **diagnosis nahi** hai, sirf padhkar bataya hai):")
        lines.append(f"* {reading['summary']}")
        if reading.get("medicines"):
            lines.append(f"* Medicines likhi hain: {', '.join(reading['medicines'][:6])}")
        if reading.get("doctor_name"):
            lines.append(f"* Parchi par doctor ka naam: {reading['doctor_name']}")
        if reading.get("document_date"):
            lines.append(f"* Date: {reading['document_date']}")
        if reading.get("visible_text") and len(reading["visible_text"]) > 40:
            snippet = " ".join(str(reading["visible_text"]).split())[:280]
            lines.append(f"* Padha hua text: _{snippet}_")
    elif reading is not None:
        lines.append("Photo thodi dhundhli hai - AI poora padh nahi paaya. Doctor ise khud dekh lega.")
    elif vision.enabled():
        lines.append("PDF hai, isliye AI ise padh nahi paaya - doctor ise kholkar dekhega.")
    else:
        lines.append("AI reading abhi off hai (GROQ_API_KEY set nahi hai), isliye doctor ise khud dekhega.")

    if record is not None:
        lines.append("")
        lines.append(f"File patient ke record mein jama ho gayi hai (*{record.original_name}*).")
        lines.append("Doctor ise verify karke confirm karega - AI ki reading sirf madad ke liye hai.")
    else:
        lines.append("")
        lines.append("Ye file is chat ke saath jud gayi hai. Apna naam aur 10-digit mobile number "
                     "bata dein to main ise aapke patient record se jod dunga.")
    return "\n".join(lines)


def _add_message(db: Session, conv: models.AIConversation, role: str, *, content: str,
                 intent: str | None = None, structured: dict | None = None) -> models.AIMessage:
    msg = models.AIMessage(conversation_id=conv.id, role=role, content=content[:4000],
                           intent=intent, structured=structured)
    db.add(msg)
    conv.message_count = (conv.message_count or 0) + 1
    db.commit()
    db.refresh(msg)
    return msg


# --------------------------------------------------------------------------
# linking pending files once the patient is known
# --------------------------------------------------------------------------
def link_pending(db: Session, conversation: models.AIConversation,
                 patient: models.Patient) -> list[models.MedicalFile]:
    """Attach files uploaded before the patient identified himself.

    Called from ``ai_engine.handle_message`` as soon as the conversation has a
    patient, so nothing a patient sent is ever lost.
    """
    if patient is None:
        return []
    created: list[models.MedicalFile] = []
    rows = db.scalars(
        select(models.AIMessage)
        .where(models.AIMessage.conversation_id == conversation.id,
               models.AIMessage.intent == "FILE_UPLOAD")
        .order_by(models.AIMessage.id)
    ).all()
    for msg in rows:
        # a copy: mutating the dict that is still referenced by the loaded JSON
        # value makes SQLAlchemy see "no change" and skip the UPDATE
        payload = dict((msg.structured or {}).get("chat_file") or {})
        if not payload or payload.get("file_id"):
            continue                      # already linked
        path = Path(payload.get("path") or "")
        if not path.exists():
            continue
        reading = (msg.structured or {}).get("ai_reading") or {}
        document_type = payload.get("document_type") or reading.get("document_type") or "OTHER"
        record = models.MedicalFile(
            patient_id=patient.id,
            uploaded_by=conversation.user_id,
            category=CATEGORY_BY_TYPE.get(document_type, "OTHER"),
            title=_title(document_type, payload.get("original_name") or "chat upload", reading),
            original_name=payload.get("original_name") or path.name,
            stored_name=payload.get("stored_name") or path.name,
            file_path=str(path),
            mime_type=payload.get("mime_type"),
            size_bytes=int(payload.get("size_bytes") or path.stat().st_size),
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            access_level="PATIENT_VISIBLE",
            notes=build_notes(document_type, float(reading.get("confidence") or 0)),
        )
        db.add(record)
        db.flush()
        payload["file_id"] = record.id
        payload["patient_linked"] = True
        msg.structured = {**(msg.structured or {}), "chat_file": payload}
        created.append(record)
    if created:
        db.commit()
        for record in created:
            db.refresh(record)
        log.info("Linked %d chat upload(s) to patient %s", len(created), patient.patient_code)
    return created


# --------------------------------------------------------------------------
# doctor review / verification
# --------------------------------------------------------------------------
def review_queue(db: Session, *, doctor_id: int | None = None, limit: int = 50,
                 only_pending: bool = True, mine_only: bool = False) -> list[dict]:
    """Chat-uploaded documents waiting for a doctor's eye.

    Every doctor with ``files:read`` may open any of these documents (that is how
    a front-desk review pool works in a small hospital), so the queue returns the
    whole pool and just *marks* the ones belonging to the doctor's own patients
    with ``mine: true``. Pass ``mine_only=True`` for a strict own-patients list.
    """
    stmt = (select(models.MedicalFile)
            .where(models.MedicalFile.notes.like("AI:%"),
                   models.MedicalFile.is_archived.is_(False))
            .order_by(models.MedicalFile.created_at.desc())
            .limit(limit))
    rows = db.scalars(stmt).all()
    out = []
    for record in rows:
        marker = parse_marker(record.notes) or {}
        if only_pending and marker.get("verify") != "PENDING":
            continue
        patient = db.get(models.Patient, record.patient_id)
        reading = _reading_for(db, record.id)
        mine = _doctor_sees_patient(db, doctor_id, record.patient_id) if doctor_id else False
        if doctor_id and mine_only and not mine:
            continue
        out.append({
            "mine": mine,
            "file_id": record.id,
            "patient_id": record.patient_id,
            "patient_name": patient.full_name if patient else None,
            "patient_code": patient.patient_code if patient else None,
            "title": record.title,
            "category": record.category,
            "document_type": marker.get("document_type"),
            "verify": marker.get("verify"),
            "confidence": marker.get("confidence"),
            "verified_by": marker.get("verified_by"),
            "verified_at": marker.get("verified_at"),
            "review_note": marker.get("note"),
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "uploaded_at": record.created_at,
            "view_url": f"/api/v1/medical-files/{record.id}/view",
            "download_url": f"/api/v1/medical-files/{record.id}/download",
            "ai_reading": reading,
        })
    return out


def _doctor_sees_patient(db: Session, doctor_id: int, patient_id: int) -> bool:
    """True when this patient is already the doctor's (appointment or consultation)."""
    if db.scalar(select(models.Appointment.id).where(
            models.Appointment.patient_id == patient_id,
            models.Appointment.doctor_id == doctor_id).limit(1)):
        return True
    return db.scalar(
        select(models.Consultation.id).where(
            models.Consultation.patient_id == patient_id,
            models.Consultation.doctor_id == doctor_id,
        ).limit(1)) is not None


def _reading_for(db: Session, file_id: int) -> dict | None:
    """The stored AI reading of a chat upload (from the conversation transcript)."""
    msgs = db.scalars(
        select(models.AIMessage)
        .where(models.AIMessage.intent == "FILE_UPLOAD")
        .order_by(models.AIMessage.id.desc())
        .limit(400)
    ).all()
    for msg in msgs:
        payload = (msg.structured or {}).get("chat_file") or {}
        if payload.get("file_id") == file_id:
            return (msg.structured or {}).get("ai_reading")
    return None


def verify_file(db: Session, record: models.MedicalFile, *, decision: str, doctor_name: str,
                doctor_id: int | None = None, note: str | None = None,
                corrected_type: str | None = None) -> dict:
    """A doctor confirms (or corrects) what the AI read.

    decision: CONFIRMED | CORRECTED | NEEDS_REVIEW
    """
    decision = (decision or "").upper()
    if decision not in {"CONFIRMED", "CORRECTED", "NEEDS_REVIEW"}:
        raise ValueError("decision must be CONFIRMED, CORRECTED or NEEDS_REVIEW")
    marker = parse_marker(record.notes) or {}
    document_type = (corrected_type or marker.get("document_type") or "OTHER").upper()
    if document_type not in CATEGORY_BY_TYPE:
        document_type = marker.get("document_type") or "OTHER"
    verified_at = utcnow().isoformat(timespec="minutes")
    record.notes = build_notes(document_type, float(marker.get("confidence") or 0),
                               verify=decision, verified_by=doctor_name,
                               verified_at=verified_at, note=note)
    if decision == "CORRECTED":
        record.category = CATEGORY_BY_TYPE.get(document_type, record.category)
        record.title = f"{TYPE_LABELS.get(document_type, 'document').split(' (')[0].title()} - {record.original_name}"[:180]
    db.commit()
    db.refresh(record)
    db.add(models.AuditLog(action="FILE_VERIFY", resource="medical_file", resource_id=str(record.id),
                           user_id=doctor_id, user_role="DOCTOR",
                           new_value={"decision": decision, "document_type": document_type,
                                      "ai_type": marker.get("document_type"), "note": note}))
    db.commit()
    return {"file_id": record.id, "patient_id": record.patient_id, "decision": decision,
            "document_type": document_type, "ai_document_type": marker.get("document_type"),
            "confidence": marker.get("confidence"), "verified_by": doctor_name,
            "verified_at": verified_at, "notes": record.notes,
            "patient_message": _patient_message(decision, record, document_type)}


def _patient_message(decision: str, record: models.MedicalFile, document_type: str) -> str:
    label = TYPE_LABELS.get(document_type, "document").split(" (")[0]
    if decision == "CONFIRMED":
        return f"Doctor ne aapki file dekh li - *{record.title}* sahi hai."
    if decision == "CORRECTED":
        return f"Doctor ne file ki reading theek kar di - ab ye *{label}* hai ({record.title})."
    return f"Doctor ne aapki file kholi hai aur uspar thodi aur jaanch maangi hai - {record.title}."


def request_doctor_review(db: Session, *, conversation: models.AIConversation,
                          patient: models.Patient | None, file_id: int | None,
                          reason: str = "patient asked a doctor to review an uploaded document",
                          escalate=None) -> dict:
    """The 'doctor se consult' button: open a DOCTOR escalation with the file."""
    detail = reason
    if file_id:
        record = db.get(models.MedicalFile, file_id)
        if record:
            detail = f"{reason} | file #{record.id}: {record.title} ({record.category})"

    esc = None
    if escalate is not None:                      # reuse ai_engine.escalate for consistency
        esc = escalate(db, conversation, "DOCTOR", "patient uploaded a document for review",
                       detail=detail, patient=patient)
    else:
        esc = models.AIEscalation(conversation_id=conversation.id,
                                  patient_id=patient.id if patient else conversation.patient_id,
                                  level="DOCTOR", reason="patient uploaded a document for review",
                                  detail=detail, status="OPEN")
        db.add(esc)
        conversation.escalation_count = (conversation.escalation_count or 0) + 1
        db.commit()
        db.refresh(esc)

    # tell every doctor on duty (in-app; whatsapp/email follow the channel rules)
    context = {"patient_name": patient.full_name if patient else "Chat visitor",
               "reason": "Document uploaded for review", "hospital": settings.app_name}
    doctors = db.scalars(select(models.User).where(models.User.role.has(name="DOCTOR"),
                                                   models.User.is_active.is_(True))).all()
    for user in doctors:
        notify.queue_notification(db, template="ESCALATION_DOCTOR", context=context,
                                  channel="IN_APP", user_id=user.id,
                                  patient=patient)
    db.commit()
    return {"escalation_id": esc.id, "level": "DOCTOR", "detail": detail,
            "notified_doctors": len(doctors)}


def conversation_files(db: Session, conversation_id: int) -> list[dict]:
    """Everything the patient sent in this chat (for the chat sidebar)."""
    msgs = db.scalars(
        select(models.AIMessage)
        .where(models.AIMessage.conversation_id == conversation_id,
               models.AIMessage.intent == "FILE_UPLOAD")
        .order_by(models.AIMessage.id)
    ).all()
    out = []
    for msg in msgs:
        payload = (msg.structured or {}).get("chat_file") or {}
        attached = payload.get("file_id")
        out.append({
            "file_id": attached,
            "name": payload.get("original_name"),
            "document_type": payload.get("document_type"),
            "size_bytes": payload.get("size_bytes"),
            "patient_linked": bool(attached),
            "uploaded_at": msg.created_at,
            "view_url": f"/api/v1/medical-files/{attached}/view" if attached else None,
        })
    return out
