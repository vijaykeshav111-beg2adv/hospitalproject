"""Prescriptions: issue, list, PDF generation and notification."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..codes import unique_code
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..security import utcnow
from ..services import documents, notify

router = APIRouter(prefix="/prescriptions", tags=["Prescriptions"])


def _code(db: Session) -> str:
    """Collision-safe prescription code (prescription_code is UNIQUE)."""
    return unique_code(db, prefix="RX", model=models.Prescription,
                       column=models.Prescription.prescription_code)


def _out(db: Session, p: models.Prescription, include_items: bool = True) -> dict:
    doctor = db.get(models.Doctor, p.doctor_id)
    patient = db.get(models.Patient, p.patient_id)
    data = {
        "id": p.id, "prescription_code": p.prescription_code, "consultation_id": p.consultation_id,
        "patient_id": p.patient_id, "patient_name": patient.full_name if patient else None,
        "doctor_id": p.doctor_id, "doctor_name": doctor.full_name if doctor else None,
        "diagnosis_summary": p.diagnosis_summary, "notes": p.notes, "advice": p.advice,
        "status": p.status, "pdf_url": f"/api/v1/prescriptions/{p.id}/pdf" if p.pdf_path else None,
        "issued_at": p.issued_at, "valid_until": p.valid_until,
    }
    if include_items:
        data["items"] = list(db.scalars(select(models.PrescriptionItem).where(
            models.PrescriptionItem.prescription_id == p.id)))
    return data


@router.get("", response_model=list[schemas.PrescriptionOut], summary="List prescriptions")
def list_prescriptions(db: Session = Depends(get_db), patient_id: int | None = None,
                       doctor_id: int | None = None, limit: int = Query(100, le=500),
                       user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Prescription)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Prescription.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("prescriptions:read"):
        raise HTTPException(status_code=403, detail="Missing permission: prescriptions:read")
    if patient_id:
        stmt = stmt.where(models.Prescription.patient_id == patient_id)
    if doctor_id:
        stmt = stmt.where(models.Prescription.doctor_id == doctor_id)
    rows = db.scalars(stmt.order_by(models.Prescription.issued_at.desc()).limit(limit)).all()
    return [_out(db, p) for p in rows]


@router.post("", response_model=schemas.PrescriptionOut, status_code=201, summary="Issue a prescription")
def create_prescription(payload: schemas.PrescriptionCreate, request: Request, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(require_permission("prescriptions:write"))):
    patient = db.get(models.Patient, payload.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    doctor_id = payload.doctor_id
    if user.role == "DOCTOR":
        doctor = db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))
        if not doctor:
            raise HTTPException(status_code=400, detail="No doctor profile linked to this login")
        doctor_id = doctor.id
    if not doctor_id:
        raise HTTPException(status_code=422, detail="doctor_id is required")

    prescription = models.Prescription(
        prescription_code=_code(db), consultation_id=payload.consultation_id,
        patient_id=payload.patient_id, doctor_id=doctor_id,
        diagnosis_summary=payload.diagnosis_summary, notes=payload.notes, advice=payload.advice,
        status="ISSUED", issued_at=utcnow(), valid_until=date.today() + timedelta(days=payload.valid_days),
    )
    db.add(prescription)
    db.commit()
    db.refresh(prescription)

    for item in payload.items:
        db.add(models.PrescriptionItem(
            prescription_id=prescription.id, medicine_id=item.medicine_id,
            medicine_name=item.medicine_name, dosage=item.dosage, frequency=item.frequency,
            duration=item.duration, instructions=item.instructions, quantity=item.quantity,
        ))
    db.commit()
    db.refresh(prescription)

    try:
        documents.prescription_pdf(db, prescription)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("vvh.prescriptions").warning("PDF generation failed: %s", exc)

    if payload.notify_patient:
        doctor = db.get(models.Doctor, doctor_id)
        notify.notify_patient(db, patient, "PRESCRIPTION_READY", {
            "patient_name": patient.full_name,
            "code": prescription.prescription_code,
            "doctor_name": doctor.full_name if doctor else "",
        }, channels=["WHATSAPP", "EMAIL", "IN_APP"])

    audit(db, action="PRESCRIPTION_ISSUE", resource="prescription", resource_id=prescription.id, user=user,
          request=request, new_value={"code": prescription.prescription_code, "patient_id": patient.id,
                                      "items": len(payload.items)})
    return _out(db, prescription)


@router.get("/{prescription_id}", response_model=schemas.PrescriptionOut, summary="Prescription detail")
def prescription_detail(prescription_id: int, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    prescription = db.get(models.Prescription, prescription_id)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or prescription.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return _out(db, prescription)


@router.get("/{prescription_id}/pdf", summary="Download prescription PDF")
def prescription_pdf(prescription_id: int, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    prescription = db.get(models.Prescription, prescription_id)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or prescription.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    path = documents.prescription_pdf(db, prescription)
    audit(db, action="PRESCRIPTION_PDF", resource="prescription", resource_id=prescription_id, user=user)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{prescription.prescription_code}.pdf")


@router.post("/{prescription_id}/cancel", response_model=schemas.PrescriptionOut, summary="Cancel")
def cancel_prescription(prescription_id: int, request: Request, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(require_permission("prescriptions:write"))):
    prescription = db.get(models.Prescription, prescription_id)
    if not prescription:
        raise HTTPException(status_code=404, detail="Prescription not found")
    prescription.status = "CANCELLED"
    db.commit()
    audit(db, action="PRESCRIPTION_CANCEL", resource="prescription", resource_id=prescription_id, user=user,
          request=request)
    return _out(db, prescription)


@router.get("/patient/{patient_id}/history", summary="Prescription history for a patient")
def prescription_history(patient_id: int, db: Session = Depends(get_db),
                         user: CurrentUser = Depends(get_current_user)):
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or patient.id != patient_id:
            raise HTTPException(status_code=403, detail="Not allowed")
    rows = db.scalars(select(models.Prescription).where(
        models.Prescription.patient_id == patient_id).order_by(models.Prescription.issued_at.desc())).all()
    return [_out(db, p) for p in rows]
