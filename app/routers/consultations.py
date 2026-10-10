"""Consultations: clinical notes, diagnosis (doctor-entered), follow-ups, record linkage."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..codes import unique_code
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..security import utcnow
from ..services import notify, patients as patient_service

router = APIRouter(prefix="/consultations", tags=["Consultations & Records"])


def _code(db: Session) -> str:
    """Collision-safe consultation code (consultation_code is UNIQUE)."""
    return unique_code(db, prefix="CON", model=models.Consultation,
                       column=models.Consultation.consultation_code)


def _out(db: Session, c: models.Consultation) -> dict:
    doctor = db.get(models.Doctor, c.doctor_id)
    patient = db.get(models.Patient, c.patient_id)
    return {
        "id": c.id, "consultation_code": c.consultation_code, "appointment_id": c.appointment_id,
        "patient_id": c.patient_id, "patient_name": patient.full_name if patient else None,
        "doctor_id": c.doctor_id, "doctor_name": doctor.full_name if doctor else None,
        "status": c.status, "chief_complaint": c.chief_complaint, "vitals": c.vitals,
        "observations": c.observations, "diagnosis": c.diagnosis, "clinical_notes": c.clinical_notes,
        "advice": c.advice, "follow_up_required": c.follow_up_required, "follow_up_date": c.follow_up_date,
        "follow_up_notes": c.follow_up_notes,
        "started_at": c.started_at, "completed_at": c.completed_at,
    }


def _doctor_for_user(db: Session, user: CurrentUser) -> models.Doctor | None:
    return db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))


@router.get("", response_model=list[schemas.ConsultationOut], summary="List consultations")
def list_consultations(db: Session = Depends(get_db), patient_id: int | None = None,
                       doctor_id: int | None = None, from_date: date | None = None,
                       status_filter: str | None = None, limit: int = 100,
                       user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Consultation)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Consultation.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("consultations:read"):
        raise HTTPException(status_code=403, detail="Missing permission: consultations:read")
    if patient_id:
        stmt = stmt.where(models.Consultation.patient_id == patient_id)
    if doctor_id:
        stmt = stmt.where(models.Consultation.doctor_id == doctor_id)
    if from_date:
        stmt = stmt.where(func.date(models.Consultation.created_at) >= from_date)
    if status_filter:
        stmt = stmt.where(models.Consultation.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(models.Consultation.created_at.desc()).limit(limit)).all()
    return [_out(db, c) for c in rows]


@router.post("", response_model=schemas.ConsultationOut, status_code=201, summary="Start a consultation")
def start_consultation(payload: schemas.ConsultationCreate, request: Request, db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require_permission("consultations:write"))):
    patient = db.get(models.Patient, payload.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    doctor_id = payload.doctor_id
    if user.role == "DOCTOR":
        doctor = _doctor_for_user(db, user)
        if not doctor:
            raise HTTPException(status_code=400, detail="No doctor profile linked to this login")
        doctor_id = doctor.id
    if not doctor_id:
        raise HTTPException(status_code=422, detail="doctor_id is required")

    appointment = db.get(models.Appointment, payload.appointment_id) if payload.appointment_id else None
    consultation = models.Consultation(
        consultation_code=_code(db), appointment_id=payload.appointment_id, patient_id=payload.patient_id,
        doctor_id=doctor_id, chief_complaint=payload.chief_complaint, vitals=payload.vitals,
        status="IN_PROGRESS", started_at=utcnow(),
    )
    db.add(consultation)
    db.commit()
    db.refresh(consultation)

    if appointment:
        appointment.queue_status = "IN_CONSULTATION"
        if not appointment.checked_in_at:
            appointment.checked_in_at = utcnow()
        db.commit()

    if payload.chief_complaint:
        patient_service.add_medical_record(
            db, patient_id=patient.id, consultation_id=consultation.id,
            appointment_id=payload.appointment_id, doctor_id=doctor_id, record_type="CONSULTATION",
            title="Chief complaint", description=payload.chief_complaint,
            values_json=payload.vitals, entered_by=user.id,
        )
    audit(db, action="CONSULTATION_START", resource="consultation", resource_id=consultation.id, user=user,
          request=request, new_value={"patient_id": patient.id, "doctor_id": doctor_id})
    return _out(db, consultation)


@router.get("/{consultation_id}", response_model=schemas.ConsultationOut, summary="Consultation detail")
def consultation_detail(consultation_id: int, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    consultation = db.get(models.Consultation, consultation_id)
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or consultation.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return _out(db, consultation)


@router.patch("/{consultation_id}", response_model=schemas.ConsultationOut,
              summary="Update notes / diagnosis (doctor only)")
def update_consultation(consultation_id: int, payload: schemas.ConsultationUpdate, request: Request,
                        db: Session = Depends(get_db),
                        user: CurrentUser = Depends(require_permission("consultations:write"))):
    consultation = db.get(models.Consultation, consultation_id)
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if user.role == "DOCTOR":
        doctor = _doctor_for_user(db, user)
        if doctor and consultation.doctor_id != doctor.id:
            raise HTTPException(status_code=403, detail="You can only edit your own consultations")

    before = _out(db, consultation)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(consultation, field, value)
    db.commit()

    # mirror clinical content into medical records (record history / versions)
    if payload.diagnosis:
        patient_service.add_medical_record(
            db, patient_id=consultation.patient_id, consultation_id=consultation.id,
            appointment_id=consultation.appointment_id, doctor_id=consultation.doctor_id,
            record_type="DIAGNOSIS", title="Diagnosis", description=payload.diagnosis,
            entered_by=user.id,
        )
    if payload.observations:
        patient_service.add_medical_record(
            db, patient_id=consultation.patient_id, consultation_id=consultation.id,
            doctor_id=consultation.doctor_id, record_type="OBSERVATION",
            title="Doctor observations", description=payload.observations, entered_by=user.id,
        )
    if payload.follow_up_required and payload.follow_up_date:
        patient_service.add_medical_record(
            db, patient_id=consultation.patient_id, consultation_id=consultation.id,
            doctor_id=consultation.doctor_id, record_type="FOLLOW_UP",
            title=f"Follow-up advised on {payload.follow_up_date}",
            description=payload.follow_up_notes or "Review required", entered_by=user.id,
        )
    audit(db, action="CONSULTATION_UPDATE", resource="consultation", resource_id=consultation_id,
          user=user, request=request, previous_value=before, new_value=data)
    db.refresh(consultation)
    return _out(db, consultation)


@router.post("/{consultation_id}/complete", response_model=schemas.ConsultationOut, summary="Complete")
def complete_consultation(consultation_id: int, request: Request, db: Session = Depends(get_db),
                          user: CurrentUser = Depends(require_permission("consultations:write"))):
    consultation = db.get(models.Consultation, consultation_id)
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found")
    consultation.status = "COMPLETED"
    consultation.completed_at = utcnow()
    if consultation.appointment_id:
        appointment = db.get(models.Appointment, consultation.appointment_id)
        if appointment:
            appointment.status = "COMPLETED"
            appointment.queue_status = "COMPLETED"
            appointment.checked_out_at = utcnow()
    db.commit()

    if consultation.follow_up_required and consultation.follow_up_date:
        patient = db.get(models.Patient, consultation.patient_id)
        doctor = db.get(models.Doctor, consultation.doctor_id)
        if patient:
            notify.notify_patient(db, patient, "FOLLOW_UP_REMINDER", {
                "patient_name": patient.full_name,
                "doctor_name": doctor.full_name if doctor else "your doctor",
                "date": consultation.follow_up_date.strftime("%d %b %Y"),
            }, channels=["WHATSAPP", "EMAIL", "IN_APP"])
    audit(db, action="CONSULTATION_COMPLETE", resource="consultation", resource_id=consultation_id,
          user=user, request=request, new_value={"status": "COMPLETED"})
    return _out(db, consultation)


@router.get("/{consultation_id}/records", response_model=list[schemas.MedicalRecordOut],
            summary="Records produced by a consultation")
def consultation_records(consultation_id: int, db: Session = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    consultation = db.get(models.Consultation, consultation_id)
    if not consultation:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or consultation.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return list(db.scalars(select(models.MedicalRecord).where(
        models.MedicalRecord.consultation_id == consultation_id).order_by(models.MedicalRecord.id)))


@router.get("/follow-ups/due", summary="Follow-ups due (doctor / admin)")
def follow_ups_due(db: Session = Depends(get_db), days: int = 7, doctor_id: int | None = None,
                   user: CurrentUser = Depends(require_permission("consultations:read"))):
    from datetime import timedelta
    if user.role == "DOCTOR" and not doctor_id:
        doctor = _doctor_for_user(db, user)
        doctor_id = doctor.id if doctor else -1
    stmt = select(models.Consultation).where(
        models.Consultation.follow_up_required.is_(True),
        models.Consultation.follow_up_date.is_not(None),
        models.Consultation.follow_up_date >= date.today(),
        models.Consultation.follow_up_date <= date.today() + timedelta(days=days),
    )
    if doctor_id:
        stmt = stmt.where(models.Consultation.doctor_id == doctor_id)
    rows = db.scalars(stmt.order_by(models.Consultation.follow_up_date)).all()
    return [{
        "consultation_id": c.id, "consultation_code": c.consultation_code,
        "patient_id": c.patient_id,
        "patient_name": (db.get(models.Patient, c.patient_id).full_name
                         if db.get(models.Patient, c.patient_id) else None),
        "patient_phone": (db.get(models.Patient, c.patient_id).phone
                          if db.get(models.Patient, c.patient_id) else None),
        "follow_up_date": c.follow_up_date.isoformat(), "notes": c.follow_up_notes,
        "diagnosis": c.diagnosis,
    } for c in rows]
