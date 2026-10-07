"""Patient system: registration, profile, search, history, records, files, payments."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import (CurrentUser, audit, current_patient, ensure_patient_scope, get_current_user,
                    paginate, require_permission)
from ..services import patients as patient_service

router = APIRouter(prefix="/patients", tags=["Patient System"])


def _out(p: models.Patient) -> dict:
    return {
        "id": p.id, "patient_code": p.patient_code, "user_id": p.user_id, "full_name": p.full_name,
        "date_of_birth": p.date_of_birth, "age": p.age, "gender": p.gender, "blood_group": p.blood_group,
        "phone": p.phone, "alternate_phone": p.alternate_phone, "email": p.email,
        "address_line": p.address_line, "city": p.city, "state": p.state, "pincode": p.pincode,
        "emergency_contact_name": p.emergency_contact_name,
        "emergency_contact_phone": p.emergency_contact_phone,
        "emergency_contact_relation": p.emergency_contact_relation,
        "allergies": p.allergies, "chronic_conditions": p.chronic_conditions, "notes": p.notes,
        "is_active": p.is_active, "created_at": p.created_at,
    }


@router.get("", response_model=list[schemas.PatientOut], summary="Search / list patients")
def list_patients(db: Session = Depends(get_db), search: str | None = None, active_only: bool = True,
                  limit: int = Query(50, le=200), page: int = 1, page_size: int = 20,
                  _: CurrentUser = Depends(require_permission("patients:read"))):
    return patient_service.search_patients(db, search or "", limit=limit, active_only=active_only)


@router.get("/search/suggest", summary="Typeahead search for reception / booking screens")
def suggest(db: Session = Depends(get_db), q: str = Query("", min_length=1),
            _: CurrentUser = Depends(require_permission("patients:read"))):
    rows = patient_service.search_patients(db, q, limit=8)
    return [{"id": p.id, "label": f"{p.full_name} - {p.patient_code} ({p.phone})",
             "patient_code": p.patient_code, "name": p.full_name, "phone": p.phone} for p in rows]


@router.post("", response_model=schemas.PatientOut, status_code=201, summary="Register a patient")
def register_patient(payload: schemas.PatientCreate, request: Request, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_permission("patients:write"))):
    existing = patient_service.search_patients(db, payload.phone, limit=1)
    if existing and existing[0].phone == payload.phone:
        raise HTTPException(status_code=409,
                            detail=f"A patient with this phone already exists: {existing[0].patient_code}")
    data = payload.model_dump(exclude={"create_portal_login", "portal_password"})
    if payload.create_portal_login and payload.email:
        patient, portal_user = patient_service.create_patient_with_login(
            db, password=payload.portal_password, created_by=user.id, source="RECEPTION", **data)
        audit(db, action="PATIENT_REGISTER", resource="patient", resource_id=patient.id, user=user,
              request=request, new_value={"patient_code": patient.patient_code, "portal_login": True})
        return _out(patient)
    patient = patient_service.create_patient_record(db, created_by=user.id, source="RECEPTION", **data)
    audit(db, action="PATIENT_REGISTER", resource="patient", resource_id=patient.id, user=user,
          request=request, new_value={"patient_code": patient.patient_code})
    return _out(patient)


@router.get("/me", response_model=schemas.PatientProfileOut, summary="My patient profile (portal)")
def my_profile(patient: models.Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return {**_out(patient), **patient_service.full_profile(db, patient)}


@router.get("/{patient_id}", response_model=schemas.PatientProfileOut, summary="Patient profile + history")
def patient_detail(patient_id: int, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    return {**_out(patient), **patient_service.full_profile(db, patient)}


@router.patch("/{patient_id}", response_model=schemas.PatientOut, summary="Update patient")
def update_patient(patient_id: int, payload: schemas.PatientUpdate, request: Request,
                   db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("patients:write"))):
    patient = db.get(models.Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    before = _out(patient)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(patient, field, value)
    if payload.date_of_birth:
        patient.age = patient_service._age_from_dob(payload.date_of_birth)
    db.commit()
    db.refresh(patient)
    audit(db, action="PATIENT_UPDATE", resource="patient", resource_id=patient_id, user=user,
          request=request, previous_value=before, new_value=_out(patient))
    return _out(patient)


@router.get("/{patient_id}/history", summary="Patient history: visits, consultations, records")
def patient_history(patient_id: int, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    profile = patient_service.full_profile(db, patient)
    return {
        "patient": {"id": patient.id, "code": patient.patient_code, "name": patient.full_name},
        "appointments": [{
            "id": a.id, "code": a.appointment_code, "date": a.appointment_date.isoformat(),
            "time": a.start_time.strftime("%H:%M"), "status": a.status, "queue_status": a.queue_status,
            "doctor": a.doctor.full_name if a.doctor else None,
            "specialty": a.specialty.name if a.specialty else None, "reason": a.reason,
        } for a in profile["previous_appointments"] + profile["upcoming_appointments"]],
        "consultations": [{
            "id": c.id, "code": c.consultation_code, "date": c.started_at.isoformat() if c.started_at else None,
            "doctor_id": c.doctor_id, "diagnosis": c.diagnosis, "follow_up_date":
            c.follow_up_date.isoformat() if c.follow_up_date else None,
        } for c in profile["consultations"]],
        "records": [{
            "id": r.id, "type": r.record_type, "title": r.title, "recorded_at": r.recorded_at.isoformat(),
            "severity": r.severity, "version": r.version,
        } for r in profile["medical_records"]],
        "stats": profile["stats"],
    }


@router.get("/{patient_id}/summary", summary="AI-style patient summary")
def patient_summary(patient_id: int, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    patient = ensure_patient_scope(user, db, patient_id)
    from ..services.ai_engine import build_patient_context_summary

    return build_patient_context_summary(db, patient)


@router.delete("/{patient_id}", summary="Archive (soft delete) a patient")
def archive_patient(patient_id: int, request: Request, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("patients:delete"))):
    patient = db.get(models.Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    patient.is_active = False
    db.commit()
    audit(db, action="PATIENT_ARCHIVE", resource="patient", resource_id=patient_id, user=user, request=request)
    return {"success": True, "patient_id": patient_id}
