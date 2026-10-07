"""Medical records: longitudinal clinical history with versions and visibility control."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import (CurrentUser, audit, ensure_patient_scope, get_current_user, patient_for_user,
                    require_permission)
from ..services import patients as patient_service

router = APIRouter(prefix="/medical-records", tags=["Medical Records"])


@router.get("", response_model=list[schemas.MedicalRecordOut], summary="List medical records")
def list_records(db: Session = Depends(get_db), patient_id: int | None = None,
                 record_type: str | None = None, consultation_id: int | None = None,
                 limit: int = Query(100, le=500),
                 user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.MedicalRecord)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.MedicalRecord.patient_id == (patient.id if patient else -1),
                          models.MedicalRecord.is_visible_to_patient.is_(True))
    elif not user.has_permission("records:read"):
        raise HTTPException(status_code=403, detail="Missing permission: records:read")

    if patient_id:
        stmt = stmt.where(models.MedicalRecord.patient_id == patient_id)
    if record_type:
        stmt = stmt.where(models.MedicalRecord.record_type == record_type.upper())
    if consultation_id:
        stmt = stmt.where(models.MedicalRecord.consultation_id == consultation_id)
    return list(db.scalars(stmt.order_by(models.MedicalRecord.recorded_at.desc()).limit(limit)))


@router.post("", response_model=schemas.MedicalRecordOut, status_code=201, summary="Add a record")
def create_record(payload: schemas.MedicalRecordCreate, request: Request, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_permission("records:write"))):
    patient = db.get(models.Patient, payload.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    record = patient_service.add_medical_record(
        db, patient_id=payload.patient_id, consultation_id=payload.consultation_id,
        appointment_id=payload.appointment_id, doctor_id=payload.doctor_id,
        record_type=payload.record_type.upper(), title=payload.title, description=payload.description,
        values_json=payload.values_json, severity=payload.severity, entered_by=user.id,
        visible=payload.is_visible_to_patient,
    )
    audit(db, action="RECORD_CREATE", resource="medical_record", resource_id=record.id, user=user,
          request=request, new_value={"patient_id": payload.patient_id, "type": payload.record_type,
                                      "title": payload.title})
    return record


@router.get("/{record_id}", response_model=schemas.MedicalRecordOut, summary="Record detail")
def record_detail(record_id: int, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    record = db.get(models.MedicalRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    ensure_patient_scope(user, db, record.patient_id)
    if user.role == "PATIENT" and not record.is_visible_to_patient:
        raise HTTPException(status_code=403, detail="This record is not shared with patients")
    return record


@router.patch("/{record_id}", response_model=schemas.MedicalRecordOut,
              summary="Amend a record (keeps the previous version)")
def amend_record(record_id: int, payload: schemas.MedicalRecordCreate, request: Request,
                 db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_permission("records:write"))):
    original = db.get(models.MedicalRecord, record_id)
    if not original:
        raise HTTPException(status_code=404, detail="Record not found")
    new_record = patient_service.add_medical_record(
        db, patient_id=original.patient_id, consultation_id=original.consultation_id,
        appointment_id=original.appointment_id, doctor_id=original.doctor_id,
        record_type=payload.record_type.upper() or original.record_type,
        title=payload.title or original.title,
        description=payload.description, values_json=payload.values_json or original.values_json,
        severity=payload.severity, entered_by=user.id, visible=payload.is_visible_to_patient,
        supersedes_id=original.id,
    )
    audit(db, action="RECORD_AMEND", resource="medical_record", resource_id=new_record.id, user=user,
          request=request, previous_value={"id": original.id, "title": original.title,
                                            "version": original.version},
          new_value={"id": new_record.id, "version": new_record.version})
    return new_record


@router.get("/{record_id}/history", summary="Version history of a record")
def record_history(record_id: int, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    record = db.get(models.MedicalRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    ensure_patient_scope(user, db, record.patient_id)
    chain, current = [], record
    while current:
        chain.append(current)
        current = db.get(models.MedicalRecord, current.supersedes_id) if current.supersedes_id else None
    children = db.scalars(select(models.MedicalRecord).where(
        models.MedicalRecord.supersedes_id == record_id)).all()
    return {
        "record_id": record_id,
        "versions": [{"id": r.id, "version": r.version, "title": r.title, "description": r.description,
                      "recorded_at": r.recorded_at.isoformat()} for r in chain],
        "superseded_by": [{"id": c.id, "version": c.version, "recorded_at": c.recorded_at.isoformat()}
                          for c in children],
    }


@router.get("/patient/{patient_id}/timeline", summary="Full patient record timeline")
def patient_timeline(patient_id: int, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    records = db.scalars(select(models.MedicalRecord).where(
        models.MedicalRecord.patient_id == patient_id).order_by(models.MedicalRecord.recorded_at)).all()
    consultations = db.scalars(select(models.Consultation).where(
        models.Consultation.patient_id == patient_id)).all()
    appointments = db.scalars(select(models.Appointment).where(
        models.Appointment.patient_id == patient_id)).all()

    events = []
    for r in records:
        if user.role == "PATIENT" and not r.is_visible_to_patient:
            continue
        events.append({"type": "RECORD", "subtype": r.record_type, "at": r.recorded_at.isoformat(),
                       "title": r.title, "detail": r.description})
    for c in consultations:
        events.append({"type": "CONSULTATION", "at": (c.started_at or c.created_at).isoformat(),
                       "title": f"Consultation {c.consultation_code}", "detail": c.diagnosis})
    for a in appointments:
        events.append({"type": "APPOINTMENT", "at": f"{a.appointment_date}T{a.start_time}",
                       "title": f"{a.appointment_code} - {a.status}", "detail": a.reason})
    events.sort(key=lambda e: e["at"])
    return {"patient": {"id": patient.id, "code": patient.patient_code, "name": patient.full_name},
            "events": events}


@router.get("/patient/{patient_id}/previous", summary="Previous consultations + records summary")
def previous_records(patient_id: int, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    consultations = db.scalars(select(models.Consultation).where(
        models.Consultation.patient_id == patient_id,
        models.Consultation.status == "COMPLETED",
    ).order_by(models.Consultation.completed_at.desc()).limit(10)).all()
    return {
        "patient": {"id": patient.id, "code": patient.patient_code, "name": patient.full_name},
        "previous_consultations": [{
            "id": c.id, "code": c.consultation_code,
            "date": c.completed_at.isoformat() if c.completed_at else None,
            "doctor": (db.get(models.Doctor, c.doctor_id).full_name if db.get(models.Doctor, c.doctor_id) else None),
            "chief_complaint": c.chief_complaint, "diagnosis": c.diagnosis,
            "advice": c.advice, "follow_up_date": c.follow_up_date.isoformat() if c.follow_up_date else None,
        } for c in consultations],
        "previous_appointments": [{
            "code": a.appointment_code, "date": a.appointment_date.isoformat(), "status": a.status,
        } for a in db.scalars(select(models.Appointment).where(
            models.Appointment.patient_id == patient_id).order_by(
            models.Appointment.appointment_date.desc()).limit(10))],
    }
