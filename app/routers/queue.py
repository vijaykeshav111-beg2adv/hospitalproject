"""Reception queue: check-in, check-out, waiting queue and status flow.

WAITING -> CHECKED_IN -> IN_CONSULTATION -> COMPLETED (or NO_SHOW)
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, require_permission
from ..security import utcnow
from ..services import analytics, notify

router = APIRouter(prefix="/queue", tags=["Reception & Queue"])


def _entry(db: Session, a: models.Appointment) -> dict:
    waiting = None
    if a.checked_in_at and a.queue_status in {"CHECKED_IN", "IN_CONSULTATION"}:
        waiting = int((utcnow() - a.checked_in_at).total_seconds() // 60)
    return {
        "appointment_id": a.id, "appointment_code": a.appointment_code, "token_number": a.token_number,
        "patient_id": a.patient_id, "patient_name": a.patient.full_name if a.patient else None,
        "patient_phone": a.patient.phone if a.patient else None,
        "doctor_name": a.doctor.full_name if a.doctor else None,
        "appointment_date": a.appointment_date, "start_time": a.start_time, "status": a.status,
        "queue_status": a.queue_status, "waiting_minutes": waiting,
        "invoice_id": db.scalar(select(models.Invoice.id).where(models.Invoice.appointment_id == a.id)),
    }


@router.get("/today", summary="Today's queue")
def today_queue(db: Session = Depends(get_db), doctor_id: int | None = None,
                queue_status: str | None = None,
                _: CurrentUser = Depends(require_permission("queue:manage", "appointments:read",
                                                            any_of=True))):
    stmt = select(models.Appointment).where(models.Appointment.appointment_date == date.today())
    if doctor_id:
        stmt = stmt.where(models.Appointment.doctor_id == doctor_id)
    if queue_status:
        stmt = stmt.where(models.Appointment.queue_status == queue_status.upper())
    rows = db.scalars(stmt.order_by(models.Appointment.start_time, models.Appointment.token_number)).all()
    return {"date": date.today().isoformat(), "count": len(rows), "queue": [_entry(db, a) for a in rows]}


@router.get("/desk", summary="Reception desk snapshot (cards + queue + doctor availability)")
def reception_desk(db: Session = Depends(get_db),
                   _: CurrentUser = Depends(require_permission("dashboard:reception"))):
    return analytics.reception_desk(db)


@router.post("/check-in", summary="Check a patient in")
def check_in(payload: schemas.QueueAction, request: Request, db: Session = Depends(get_db),
             user: CurrentUser = Depends(require_permission("queue:manage"))):
    appointment = db.get(models.Appointment, payload.appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appointment.status in {"CANCELLED", "NO_SHOW"}:
        raise HTTPException(status_code=409, detail=f"Cannot check in a {appointment.status} appointment")
    if appointment.queue_status in {"CHECKED_IN", "IN_CONSULTATION"}:
        raise HTTPException(status_code=409, detail="Patient is already checked in")

    before = {"queue_status": appointment.queue_status}
    appointment.queue_status = "CHECKED_IN"
    appointment.checked_in_at = utcnow()
    if appointment.status == "PENDING":
        appointment.status = "CONFIRMED"
    db.commit()
    audit(db, action="QUEUE_CHECK_IN", resource="appointment", resource_id=appointment.id, user=user,
          request=request, previous_value=before, new_value={"queue_status": "CHECKED_IN"})
    return {"success": True, **_entry(db, appointment)}


@router.post("/start-consultation", summary="Move a patient into consultation")
def start_consultation(payload: schemas.QueueAction, request: Request, db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require_permission("queue:manage", "consultations:write",
                                                                      any_of=True))):
    appointment = db.get(models.Appointment, payload.appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appointment.queue_status = "IN_CONSULTATION"
    if not appointment.checked_in_at:
        appointment.checked_in_at = utcnow()
    db.commit()
    audit(db, action="QUEUE_IN_CONSULTATION", resource="appointment", resource_id=appointment.id,
          user=user, request=request)
    return {"success": True, **_entry(db, appointment)}


@router.post("/check-out", summary="Check a patient out (completes the visit)")
def check_out(payload: schemas.QueueAction, request: Request, db: Session = Depends(get_db),
              user: CurrentUser = Depends(require_permission("queue:manage"))):
    appointment = db.get(models.Appointment, payload.appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appointment.queue_status = "COMPLETED"
    appointment.status = "COMPLETED"
    appointment.checked_out_at = utcnow()
    db.commit()
    invoice = db.scalar(select(models.Invoice).where(models.Invoice.appointment_id == appointment.id))
    audit(db, action="QUEUE_CHECK_OUT", resource="appointment", resource_id=appointment.id, user=user,
          request=request, new_value={"queue_status": "COMPLETED"})
    return {
        "success": True, **_entry(db, appointment),
        "invoice": {"id": invoice.id, "number": invoice.invoice_number, "total": float(invoice.total_amount),
                    "balance": float(invoice.balance_amount), "status": invoice.status} if invoice else None,
    }


@router.post("/no-show", summary="Mark a queue entry as no-show")
def mark_no_show(payload: schemas.QueueAction, request: Request, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_permission("queue:manage"))):
    appointment = db.get(models.Appointment, payload.appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appointment.queue_status = "NO_SHOW"
    appointment.status = "NO_SHOW"
    db.commit()
    audit(db, action="QUEUE_NO_SHOW", resource="appointment", resource_id=appointment.id, user=user,
          request=request)
    return {"success": True, **_entry(db, appointment)}


@router.get("/waiting", summary="Patients currently waiting")
def waiting(db: Session = Depends(get_db),
            _: CurrentUser = Depends(require_permission("queue:manage", "appointments:read", any_of=True))):
    rows = db.scalars(select(models.Appointment).where(
        models.Appointment.appointment_date == date.today(),
        models.Appointment.queue_status.in_(["WAITING", "CHECKED_IN", "IN_CONSULTATION"]),
    ).order_by(models.Appointment.start_time)).all()
    return [_entry(db, a) for a in rows]


@router.post("/call-next/{doctor_id}", summary="Call the next waiting patient for a doctor")
def call_next(doctor_id: int, request: Request, db: Session = Depends(get_db),
              user: CurrentUser = Depends(require_permission("queue:manage"))):
    appointment = db.scalar(
        select(models.Appointment).where(
            models.Appointment.doctor_id == doctor_id,
            models.Appointment.appointment_date == date.today(),
            models.Appointment.queue_status.in_(["WAITING", "CHECKED_IN"]),
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        ).order_by(models.Appointment.token_number, models.Appointment.start_time).limit(1)
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="No waiting patients for this doctor")
    appointment.queue_status = "IN_CONSULTATION"
    if not appointment.checked_in_at:
        appointment.checked_in_at = utcnow()
    db.commit()
    patient = db.get(models.Patient, appointment.patient_id)
    if patient:
        notify.notify_patient(
            db,
            patient,
            "CUSTOM",
            {"message": f"{patient.full_name}, it is your turn now. Please proceed to the "
                         f"doctor's room. Token {appointment.token_number}."},
            channels=["WHATSAPP", "EMAIL", "IN_APP"],
            appointment_id=appointment.id,
        )
    audit(db, action="QUEUE_CALL_NEXT", resource="appointment", resource_id=appointment.id, user=user,
          request=request)
    return {"success": True, **_entry(db, appointment)}
