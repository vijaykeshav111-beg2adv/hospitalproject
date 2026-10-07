"""Slot engine: generation, holding, booking, release, expiry and leave blocking."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..security import utcnow

log = logging.getLogger("vvh.slots")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _combine(day: date, value: time) -> datetime:
    return datetime.combine(day, value)


def is_holiday(db: Session, day: date) -> models.Holiday | None:
    return db.scalar(select(models.Holiday).where(models.Holiday.holiday_date == day))


def leave_for(db: Session, doctor_id: int, day: date) -> list[models.DoctorLeave]:
    return list(db.scalars(
        select(models.DoctorLeave).where(
            models.DoctorLeave.doctor_id == doctor_id,
            models.DoctorLeave.status == "APPROVED",
            models.DoctorLeave.start_date <= day,
            models.DoctorLeave.end_date >= day,
        )
    ))


def leave_blocks_range(leave: models.DoctorLeave, day: date, start: time, end: time) -> bool:
    if leave.leave_type in {"FULL_DAY", "HOLIDAY", "EMERGENCY"}:
        return True
    ls, le = leave.start_time, leave.end_time
    if not ls or not le:
        return True
    return start < le and end > ls  # overlap


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------
def generate_slots_for_doctor(
    db: Session,
    doctor: models.Doctor,
    start_date: date,
    days_ahead: int = 7,
    generated_by: str = "SCHEDULER",
) -> dict:
    schedules = list(db.scalars(
        select(models.DoctorSchedule).where(
            models.DoctorSchedule.doctor_id == doctor.id,
            models.DoctorSchedule.is_active.is_(True),
        )
    ))
    by_day: dict[int, list[models.DoctorSchedule]] = {}
    for s in schedules:
        by_day.setdefault(s.day_of_week, []).append(s)

    created = skipped_holiday = skipped_leave = existing = 0
    for offset in range(days_ahead):
        day = start_date + timedelta(days=offset)
        if is_holiday(db, day):
            skipped_holiday += 1
            continue
        day_schedules = by_day.get(day.weekday(), [])
        if not day_schedules:
            continue
        leaves = leave_for(db, doctor.id, day)

        for sch in day_schedules:
            duration = sch.slot_duration_minutes or doctor.slot_duration_minutes or 30
            capacity = max(1, sch.capacity_per_slot or 1)
            cursor = _combine(day, sch.start_time)
            end_of_day = _combine(day, sch.end_time)
            produced_for_day = 0
            while cursor + timedelta(minutes=duration) <= end_of_day:
                slot_start = cursor.time()
                slot_end = (cursor + timedelta(minutes=duration)).time()
                cursor += timedelta(minutes=duration)

                if produced_for_day >= (doctor.daily_capacity or 999):
                    break

                if sch.break_start and sch.break_end:
                    slot_s = _combine(day, slot_start)
                    slot_e = _combine(day, slot_end)
                    if slot_s < _combine(day, sch.break_end) and slot_e > _combine(day, sch.break_start):
                        continue

                if any(leave_blocks_range(lv, day, slot_start, slot_end) for lv in leaves):
                    skipped_leave += 1
                    continue

                exists = db.scalar(
                    select(models.Slot).where(
                        models.Slot.doctor_id == doctor.id,
                        models.Slot.slot_date == day,
                        models.Slot.start_time == slot_start,
                    )
                )
                if exists:
                    existing += 1
                    continue

                db.add(models.Slot(
                    doctor_id=doctor.id,
                    slot_date=day,
                    start_time=slot_start,
                    end_time=slot_end,
                    capacity=capacity,
                    status="AVAILABLE",
                    generated_by=generated_by,
                ))
                produced_for_day += 1
                created += 1
    db.commit()
    return {
        "doctor_id": doctor.id,
        "created": created,
        "existing": existing,
        "skipped_leave": skipped_leave,
        "skipped_holiday_days": skipped_holiday,
    }


def generate_all_slots(db: Session, start_date: date | None = None, days_ahead: int = 7) -> dict:
    start_date = start_date or (date.today() + timedelta(days=1))
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.is_active.is_(True))).all()
    results = [generate_slots_for_doctor(db, d, start_date, days_ahead) for d in doctors]
    return {
        "start_date": start_date.isoformat(),
        "days_ahead": days_ahead,
        "doctors": len(results),
        "created": sum(r["created"] for r in results),
        "details": results,
    }


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
def slot_label(slot: models.Slot) -> str:
    return f"{slot.start_time.strftime('%H:%M')} - {slot.end_time.strftime('%H:%M')}"


def available_slots(
    db: Session,
    doctor_id: int | None = None,
    specialty_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 40,
    exclude_held_for_others: bool = True,
) -> list[dict]:
    from_date = from_date or date.today()
    to_date = to_date or (from_date + timedelta(days=14))

    now = utcnow()
    stmt = (
        select(models.Slot, models.Doctor, models.Specialty)
        .join(models.Doctor, models.Doctor.id == models.Slot.doctor_id)
        .join(models.Specialty, models.Specialty.id == models.Doctor.specialty_id)
        .where(
            models.Slot.slot_date >= from_date,
            models.Slot.slot_date <= to_date,
            models.Slot.status.in_(["AVAILABLE", "HELD"]),
            models.Slot.booked_count < models.Slot.capacity,
            models.Doctor.is_active.is_(True),
            # future days: any open slot; today: only slots that have not started yet
            or_(
                models.Slot.slot_date > from_date,
                and_(models.Slot.slot_date == from_date, models.Slot.start_time > now.time()),
            ),
        )
        .order_by(models.Slot.slot_date, models.Slot.start_time)
        .limit(limit * 3)
    )
    if doctor_id:
        stmt = stmt.where(models.Slot.doctor_id == doctor_id)
    if specialty_id:
        stmt = stmt.where(models.Doctor.specialty_id == specialty_id)

    out: list[dict] = []
    for slot, doctor, specialty in db.execute(stmt):
        if slot.status == "HELD":
            if not exclude_held_for_others:
                pass
            elif slot.hold_expires_at and slot.hold_expires_at > now:
                continue
            else:  # stale hold -> release on the fly
                slot.status = "AVAILABLE"
                slot.hold_token = None
                slot.hold_expires_at = None
                db.commit()
        out.append({
            "slot_id": slot.id,
            "doctor_id": doctor.id,
            "doctor_name": doctor.full_name,
            "specialty_name": specialty.name,
            "label": f"{slot.slot_date.strftime('%a %d %b')} at {slot_label(slot)}",
            "slot_date": slot.slot_date,
            "start_time": slot.start_time,
            "end_time": slot.end_time,
            "fee": float(doctor.consultation_fee or 0),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# hold / release / book
# --------------------------------------------------------------------------
def hold_slot(
    db: Session,
    slot: models.Slot,
    patient_id: int | None = None,
    conversation_id: int | None = None,
    minutes: int | None = None,
) -> models.Slot:
    minutes = minutes or settings.slot_hold_minutes
    now = utcnow()
    if slot.status == "BOOKED" or slot.booked_count >= slot.capacity:
        raise ValueError("Slot is already booked")
    if slot.status == "HELD" and slot.hold_expires_at and slot.hold_expires_at > now:
        if slot.hold_patient_id != patient_id:
            raise ValueError("Slot is temporarily held by another booking flow")
    token = f"hold_{slot.id}_{int(now.timestamp())}"
    slot.status = "HELD"
    slot.hold_token = token
    slot.hold_patient_id = patient_id
    slot.hold_expires_at = now + timedelta(minutes=minutes)
    db.add(models.SlotHoldHistory(
        slot_id=slot.id, action="HOLD", actor=f"patient:{patient_id}" if patient_id else "anonymous",
        conversation_id=conversation_id, hold_token=token,
    ))
    db.commit()
    db.refresh(slot)
    return slot


def release_slot(db: Session, slot: models.Slot, actor: str = "system", conversation_id: int | None = None) -> models.Slot:
    if slot.booked_count >= slot.capacity:
        return slot
    slot.status = "AVAILABLE"
    slot.hold_token = None
    slot.hold_patient_id = None
    slot.hold_expires_at = None
    db.add(models.SlotHoldHistory(slot_id=slot.id, action="RELEASE", actor=actor, conversation_id=conversation_id))
    db.commit()
    db.refresh(slot)
    return slot


def mark_slot_booked(db: Session, slot: models.Slot, conversation_id: int | None = None) -> models.Slot:
    slot.booked_count = (slot.booked_count or 0) + 1
    slot.hold_token = None
    slot.hold_expires_at = None
    slot.status = "BOOKED" if slot.booked_count >= slot.capacity else "AVAILABLE"
    db.add(models.SlotHoldHistory(slot_id=slot.id, action="BOOK", actor="system", conversation_id=conversation_id))
    db.commit()
    return slot


def free_slot(db: Session, slot: models.Slot) -> models.Slot:
    slot.booked_count = max(0, (slot.booked_count or 0) - 1)
    if slot.booked_count < slot.capacity:
        slot.status = "AVAILABLE"
    db.commit()
    return slot


def expire_holds(db: Session) -> int:
    now = utcnow()
    slots = list(db.scalars(
        select(models.Slot).where(
            models.Slot.status == "HELD",
            models.Slot.hold_expires_at.is_not(None),
            models.Slot.hold_expires_at < now,
        )
    ))
    for slot in slots:
        slot.status = "AVAILABLE"
        slot.hold_token = None
        slot.hold_patient_id = None
        slot.hold_expires_at = None
        db.add(models.SlotHoldHistory(slot_id=slot.id, action="EXPIRE", actor="scheduler"))
    db.commit()
    return len(slots)


def expire_past_slots(db: Session) -> int:
    today = date.today()
    slots = list(db.scalars(
        select(models.Slot).where(
            models.Slot.status.in_(["AVAILABLE", "HELD"]),
            or_(
                models.Slot.slot_date < today,
                and_(models.Slot.slot_date == today, models.Slot.end_time < utcnow().time()),
            ),
        )
    ))
    for slot in slots:
        slot.status = "EXPIRED"
        slot.hold_token = None
        slot.hold_expires_at = None
    db.commit()
    return len(slots)


# --------------------------------------------------------------------------
# leave blocking
# --------------------------------------------------------------------------
def block_slots_for_leave(db: Session, leave: models.DoctorLeave) -> int:
    """Block generated slots that fall inside an approved leave."""
    end = leave.end_date or leave.start_date
    slots = list(db.scalars(
        select(models.Slot).where(
            models.Slot.doctor_id == leave.doctor_id,
            models.Slot.slot_date >= leave.start_date,
            models.Slot.slot_date <= end,
            models.Slot.status.in_(["AVAILABLE", "HELD"]),
        )
    ))
    blocked = 0
    for slot in slots:
        if leave_blocks_range(leave, slot.slot_date, slot.start_time, slot.end_time):
            slot.status = "BLOCKED"
            slot.blocked_reason = f"{leave.leave_type} leave"
            db.add(models.SlotHoldHistory(slot_id=slot.id, action="BLOCK", actor="leave"))
            blocked += 1
    leave.slots_blocked = blocked
    db.commit()
    return blocked


def slot_utilisation(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            models.Doctor.id, models.Doctor.full_name,
        ).where(models.Doctor.is_active.is_(True))
    ).all()
    out = []
    for doctor_id, name in rows:
        total = db.scalar(
            select(func.count(models.Slot.id)).where(models.Slot.doctor_id == doctor_id)
        ) or 0
        booked = db.scalar(
            select(func.count(models.Slot.id)).where(
                models.Slot.doctor_id == doctor_id, models.Slot.status == "BOOKED"
            )
        ) or 0
        out.append({
            "doctor_id": doctor_id,
            "doctor_name": name,
            "total_slots": total,
            "booked_slots": booked,
            "utilisation_percent": round((booked / total) * 100, 1) if total else 0,
        })
    return sorted(out, key=lambda r: -r["utilisation_percent"])
