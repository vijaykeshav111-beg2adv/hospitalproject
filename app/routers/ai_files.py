"""Chat document endpoints: upload -> AI reading -> doctor verify / consult.

Kept in its own router (instead of editing ``app/routers/ai.py``) so the
existing AI chat contract stays exactly as it is. Mounted at
``/api/v1/ai/files``.

Route map
---------
POST   /api/v1/ai/files                       patient uploads a photo / PDF in the chat
GET    /api/v1/ai/files/conversation/{id}     what this chat has received so far
POST   /api/v1/ai/files/{file_id}/consult     "doctor se baat karni hai" - opens a DOCTOR escalation
GET    /api/v1/ai/files/review                doctor's review queue (AI readings waiting for a look)
POST   /api/v1/ai/files/{file_id}/verify      doctor confirms / corrects the AI reading
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, get_optional_user, patient_for_user, require_permission
from ..services import chat_documents, vision
from ..services import ai_engine

router = APIRouter(prefix="/ai/files", tags=["AI chat files"])


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
def _resolve_conversation(db: Session, *, session_id: str | None, conversation_id: int | None,
                          user: CurrentUser | None, channel: str) -> models.AIConversation:
    if not session_id and not conversation_id:
        raise HTTPException(status_code=422, detail="session_id (ya conversation_id) zaroori hai")
    return ai_engine.get_or_create_conversation(
        db, conversation_id=conversation_id, session_id=session_id or "upload",
        channel=channel or "WEB", user_id=getattr(user, "id", None),
    )


def _name_of(user) -> str:
    """CurrentUser wraps the ORM user - the display name lives on the ORM row."""
    orm = getattr(user, "user", None)
    return (getattr(orm, "full_name", None) or getattr(user, "email", None) or "Doctor")[:80]


def _public(meta: dict) -> dict:
    """Shape for the browser - no filesystem paths leak out."""
    return {
        "file_id": meta.get("file_id"),
        "document_type": meta.get("document_type"),
        "category": meta.get("category"),
        "size_bytes": meta.get("size_bytes"),
        "patient_linked": meta.get("patient_linked"),
        "reply": meta.get("reply"),
        "ai_reading": meta.get("ai_reading"),
        "view_url": f"/api/v1/medical-files/{meta['file_id']}/view" if meta.get("file_id") else None,
        "download_url": f"/api/v1/medical-files/{meta['file_id']}/download" if meta.get("file_id") else None,
    }


# --------------------------------------------------------------------------
# UPLOAD
# --------------------------------------------------------------------------
@router.post("", summary="Upload a prescription / report / X-ray / wound photo in the chat")
async def upload_chat_file(
    request: Request,
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    conversation_id: int | None = Form(None),
    note: str | None = Form(None),
    name: str | None = Form(None),
    phone: str | None = Form(None),
    channel: str = Form("WEB"),
    read_with_ai: bool = Form(True),
    db: Session = Depends(get_db),
    user: CurrentUser | None = Depends(get_optional_user),
):
    """Works for a logged-in patient and for an anonymous chat visitor.

    The file is always stored. When the patient is known (logged in, or already
    identified in this conversation) a ``medical_files`` row is created
    immediately; otherwise the upload stays attached to the conversation and is
    linked to the patient record as soon as the chat identifies them.
    """
    content = await file.read()
    error = chat_documents.validate(content, file.filename or "", file.content_type)
    if error:
        raise HTTPException(status_code=422, detail=error)

    conv = _resolve_conversation(db, session_id=session_id, conversation_id=conversation_id,
                                user=user, channel=channel)

    patient = ai_engine.resolve_patient(db, conv, user_id=getattr(user, "id", None),
                                        phone=phone, name=name)
    if patient is not None:
        # anything sent earlier in this chat belongs to the patient too
        chat_documents.link_pending(db, conv, patient)

    meta = chat_documents.store_upload(
        db, content=content, filename=file.filename or "upload",
        content_type=file.content_type, conversation=conv, patient=patient,
        uploaded_by=getattr(user, "id", None), note=note, read_with_ai=read_with_ai,
    )

    understanding = _file_understanding(db, meta, conv)
    audit(db, action="AI_CHAT_FILE_UPLOAD", resource="medical_file",
          resource_id=str(meta.get("file_id") or conv.id), user=user, request=request,
          new_value={"kind": meta["document_type"], "bytes": meta["size_bytes"],
                     "linked": meta["patient_linked"]})
    return {
        **_public(meta),
        "conversation_id": conv.id,
        "session_id": conv.session_id,
        "understanding": understanding,
        "quick_replies": ["Doctor se consult karein", "Appointment book karein", "Koi aur file bhejein"],
        "disclaimer": ("AI sirf padhkar batata hai - diagnosis doctor karega. "
                       "Ye kisi bhi emergency ka ilaaj nahi hai."),
    }


def _file_understanding(db: Session, meta: dict, conv: models.AIConversation) -> dict:
    """Turn the AI reading into a routing hint the rest of the system can use."""
    reading = meta.get("ai_reading") or {}
    terms = reading.get("canonical_terms") or []
    out: dict = {
        "document_type": meta["document_type"],
        "read_by_ai": bool(reading),
        "model": reading.get("model"),
        "confidence": reading.get("confidence"),
        "urgency_hint": reading.get("urgency_hint"),
        "patient_linked": meta["patient_linked"],
        "verify": chat_documents.parse_marker(
            getattr(meta.get("file"), "notes", None) or "") or {"verify": "PENDING"},
    }
    if not terms:
        return out

    # same validation path the text fallback uses: only hospital vocabulary may
    # influence routing, and the concern is stored like any other one
    text = " ".join([str(reading.get("summary") or ""), " ".join(terms)])
    rescued = ai_engine._rescue_with_terms(db, text, {
        "canonical_terms": terms,
        "concern": reading.get("summary") or "uploaded document",
        "body_area": reading.get("body_area"),
        "symptoms": terms,
        "confidence": max(0.5, float(reading.get("confidence") or 0)),
        "language": reading.get("language"),
        "urgency_hint": reading.get("urgency_hint"),
    }, source="groq-vision")
    if rescued:
        specialty = (rescued.get("candidate_specialties") or [{}])[0]
        out["suggested_specialty"] = specialty.get("specialty_name")
        out["route"] = "semantic"
        out["source"] = "groq-vision"
        out["accepted_terms"] = terms
    return out


# --------------------------------------------------------------------------
# CONVERSATION FILES
# --------------------------------------------------------------------------
@router.get("/conversation/{conversation_id}", summary="Files received in this chat")
def list_conversation_files(conversation_id: int, db: Session = Depends(get_db),
                            user: CurrentUser | None = Depends(get_optional_user)):
    conv = db.get(models.AIConversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id and user and conv.user_id != user.id and user.role not in {"ADMIN", "SUPER_ADMIN", "DOCTOR", "RECEPTIONIST"}:
        raise HTTPException(status_code=403, detail="Not allowed")
    return {"conversation_id": conversation_id,
            "files": chat_documents.conversation_files(db, conversation_id),
            "ai_reading_enabled": vision.enabled()}


# --------------------------------------------------------------------------
# DOCTOR REVIEW / VERIFY
# --------------------------------------------------------------------------
@router.get("/review", summary="Documents uploaded in chat, waiting for a doctor")
def review(db: Session = Depends(get_db), doctor_id: int | None = None, limit: int = 50,
           include_verified: bool = False, mine_only: bool = False,
           user: CurrentUser = Depends(require_permission("files:read"))):
    """The review pool. ``mine_only=true`` narrows it to the doctor's own patients.

    This is a clinical queue: it can contain other patients' documents, so it is
    limited to doctors and administrators even though ``files:read`` is wider.
    """
    if user.role not in {"DOCTOR", "ADMIN", "SUPER_ADMIN"}:
        raise HTTPException(status_code=403, detail="Only a doctor or admin can open the review queue")
    if doctor_id is None and user.role == "DOCTOR":
        doctor = db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))
        doctor_id = doctor.id if doctor else None
    rows = chat_documents.review_queue(db, doctor_id=doctor_id, limit=limit,
                                       only_pending=not include_verified, mine_only=mine_only)
    return {"pending": sum(1 for r in rows if (r.get("verify") or "PENDING") == "PENDING"),
            "ai_reading_enabled": vision.enabled(),
            "files": rows}


@router.post("/{file_id}/verify", summary="Doctor confirms / corrects what the AI read")
def verify(file_id: int, request: Request, decision: str = Form("CONFIRMED"),
           note: str | None = Form(None), document_type: str | None = Form(None),
           db: Session = Depends(get_db),
           user: CurrentUser = Depends(require_permission("files:write", "consultations:write",
                                                          any_of=True))):
    if user.role not in {"DOCTOR", "ADMIN", "SUPER_ADMIN"}:
        raise HTTPException(status_code=403, detail="Only a doctor can verify a document")
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        result = chat_documents.verify_file(db, record, decision=decision,
                                            doctor_name=_name_of(user), doctor_id=user.id,
                                            note=note, corrected_type=document_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # the patient sees the outcome in the same chat
    conv = db.scalar(
        select(models.AIConversation)
        .where(models.AIConversation.patient_id == record.patient_id)
        .order_by(models.AIConversation.id.desc()).limit(1))
    if conv:
        ai_engine.add_message(db, conv, "assistant", result["patient_message"], intent="FILE_VERIFIED")
        result["notified_conversation_id"] = conv.id
    return result


# --------------------------------------------------------------------------
# CONSULT REQUEST ("doctor se baat karni hai")
# --------------------------------------------------------------------------
@router.post("/{file_id}/consult", summary="Ask a doctor to review this document")
def consult(file_id: int, request: Request, session_id: str | None = Form(None),
            conversation_id: int | None = Form(None), reason: str | None = Form(None),
            db: Session = Depends(get_db),
            user: CurrentUser | None = Depends(get_optional_user)):
    record = db.get(models.MedicalFile, file_id)
    conv = None
    if conversation_id:
        conv = db.get(models.AIConversation, conversation_id)
    if conv is None and session_id:
        conv = ai_engine.get_or_create_conversation(db, conversation_id=None, session_id=session_id,
                                                    channel="WEB", user_id=getattr(user, "id", None))
    if conv is None:
        raise HTTPException(status_code=422, detail="conversation_id ya session_id chahiye")
    if not record:
        raise HTTPException(status_code=404, detail="File not found")

    patient = ai_engine.resolve_patient(db, conv, user_id=getattr(user, "id", None),
                                        phone=None, name=None)
    result = chat_documents.request_doctor_review(
        db, conversation=conv, patient=patient, file_id=file_id,
        reason=reason or "patient asked a doctor to review an uploaded document",
        escalate=ai_engine.escalate,
    )
    ai_engine.add_message(db, conv, "assistant",
                          "Theek hai - maine ye file doctor ko review ke liye bhej di hai. "
                          "Doctor dekhte hi aapko isi chat / phone par bata dega.",
                          intent="FILE_CONSULT_REQUESTED")
    audit(db, action="AI_CHAT_FILE_CONSULT", resource="ai_escalation",
          resource_id=str(result["escalation_id"]), user=user, request=request,
          new_value={"file_id": file_id, "level": "DOCTOR"})
    return {"escalation": result,
            "reply": "Doctor ko review request bhej di gayi hai. Aap chahein to appointment bhi book kar sakte hain.",
            "quick_replies": ["Appointment book karein", "Koi aur file bhejein"]}
