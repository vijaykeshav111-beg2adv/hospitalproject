"""Doctor leave (full day, partial, multi-day, emergency, holiday) + holidays master."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, require_permission
from ..services import notify, slots as slot_service

router = APIRouter(tags=["Leave & Holidays"])


@router.get("/leaves", response_model=list[schemas.DoctorLeaveOut], summary="List leaves")
def list_leaves(db: Session = Depends(get_db), doctor_id: int | None = None, upcoming_only: bool = False,
                status_filter: str | None = None, _: CurrentUser = Depends(get_current_user)):
    stmt = select(models.DoctorLeave)
    if doctor_id:
        stmt = stmt.where(models.DoctorLeave.doctor_id == doctor_id)
    if upcoming_only:
        stmt = stmt.where(models.DoctorLeave.end_date >= date.today())
    if status_filter:
        stmt = stmt.where(models.DoctorLeave.status == status_filter.upper())
    return list(db.scalars(stmt.order_by(models.DoctorLeave.start_date.desc()).limit(200)))


@router.post("/leaves", response_model=schemas.DoctorLeaveOut, status_code=201, summary="Apply leave")
def apply_leave(doctor_id: int, payload: schemas.DoctorLeaveCreate, request: Request,
                db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("schedules:write"))):
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    end_date = payload.end_date or payload.start_date
    if end_date < payload.start_date:
        raise HTTPException(status_code=422, detail="end_date cannot be before start_date")
    if payload.leave_type == "PARTIAL" and not (payload.start_time and payload.end_time):
        raise HTTPException(status_code=422, detail="Partial leave needs start_time and end_time")

    leave = models.DoctorLeave(
        doctor_id=doctor_id, leave_type=payload.leave_type, start_date=payload.start_date,
        end_date=end_date, start_time=payload.start_time, end_time=payload.end_time,
        reason=payload.reason,
        status="APPROVED" if user.role in {"ADMIN", "SUPER_ADMIN"} else "PENDING",
        approved_by=user.id if user.role in {"ADMIN", "SUPER_ADMIN"} else None,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)

    if leave.status == "APPROVED":
        slot_service.block_slots_for_leave(db, leave)
        _notify_affected_patients(db, leave)

    audit(db, action="LEAVE_APPLY", resource="doctor_leave", resource_id=leave.id, user=user,
          request=request, new_value={"doctor_id": doctor_id, "type": leave.leave_type,
                                      "from": str(leave.start_date), "to": str(leave.end_date),
                                      "slots_blocked": leave.slots_blocked})
    return leave


def _notify_affected_patients(db: Session, leave: models.DoctorLeave) -> int:
    appointments = db.scalars(
        select(models.Appointment).where(
            models.Appointment.doctor_id == leave.doctor_id,
            models.Appointment.appointment_date >= leave.start_date,
            models.Appointment.appointment_date <= (leave.end_date or leave.start_date),
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        )
    ).all()
    doctor = db.get(models.Doctor, leave.doctor_id)
    for appt in appointments:
        notify.notify_patient(db, appt.patient, "APPOINTMENT_CANCELLED", {
            "patient_name": appt.patient.full_name,
            "code": appt.appointment_code,
            "date": appt.appointment_date.strftime("%d %b %Y"),
            "time": appt.start_time.strftime("%H:%M"),
            "doctor_name": doctor.full_name if doctor else "",
            "reason": f"doctor on {leave.leave_type} leave",
        }, channels=["WHATSAPP", "IN_APP"], appointment_id=appt.id)
    return len(appointments)


@router.post("/leaves/{leave_id}/approve", response_model=schemas.DoctorLeaveOut, summary="Approve leave")
def approve_leave(leave_id: int, request: Request, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_permission("schedules:write"))):
    leave = db.get(models.DoctorLeave, leave_id)
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    leave.status = "APPROVED"
    leave.approved_by = user.id
    db.commit()
    blocked = slot_service.block_slots_for_leave(db, leave)
    notified = _notify_affected_patients(db, leave)
    audit(db, action="LEAVE_APPROVE", resource="doctor_leave", resource_id=leave_id, user=user,
          request=request, new_value={"slots_blocked": blocked, "patients_notified": notified})
    db.refresh(leave)
    return leave


@router.post("/leaves/{leave_id}/reject", response_model=schemas.DoctorLeaveOut, summary="Reject leave")
def reject_leave(leave_id: int, request: Request, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_permission("schedules:write"))):
    leave = db.get(models.DoctorLeave, leave_id)
    if not leave:
        raise HTTPException(status_code=404, detail="Leave not found")
    leave.status = "REJECTED"
    db.commit()
    audit(db, action="LEAVE_REJECT", resource="doctor_leave", resource_id=leave_id, user=user, request=request)
    db.refresh(leave)
    return leave


@router.post("/leaves/emergency", summary="Emergency leave: block slots + alert affected patients")
def emergency_leave(doctor_id: int, payload: schemas.DoctorLeaveCreate, request: Request,
                    db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("schedules:write"))):
    payload.leave_type = "EMERGENCY"
    leave = apply_leave(doctor_id, payload, request, db, user)
    return {"leave_id": leave.id, "slots_blocked": leave.slots_blocked,
            "message": "Emergency leave recorded, slots blocked and patients notified"}


# --------------------------------------------------------------------------
# holidays
# --------------------------------------------------------------------------
@router.get("/holidays", response_model=list[schemas.HolidayOut], summary="List holidays")
def list_holidays(db: Session = Depends(get_db), upcoming_only: bool = False,
                  _: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Holiday)
    if upcoming_only:
        stmt = stmt.where(models.Holiday.holiday_date >= date.today())
    return list(db.scalars(stmt.order_by(models.Holiday.holiday_date)))


@router.post("/holidays", response_model=schemas.HolidayOut, status_code=201, summary="Add holiday")
def add_holiday(payload: schemas.HolidayCreate, request: Request, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("schedules:write"))):
    if db.scalar(select(models.Holiday).where(models.Holiday.holiday_date == payload.holiday_date)):
        raise HTTPException(status_code=409, detail="Holiday already exists for this date")
    holiday = models.Holiday(**payload.model_dump())
    db.add(holiday)
    db.commit()
    db.refresh(holiday)
    audit(db, action="HOLIDAY_CREATE", resource="holiday", resource_id=holiday.id, user=user,
          request=request, new_value=payload.model_dump(mode="json"))
    return holiday


@router.delete("/holidays/{holiday_id}", summary="Remove holiday")
def delete_holiday(holiday_id: int, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("schedules:write"))):
    holiday = db.get(models.Holiday, holiday_id)
    if not holiday:
        raise HTTPException(status_code=404, detail="Holiday not found")
    db.delete(holiday)
    db.commit()
    audit(db, action="HOLIDAY_DELETE", resource="holiday", resource_id=holiday_id, user=user, request=request)
    return {"success": True}
