"""Medical files: lab reports, scans, images, PDFs - upload, download, metadata, access control."""
from __future__ import annotations

import hashlib
import shutil
from datetime import date
from pathlib import Path

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import CurrentUser, audit, ensure_patient_scope, get_current_user, patient_for_user, require_permission

router = APIRouter(prefix="/medical-files", tags=["Medical Files"])

ALLOWED_CATEGORIES = {"LAB_REPORT", "PRESCRIPTION", "SCAN", "IMAGE", "PDF", "DISCHARGE", "DOCTOR_DOC", "OTHER"}
ALLOWED_MIME = {
    "application/pdf", "image/jpeg", "image/png", "image/webp", "image/tiff",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain", "application/dicom",
}
ACCESS_LEVELS = {"PATIENT_VISIBLE", "DOCTOR_ONLY", "STAFF_ONLY"}


def _out(f: models.MedicalFile) -> dict:
    return {
        "id": f.id, "patient_id": f.patient_id, "consultation_id": f.consultation_id,
        "category": f.category, "title": f.title, "original_name": f.original_name,
        "mime_type": f.mime_type, "size_bytes": f.size_bytes, "access_level": f.access_level,
        "notes": f.notes, "uploaded_by": f.uploaded_by,
        "created_at": f.created_at,
    }


@router.get("", response_model=list[schemas.MedicalFileOut], summary="List medical files")
def list_files(db: Session = Depends(get_db), patient_id: int | None = None, category: str | None = None,
               limit: int = Query(100, le=500), user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.MedicalFile).where(models.MedicalFile.is_archived.is_(False))
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.MedicalFile.patient_id == (patient.id if patient else -1),
                          models.MedicalFile.access_level == "PATIENT_VISIBLE")
    elif not user.has_permission("files:read"):
        raise HTTPException(status_code=403, detail="Missing permission: files:read")
    if patient_id:
        stmt = stmt.where(models.MedicalFile.patient_id == patient_id)
    if category:
        stmt = stmt.where(models.MedicalFile.category == category.upper())
    return list(db.scalars(stmt.order_by(models.MedicalFile.created_at.desc()).limit(limit)))


@router.post("", response_model=schemas.MedicalFileOut, status_code=201, summary="Upload a file")
async def upload_file(request: Request, file: UploadFile = File(...), patient_id: int = Form(...),
                      category: str = Form("OTHER"), title: str | None = Form(None),
                      consultation_id: int | None = Form(None), appointment_id: int | None = Form(None),
                      access_level: str = Form("PATIENT_VISIBLE"), notes: str | None = Form(None),
                      db: Session = Depends(get_db),
                      user: CurrentUser = Depends(require_permission("files:write"))):
    patient = db.get(models.Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    category = category.upper()
    if category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of {sorted(ALLOWED_CATEGORIES)}")
    access_level = access_level.upper()
    if access_level not in ACCESS_LEVELS:
        raise HTTPException(status_code=422, detail=f"access_level must be one of {sorted(ACCESS_LEVELS)}")

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB limit")
    if file.content_type and file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported file type {file.content_type}")

    checksum = hashlib.sha256(content).hexdigest()
    stored_name = f"{checksum[:12]}_{(file.filename or 'upload').replace(' ', '_')}"
    patient_dir = settings.uploads_dir / patient.patient_code
    patient_dir.mkdir(parents=True, exist_ok=True)
    target = patient_dir / stored_name
    with target.open("wb") as fh:
        fh.write(content)

    record = models.MedicalFile(
        patient_id=patient_id, consultation_id=consultation_id, appointment_id=appointment_id,
        uploaded_by=user.id, category=category, title=title or (file.filename or "Medical file"),
        original_name=file.filename or stored_name, stored_name=stored_name,
        file_path=str(target), mime_type=file.content_type, size_bytes=len(content),
        checksum=checksum, access_level=access_level, notes=notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    audit(db, action="FILE_UPLOAD", resource="medical_file", resource_id=record.id, user=user,
          request=request, new_value={"patient_id": patient_id, "category": category,
                                      "size_bytes": len(content), "access_level": access_level})
    return _out(record)


@router.get("/{file_id}", response_model=schemas.MedicalFileOut, summary="File metadata")
def file_metadata(file_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    _check_access(user, db, record)
    return _out(record)


@router.get("/{file_id}/download", summary="Download a file")
def download_file(file_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    _check_access(user, db, record)
    path = Path(record.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="File is no longer available on storage")
    audit(db, action="FILE_DOWNLOAD", resource="medical_file", resource_id=file_id, user=user,
          new_value={"patient_id": record.patient_id})
    return FileResponse(path, media_type=record.mime_type or "application/octet-stream",
                        filename=record.original_name)


@router.get("/{file_id}/view", summary="Inline view (PDF / image)")
def view_file(file_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    _check_access(user, db, record)
    path = Path(record.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="File unavailable")
    return FileResponse(path, media_type=record.mime_type or "application/octet-stream",
                        headers={"Content-Disposition": f'inline; filename="{record.original_name}"'})


@router.patch("/{file_id}", response_model=schemas.MedicalFileOut, summary="Update metadata / access level")
def update_file(file_id: int, request: Request, title: str | None = None, category: str | None = None,
                access_level: str | None = None, notes: str | None = None,
                db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("files:write"))):
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    before = _out(record)
    if title:
        record.title = title
    if category:
        record.category = category.upper()
    if access_level:
        record.access_level = access_level.upper()
    if notes is not None:
        record.notes = notes
    db.commit()
    db.refresh(record)
    audit(db, action="FILE_UPDATE", resource="medical_file", resource_id=file_id, user=user, request=request,
          previous_value=before, new_value=_out(record))
    return _out(record)


@router.delete("/{file_id}", summary="Archive / delete a file")
def delete_file(file_id: int, request: Request, hard: bool = False, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("files:write"))):
    record = db.get(models.MedicalFile, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if hard and user.role in {"ADMIN", "SUPER_ADMIN"}:
        Path(record.file_path).unlink(missing_ok=True)
        db.delete(record)
    else:
        record.is_archived = True
    db.commit()
    audit(db, action="FILE_DELETE", resource="medical_file", resource_id=file_id, user=user, request=request,
          new_value={"hard": hard})
    return {"success": True, "file_id": file_id, "hard_deleted": bool(hard)}


@router.get("/patient/{patient_id}/summary", summary="File counts by category for a patient")
def patient_files_summary(patient_id: int, db: Session = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    rows = db.scalars(select(models.MedicalFile).where(
        models.MedicalFile.patient_id == patient_id, models.MedicalFile.is_archived.is_(False))).all()
    if user.role == "PATIENT":
        rows = [r for r in rows if r.access_level == "PATIENT_VISIBLE"]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.category] = counts.get(r.category, 0) + 1
    return {"patient": {"id": patient.id, "code": patient.patient_code, "name": patient.full_name},
            "total_files": len(rows), "by_category": counts,
            "storage_kb": round(sum(r.size_bytes or 0 for r in rows) / 1024, 1)}


def _check_access(user: CurrentUser, db: Session, record: models.MedicalFile) -> None:
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or record.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
        if record.access_level != "PATIENT_VISIBLE":
            raise HTTPException(status_code=403, detail="This file is restricted to clinical staff")
        return
    if record.access_level == "DOCTOR_ONLY" and user.role not in {"DOCTOR", "ADMIN", "SUPER_ADMIN"}:
        raise HTTPException(status_code=403, detail="Doctor-only file")
    if not user.has_permission("files:read"):
        raise HTTPException(status_code=403, detail="Missing permission: files:read")
