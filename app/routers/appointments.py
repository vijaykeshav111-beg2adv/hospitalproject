"""Appointments: create, confirm, reschedule, cancel, complete, no-show, history, status."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..codes import unique_code
from ..database import get_db
from ..deps import (
    CurrentUser,
    audit,
    get_current_user,
    patient_for_user,
    require_permission,
)
from ..security import utcnow
from ..services import billing, notify, slots as slot_service

router = APIRouter(
    prefix="/appointments",
    tags=["Appointments"],
)

STATUSES = [
    "PENDING",
    "CONFIRMED",
    "COMPLETED",
    "CANCELLED",
    "NO_SHOW",
    "RESCHEDULED",
]


def _code(db: Session) -> str:
    """Collision-safe appointment code (MySQL UNIQUE on appointment_code).

    A 4-digit random suffix collides often enough to break a booking with
    IntegrityError 1062, so the code is verified against the database first.
    """
    return unique_code(
        db,
        prefix="APT",
        model=models.Appointment,
        column=models.Appointment.appointment_code,
    )


def _out(a: models.Appointment) -> dict:
    consultation = None

    return {
        "id": a.id,
        "appointment_code": a.appointment_code,
        "patient_id": a.patient_id,
        "patient_name": a.patient.full_name if a.patient else None,
        "patient_code": a.patient.patient_code if a.patient else None,
        "patient_phone": a.patient.phone if a.patient else None,
        "doctor_id": a.doctor_id,
        "doctor_name": a.doctor.full_name if a.doctor else None,
        "specialty_name": a.specialty.name if a.specialty else None,
        "appointment_date": a.appointment_date,
        "start_time": a.start_time,
        "end_time": a.end_time,
        "token_number": a.token_number,
        "status": a.status,
        "queue_status": a.queue_status,
        "source": a.source,
        "reason": a.reason,
        "consultation_id": consultation,
        "checked_in_at": a.checked_in_at,
        "checked_out_at": a.checked_out_at,
        "created_at": a.created_at,
        "invoice_id": None,
    }


def _guard_patient_scope(
    user: CurrentUser,
    db: Session,
    appointment: models.Appointment,
) -> None:
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)

        if not patient or appointment.patient_id != patient.id:
            raise HTTPException(
                status_code=403,
                detail="You can only access your own appointments",
            )


@router.get(
    "",
    response_model=list[schemas.AppointmentOut],
    summary="List / filter appointments",
)
def list_appointments(
    db: Session = Depends(get_db),
    patient_id: int | None = None,
    doctor_id: int | None = None,
    specialty_id: int | None = None,
    status_filter: str | None = None,
    queue_status: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    search: str | None = None,
    limit: int = Query(100, le=500),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(models.Appointment)

    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)

        if not patient:
            return []

        stmt = stmt.where(
            models.Appointment.patient_id == patient.id
        )

    elif user.role == "DOCTOR":
        doctor = db.scalar(
            select(models.Doctor).where(
                models.Doctor.user_id == user.id
            )
        )

        stmt = (
            stmt.where(
                models.Appointment.doctor_id == doctor.id
            )
            if doctor
            else stmt.where(False)
        )

    elif not user.has_permission("appointments:read"):
        raise HTTPException(
            status_code=403,
            detail="Missing permission: appointments:read",
        )

    if patient_id:
        stmt = stmt.where(
            models.Appointment.patient_id == patient_id
        )

    if doctor_id:
        stmt = stmt.where(
            models.Appointment.doctor_id == doctor_id
        )

    if specialty_id:
        stmt = stmt.where(
            models.Appointment.specialty_id == specialty_id
        )

    if status_filter:
        stmt = stmt.where(
            models.Appointment.status == status_filter.upper()
        )

    if queue_status:
        stmt = stmt.where(
            models.Appointment.queue_status == queue_status.upper()
        )

    if from_date:
        stmt = stmt.where(
            models.Appointment.appointment_date >= from_date
        )

    if to_date:
        stmt = stmt.where(
            models.Appointment.appointment_date <= to_date
        )

    if search:
        like = f"%{search}%"

        stmt = stmt.join(models.Patient).where(
            or_(
                models.Appointment.appointment_code.like(like),
                models.Patient.full_name.like(like),
                models.Patient.patient_code.like(like),
                models.Patient.phone.like(like),
            )
        )

    rows = db.scalars(
        stmt.order_by(
            models.Appointment.appointment_date.desc(),
            models.Appointment.start_time.desc(),
        ).limit(limit)
    ).all()

    out = []

    for a in rows:
        row = _out(a)

        row["invoice_id"] = db.scalar(
            select(models.Invoice.id).where(
                models.Invoice.appointment_id == a.id
            )
        )

        consultation = db.scalar(
            select(models.Consultation)
            .where(
                models.Consultation.appointment_id == a.id
            )
            .order_by(models.Consultation.id.desc())
        )

        row["consultation_id"] = (
            consultation.id
            if consultation
            else None
        )

        out.append(row)

    return out


@router.get(
    "/today",
    response_model=list[schemas.AppointmentOut],
    summary="Today's appointments",
)
def todays_appointments(
    db: Session = Depends(get_db),
    doctor_id: int | None = None,
    limit: int = Query(100, le=500),
    user: CurrentUser = Depends(get_current_user),
):
    return list_appointments(
        db=db,
        doctor_id=doctor_id,
        from_date=date.today(),
        to_date=date.today(),
        limit=limit,
        user=user,
    )


@router.post(
    "",
    response_model=schemas.AppointmentOut,
    status_code=201,
    summary="Book an appointment",
)
def create_appointment(
    payload: schemas.AppointmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission("appointments:write")
    ),
):
    # ---------------------------------------------------------
    # 1. Validate patient
    # ---------------------------------------------------------
    patient = db.get(
        models.Patient,
        payload.patient_id,
    )

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    # ---------------------------------------------------------
    # 2. Resolve slot / doctor / appointment time
    # ---------------------------------------------------------
    slot = (
        db.get(models.Slot, payload.slot_id)
        if payload.slot_id
        else None
    )

    doctor = None

    if slot:
        doctor = db.get(
            models.Doctor,
            slot.doctor_id,
        )

        # Slot is completely booked.
        if (
            slot.status == "BOOKED"
            and slot.booked_count >= slot.capacity
        ):
            raise HTTPException(
                status_code=409,
                detail="Slot already booked",
            )

        # Slot is blocked by leave / holiday.
        if slot.status == "BLOCKED":
            raise HTTPException(
                status_code=409,
                detail="Slot is blocked (leave/holiday)",
            )

        # A hold token was supplied but does not match.
        if (
            payload.hold_token
            and slot.hold_token
            and payload.hold_token != slot.hold_token
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Hold token mismatch - "
                    "slot may have expired"
                ),
            )

        appt_date = slot.slot_date
        start_time = slot.start_time
        end_time = slot.end_time

        if slot_service.slot_has_started(slot):
            raise HTTPException(
                status_code=409,
                detail="Cannot book an appointment slot that has already started or passed",
            )

    else:
        doctor = (
            db.get(
                models.Doctor,
                payload.doctor_id,
            )
            if payload.doctor_id
            else None
        )

        if (
            not doctor
            or not payload.appointment_date
            or not payload.start_time
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Provide slot_id, or "
                    "doctor_id + date + time"
                ),
            )

        appt_date = payload.appointment_date
        start_time = payload.start_time

        duration = (
            doctor.slot_duration_minutes
            or 30
        )

        end_time = (
            datetime.combine(
                appt_date,
                start_time,
            )
            + timedelta(minutes=duration)
        ).time()

        # Appointment times are hospital-local (Asia/Kolkata by default).
        if appt_date < slot_service.hospital_now().date() or (
            appt_date == slot_service.hospital_now().date()
            and start_time <= slot_service.hospital_now().time()
        ):
            raise HTTPException(
                status_code=409,
                detail="Cannot book an appointment time that has already started or passed",
            )

    # ---------------------------------------------------------
    # 3. Validate doctor
    # ---------------------------------------------------------
    if doctor is None or not doctor.is_active:
        raise HTTPException(
            status_code=400,
            detail="Doctor is not available",
        )

    # ---------------------------------------------------------
    # 4. Prevent duplicate booking of THE SAME SLOT
    #
    # IMPORTANT:
    # We intentionally do NOT block the patient from having
    # another appointment with the same doctor on the same day.
    #
    # Example:
    #   10:00 slot -> existing appointment
    #   11:00 slot -> new appointment
    #
    # This is allowed.
    #
    # But:
    #   same patient + same slot -> rejected
    # ---------------------------------------------------------
    if slot:
        duplicate = db.scalar(
            select(models.Appointment).where(
                models.Appointment.patient_id
                == patient.id,
                models.Appointment.slot_id
                == slot.id,
                models.Appointment.status.in_(
                    ["PENDING", "CONFIRMED"]
                ),
            )
        )

        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Patient already has appointment "
                    f"{duplicate.appointment_code} "
                    f"for this slot"
                ),
            )

    # ---------------------------------------------------------
    # 5. For bookings without slot_id, prevent exact
    #    doctor/date/time duplicate.
    # ---------------------------------------------------------
    else:
        duplicate = db.scalar(
            select(models.Appointment).where(
                models.Appointment.patient_id
                == patient.id,
                models.Appointment.doctor_id
                == doctor.id,
                models.Appointment.appointment_date
                == appt_date,
                models.Appointment.start_time
                == start_time,
                models.Appointment.status.in_(
                    ["PENDING", "CONFIRMED"]
                ),
            )
        )

        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Patient already has appointment "
                    f"{duplicate.appointment_code} "
                    f"for this time"
                ),
            )

    # ---------------------------------------------------------
    # 6. Generate queue token
    # ---------------------------------------------------------
    token = (
        db.scalar(
            select(
                func.coalesce(
                    func.max(
                        models.Appointment.token_number
                    ),
                    0,
                )
            ).where(
                models.Appointment.doctor_id
                == doctor.id,
                models.Appointment.appointment_date
                == appt_date,
            )
        )
        or 0
    ) + 1

    # ---------------------------------------------------------
    # 7. Create appointment
    # ---------------------------------------------------------
    appointment = models.Appointment(
        appointment_code=_code(db),
        patient_id=patient.id,
        doctor_id=doctor.id,
        specialty_id=doctor.specialty_id,
        slot_id=slot.id if slot else None,
        appointment_date=appt_date,
        start_time=start_time,
        end_time=end_time,
        token_number=token,
        status="CONFIRMED",
        queue_status="WAITING",
        source=payload.source,
        reason=payload.reason,
        booked_by=user.id,
        follow_up_of=payload.follow_up_of,
    )

    db.add(appointment)

    db.commit()
    db.refresh(appointment)

    # ---------------------------------------------------------
    # 8. Mark slot as booked
    # ---------------------------------------------------------
    if slot:
        slot_service.mark_slot_booked(
            db,
            slot,
        )

    # ---------------------------------------------------------
    # 9. Generate consultation invoice
    # ---------------------------------------------------------
    invoice = billing.ensure_consultation_invoice(
        db,
        appointment,
        doctor,
    )

    # ---------------------------------------------------------
    # 10. Send notifications
    # ---------------------------------------------------------
    if payload.send_notifications:
        notify.notify_patient(
            db,
            patient,
            "APPOINTMENT_CONFIRMATION",
            {
                "patient_name": patient.full_name,
                "doctor_name": doctor.full_name,
                "specialty": (
                    doctor.specialty.name
                    if doctor.specialty
                    else ""
                ),
                "date": appt_date.strftime(
                    "%d %b %Y"
                ),
                "time": start_time.strftime(
                    "%H:%M"
                ),
                "token": token,
                "fee": f"{float(doctor.consultation_fee or 0):.2f}",
                "code": appointment.appointment_code,
            },
            channels=[
                "WHATSAPP",
                "IN_APP",
            ],
            appointment_id=appointment.id,
        )

    # ---------------------------------------------------------
    # 11. Audit
    # ---------------------------------------------------------
    audit(
        db,
        action="APPOINTMENT_CREATE",
        resource="appointment",
        resource_id=appointment.id,
        user=user,
        request=request,
        new_value={
            "code": appointment.appointment_code,
            "doctor_id": doctor.id,
            "date": str(appt_date),
            "time": start_time.strftime("%H:%M"),
            "source": payload.source,
            "invoice": invoice.invoice_number,
        },
    )

    # ---------------------------------------------------------
    # 12. Response
    # ---------------------------------------------------------
    row = _out(appointment)
    row["invoice_id"] = invoice.id

    return row


@router.get(
    "/{appointment_id}",
    response_model=schemas.AppointmentOut,
    summary="Appointment detail",
)
def appointment_detail(
    appointment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    _guard_patient_scope(
        user,
        db,
        appointment,
    )

    row = _out(appointment)

    row["invoice_id"] = db.scalar(
        select(models.Invoice.id).where(
            models.Invoice.appointment_id
            == appointment.id
        )
    )

    return row


@router.post(
    "/{appointment_id}/confirm",
    response_model=schemas.AppointmentOut,
    summary="Confirm",
)
def confirm_appointment(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission("appointments:write")
    ),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    before = {
        "status": appointment.status
    }

    appointment.status = "CONFIRMED"

    db.commit()

    audit(
        db,
        action="APPOINTMENT_CONFIRM",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        previous_value=before,
        new_value={
            "status": "CONFIRMED"
        },
    )

    return _out(appointment)


@router.post(
    "/{appointment_id}/reschedule",
    response_model=schemas.AppointmentOut,
    summary="Reschedule to another slot",
)
def reschedule(
    appointment_id: int,
    payload: schemas.AppointmentReschedule,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission("appointments:write")
    ),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    new_slot = db.get(
        models.Slot,
        payload.new_slot_id,
    )

    if not new_slot:
        raise HTTPException(
            status_code=404,
            detail="New slot not found",
        )

    if slot_service.slot_has_started(new_slot):
        raise HTTPException(
            status_code=409,
            detail="Cannot reschedule to an appointment slot that has already started or passed",
        )

    if (
        new_slot.status in {"BOOKED", "BLOCKED"}
        and new_slot.booked_count >= new_slot.capacity
    ):
        raise HTTPException(
            status_code=409,
            detail="New slot is not available",
        )

    before = {
        "date": str(
            appointment.appointment_date
        ),
        "time": appointment.start_time.strftime(
            "%H:%M"
        ),
        "doctor_id": appointment.doctor_id,
    }

    if appointment.slot_id:
        old_slot = db.get(
            models.Slot,
            appointment.slot_id,
        )

        if old_slot:
            slot_service.free_slot(
                db,
                old_slot,
            )

    doctor = db.get(
        models.Doctor,
        new_slot.doctor_id,
    )

    appointment.slot_id = new_slot.id
    appointment.doctor_id = new_slot.doctor_id
    appointment.specialty_id = (
        doctor.specialty_id
        if doctor
        else appointment.specialty_id
    )
    appointment.appointment_date = new_slot.slot_date
    appointment.start_time = new_slot.start_time
    appointment.end_time = new_slot.end_time
    appointment.status = "CONFIRMED"
    appointment.queue_status = "WAITING"

    appointment.token_number = (
        db.scalar(
            select(
                func.coalesce(
                    func.max(
                        models.Appointment.token_number
                    ),
                    0,
                )
            ).where(
                models.Appointment.doctor_id
                == new_slot.doctor_id,
                models.Appointment.appointment_date
                == new_slot.slot_date,
            )
        )
        or 0
    ) + 1

    db.commit()

    slot_service.mark_slot_booked(
        db,
        new_slot,
    )

    if payload.send_notifications:
        notify.notify_patient(
            db,
            appointment.patient,
            "APPOINTMENT_RESCHEDULED",
            {
                "patient_name": appointment.patient.full_name,
                "date": appointment.appointment_date.strftime(
                    "%d %b %Y"
                ),
                "time": appointment.start_time.strftime(
                    "%H:%M"
                ),
                "doctor_name": (
                    doctor.full_name
                    if doctor
                    else ""
                ),
                "code": appointment.appointment_code,
            },
            channels=[
                "WHATSAPP",
                "IN_APP",
            ],
            appointment_id=appointment.id,
        )

    audit(
        db,
        action="APPOINTMENT_RESCHEDULE",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        previous_value=before,
        new_value={
            "date": str(
                appointment.appointment_date
            ),
            "time": appointment.start_time.strftime(
                "%H:%M"
            ),
            "doctor_id": appointment.doctor_id,
            "reason": payload.reason,
        },
    )

    return _out(appointment)


@router.post(
    "/{appointment_id}/cancel",
    response_model=schemas.AppointmentOut,
    summary="Cancel",
)
def cancel(
    appointment_id: int,
    payload: schemas.AppointmentCancel,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    _guard_patient_scope(
        user,
        db,
        appointment,
    )

    if (
        user.role == "PATIENT"
        and not user.has_permission(
            "appointments:cancel"
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Missing permission: appointments:cancel",
        )

    if appointment.status in {
        "CANCELLED",
        "COMPLETED",
    }:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Appointment already "
                f"{appointment.status}"
            ),
        )

    before = {
        "status": appointment.status
    }

    appointment.status = "CANCELLED"
    appointment.cancellation_reason = (
        payload.reason or "Cancelled"
    )
    appointment.cancelled_by = user.id

    db.commit()

    if appointment.slot_id:
        slot = db.get(
            models.Slot,
            appointment.slot_id,
        )

        if slot:
            slot_service.free_slot(
                db,
                slot,
            )

    if payload.send_notifications:
        notify.notify_patient(
            db,
            appointment.patient,
            "APPOINTMENT_CANCELLED",
            {
                "patient_name": appointment.patient.full_name,
                "code": appointment.appointment_code,
                "date": appointment.appointment_date.strftime(
                    "%d %b %Y"
                ),
                "time": appointment.start_time.strftime(
                    "%H:%M"
                ),
                "doctor_name": (
                    appointment.doctor.full_name
                    if appointment.doctor
                    else ""
                ),
                "reason": appointment.cancellation_reason,
            },
            channels=[
                "WHATSAPP",
                "IN_APP",
            ],
            appointment_id=appointment.id,
        )

    audit(
        db,
        action="APPOINTMENT_CANCEL",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        previous_value=before,
        new_value={
            "status": "CANCELLED",
            "reason": appointment.cancellation_reason,
        },
    )

    return _out(appointment)


@router.post(
    "/{appointment_id}/complete",
    response_model=schemas.AppointmentOut,
    summary="Complete",
)
def complete(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission(
            "appointments:write",
            "consultations:write",
            any_of=True,
        )
    ),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    appointment.status = "COMPLETED"
    appointment.queue_status = "COMPLETED"
    appointment.checked_out_at = utcnow()

    db.commit()

    audit(
        db,
        action="APPOINTMENT_COMPLETE",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        new_value={
            "status": "COMPLETED"
        },
    )

    return _out(appointment)


@router.post(
    "/{appointment_id}/no-show",
    response_model=schemas.AppointmentOut,
    summary="Mark no-show",
)
def no_show(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission(
            "queue:manage",
            "appointments:write",
            any_of=True,
        )
    ),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    appointment.status = "NO_SHOW"
    appointment.queue_status = "NO_SHOW"

    db.commit()

    audit(
        db,
        action="APPOINTMENT_NO_SHOW",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        new_value={
            "status": "NO_SHOW"
        },
    )

    return _out(appointment)


@router.patch(
    "/{appointment_id}/status",
    response_model=schemas.AppointmentOut,
    summary="Set status",
)
def set_status(
    appointment_id: int,
    payload: schemas.AppointmentStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(
        require_permission("appointments:write")
    ),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    before = {
        "status": appointment.status
    }

    appointment.status = payload.status

    if payload.status == "COMPLETED":
        appointment.queue_status = "COMPLETED"

    db.commit()

    audit(
        db,
        action="APPOINTMENT_STATUS",
        resource="appointment",
        resource_id=appointment_id,
        user=user,
        request=request,
        previous_value=before,
        new_value={
            "status": payload.status
        },
    )

    return _out(appointment)


@router.get(
    "/{appointment_id}/history",
    summary="Status change + notification trail",
)
def appointment_history(
    appointment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    _guard_patient_scope(
        user,
        db,
        appointment,
    )

    logs = db.scalars(
        select(models.AuditLog)
        .where(
            models.AuditLog.resource
            == "appointment",
            models.AuditLog.resource_id
            == str(appointment_id),
        )
        .order_by(
            models.AuditLog.created_at.desc()
        )
    ).all()

    notifs = db.scalars(
        select(models.Notification)
        .where(
            models.Notification.patient_id
            == appointment.patient_id
        )
        .order_by(
            models.Notification.created_at.desc()
        )
        .limit(10)
    ).all()

    return {
        "appointment": _out(appointment),
        "audit": [
            {
                "action": l.action,
                "at": l.created_at.isoformat(),
                "by": l.user_email,
                "old": l.previous_value,
                "new": l.new_value,
            }
            for l in logs
        ],
        "notifications": [
            {
                "template": n.template,
                "channel": n.channel,
                "status": n.status,
                "at": (
                    n.created_at.isoformat()
                    if n.created_at
                    else None
                ),
            }
            for n in notifs
        ],
    }


@router.get(
    "/{appointment_id}/consultation-link",
    summary="Link appointment -> consultation",
)
def consultation_link(
    appointment_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    appointment = db.get(
        models.Appointment,
        appointment_id,
    )

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found",
        )

    _guard_patient_scope(
        user,
        db,
        appointment,
    )

    consultation = db.scalar(
        select(models.Consultation)
        .where(
            models.Consultation.appointment_id
            == appointment_id
        )
        .order_by(
            models.Consultation.id.desc()
        )
    )

    return {
        "appointment_id": appointment_id,
        "consultation_id": (
            consultation.id
            if consultation
            else None
        ),
        "consultation_code": (
            consultation.consultation_code
            if consultation
            else None
        ),
        "status": (
            consultation.status
            if consultation
            else "NOT_STARTED"
        ),
        "start_url": (
            "/api/v1/consultations"
            if not consultation
            else f"/api/v1/consultations/{consultation.id}"
        ),
    }