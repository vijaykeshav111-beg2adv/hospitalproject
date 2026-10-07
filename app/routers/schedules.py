"""Doctor schedules (working days, hours, breaks, capacity) and slot generation."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, get_optional_user, require_permission
from ..services import slots as slot_service

router = APIRouter(tags=["Schedules & Slots"])


@router.get("/schedules", response_model=list[schemas.DoctorScheduleOut], summary="Weekly schedules")
def list_schedules(db: Session = Depends(get_db), doctor_id: int | None = None,
                   _: CurrentUser = Depends(get_current_user)):
    stmt = select(models.DoctorSchedule)
    if doctor_id:
        stmt = stmt.where(models.DoctorSchedule.doctor_id == doctor_id)
    return list(db.scalars(stmt.order_by(models.DoctorSchedule.doctor_id, models.DoctorSchedule.day_of_week)))


@router.post("/schedules", response_model=schemas.DoctorScheduleOut, status_code=201,
             summary="Create a recurring schedule block")
def create_schedule(doctor_id: int, payload: schemas.DoctorScheduleCreate, request: Request,
                    db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("schedules:write"))):
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")
    if payload.break_start and payload.break_end and payload.break_end <= payload.break_start:
        raise HTTPException(status_code=422, detail="break_end must be after break_start")
    duplicate = db.scalar(select(models.DoctorSchedule).where(
        models.DoctorSchedule.doctor_id == doctor_id,
        models.DoctorSchedule.day_of_week == payload.day_of_week,
        models.DoctorSchedule.start_time == payload.start_time,
    ))
    if duplicate:
        raise HTTPException(status_code=409,
                            detail=(f"A block already starts at {payload.start_time} on this day for the "
                                    f"doctor (schedule id {duplicate.id}). Edit or delete it instead."))
    schedule = models.DoctorSchedule(doctor_id=doctor_id, **payload.model_dump())
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    audit(db, action="SCHEDULE_CREATE", resource="doctor_schedule", resource_id=schedule.id, user=user,
          request=request, new_value=payload.model_dump(mode="json"))
    return schedule


@router.patch("/schedules/{schedule_id}", response_model=schemas.DoctorScheduleOut, summary="Update schedule")
def update_schedule(schedule_id: int, payload: schemas.DoctorScheduleCreate, request: Request,
                    db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("schedules:write"))):
    schedule = db.get(models.DoctorSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    for field, value in payload.model_dump().items():
        setattr(schedule, field, value)
    db.commit()
    db.refresh(schedule)
    audit(db, action="SCHEDULE_UPDATE", resource="doctor_schedule", resource_id=schedule_id, user=user,
          request=request, new_value=payload.model_dump(mode="json"))
    return schedule


@router.delete("/schedules/{schedule_id}", summary="Delete a schedule block")
def delete_schedule(schedule_id: int, request: Request, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("schedules:write"))):
    schedule = db.get(models.DoctorSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.delete(schedule)
    db.commit()
    audit(db, action="SCHEDULE_DELETE", resource="doctor_schedule", resource_id=schedule_id, user=user,
          request=request)
    return {"success": True}


@router.get("/schedules/working-days", summary="Human readable working days for a doctor")
def working_days(doctor_id: int, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    schedules = db.scalars(select(models.DoctorSchedule).where(
        models.DoctorSchedule.doctor_id == doctor_id, models.DoctorSchedule.is_active.is_(True))).all()
    return [{
        "day": slot_service.DAY_NAMES[s.day_of_week],
        "start": s.start_time.strftime("%H:%M"), "end": s.end_time.strftime("%H:%M"),
        "break": f"{s.break_start.strftime('%H:%M')}-{s.break_end.strftime('%H:%M')}"
        if s.break_start and s.break_end else None,
        "slot_duration_minutes": s.slot_duration_minutes, "capacity_per_slot": s.capacity_per_slot,
    } for s in sorted(schedules, key=lambda x: x.day_of_week)]


# ==========================================================================
# SLOT ENGINE
# ==========================================================================
slot_router = APIRouter(prefix="/slots", tags=["Slot Engine"])


@slot_router.get("", response_model=list[schemas.SlotOut], summary="List slots (filterable)")
def list_slots(db: Session = Depends(get_db), doctor_id: int | None = None, slot_date: date | None = None,
               status_filter: str | None = None, limit: int = 200,
               _: CurrentUser = Depends(require_permission("slots:read", "appointments:read", any_of=True))):
    stmt = select(models.Slot)
    if doctor_id:
        stmt = stmt.where(models.Slot.doctor_id == doctor_id)
    if slot_date:
        stmt = stmt.where(models.Slot.slot_date == slot_date)
    if status_filter:
        stmt = stmt.where(models.Slot.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(models.Slot.slot_date, models.Slot.start_time).limit(limit)).all()
    return [{
        "id": s.id, "doctor_id": s.doctor_id, "slot_date": s.slot_date, "start_time": s.start_time,
        "end_time": s.end_time, "capacity": s.capacity, "booked_count": s.booked_count, "status": s.status,
        "blocked_reason": s.blocked_reason, "hold_expires_at": s.hold_expires_at,
        "label": f"{s.slot_date.strftime('%a %d %b')} {slot_service.slot_label(s)}",
    } for s in rows]


@slot_router.get("/available", response_model=list[schemas.AvailableSlotOut], summary="Live slot search")
def available(db: Session = Depends(get_db), doctor_id: int | None = None, specialty_id: int | None = None,
              from_date: date | None = None, to_date: date | None = None, limit: int = 30,
              _: CurrentUser | None = Depends(get_optional_user)):
    rows = slot_service.available_slots(db, doctor_id=doctor_id, specialty_id=specialty_id,
                                        from_date=from_date, to_date=to_date, limit=limit)
    return rows


@slot_router.post("/generate", summary="Generate slots from schedules (scheduler engine)")
def generate(payload: schemas.SlotGenerateRequest, request: Request, db: Session = Depends(get_db),
             user: CurrentUser = Depends(require_permission("slots:manage"))):
    start = payload.start_date or (date.today() + timedelta(days=1))
    if payload.doctor_id:
        doctor = db.get(models.Doctor, payload.doctor_id)
        if not doctor:
            raise HTTPException(status_code=404, detail="Doctor not found")
        result = slot_service.generate_slots_for_doctor(db, doctor, start, payload.days_ahead, "MANUAL")
    else:
        result = slot_service.generate_all_slots(db, start, payload.days_ahead)
    audit(db, action="SLOT_GENERATE", resource="slot", user=user, request=request, new_value=result)
    return result


@slot_router.post("/hold", response_model=schemas.SlotHoldOut, summary="Hold a slot temporarily")
def hold(payload: schemas.SlotHoldRequest, request: Request, db: Session = Depends(get_db),
         user: CurrentUser = Depends(require_permission("appointments:write", "ai:chat", any_of=True))):
    slot = db.get(models.Slot, payload.slot_id)
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    patient_id = payload.patient_id
    if user.role == "PATIENT" and not patient_id:
        from ..deps import patient_for_user
        p = patient_for_user(db, user.id)
        patient_id = p.id if p else None
    try:
        slot = slot_service.hold_slot(db, slot, patient_id=patient_id,
                                      conversation_id=payload.conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"slot_id": slot.id, "hold_token": slot.hold_token, "expires_at": slot.hold_expires_at,
            "message": f"Slot held for {settings_hold_minutes()} minutes"}


def settings_hold_minutes() -> int:
    from ..config import settings

    return settings.slot_hold_minutes


@slot_router.post("/{slot_id}/release", summary="Release a held slot")
def release(slot_id: int, request: Request, db: Session = Depends(get_db),
            user: CurrentUser = Depends(require_permission("slots:manage", "appointments:write", any_of=True))):
    slot = db.get(models.Slot, slot_id)
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    slot_service.release_slot(db, slot, actor=user.email)
    audit(db, action="SLOT_RELEASE", resource="slot", resource_id=slot_id, user=user, request=request)
    return {"success": True, "slot_id": slot_id, "status": slot.status}


@slot_router.get("/{slot_id}", response_model=schemas.SlotOut, summary="Slot detail")
def slot_detail(slot_id: int, db: Session = Depends(get_db),
                _: CurrentUser = Depends(require_permission("slots:read", "appointments:read", any_of=True))):
    slot = db.get(models.Slot, slot_id)
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    return {
        "id": slot.id, "doctor_id": slot.doctor_id, "slot_date": slot.slot_date,
        "start_time": slot.start_time, "end_time": slot.end_time, "capacity": slot.capacity,
        "booked_count": slot.booked_count, "status": slot.status, "blocked_reason": slot.blocked_reason,
        "hold_expires_at": slot.hold_expires_at, "label": slot_service.slot_label(slot),
    }


@slot_router.get("/{slot_id}/history", summary="Hold / release / booking trail for a slot")
def slot_history(slot_id: int, db: Session = Depends(get_db),
                 _: CurrentUser = Depends(require_permission("slots:read"))):
    rows = db.scalars(select(models.SlotHoldHistory).where(models.SlotHoldHistory.slot_id == slot_id)
                      .order_by(models.SlotHoldHistory.created_at.desc())).all()
    return [{"action": r.action, "actor": r.actor, "at": r.created_at.isoformat()} for r in rows]


@slot_router.post("/expire-holds", summary="Force-expire stale holds (job trigger)")
def expire_holds(request: Request, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(require_permission("slots:manage"))):
    count = slot_service.expire_holds(db)
    audit(db, action="SLOT_EXPIRE_HOLDS", resource="slot", user=user, request=request, new_value={"count": count})
    return {"expired_holds": count}


@slot_router.get("/utilisation/report", summary="Slot utilisation by doctor")
def utilisation(db: Session = Depends(get_db), _: CurrentUser = Depends(require_permission("analytics:read"))):
    return slot_service.slot_utilisation(db)
