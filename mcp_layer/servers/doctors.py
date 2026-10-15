"""MCP server 02 - Doctor & Specialty Server  [USER tier].

Doctor profiles, specialty master, weekly schedules, leaves and a
grounded availability window per doctor/date.
"""
from __future__ import annotations

import difflib
from datetime import date as date_cls, datetime, timedelta

from sqlalchemy import func, or_, select

from app import models

from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["doctors"]
TOOLS: dict = {}


def _doctor_brief(d: models.Doctor) -> dict:
    return {
        "id": d.id,
        "doctor_code": d.doctor_code,
        "full_name": d.full_name,
        "specialty": d.specialty.name if d.specialty else None,
        "qualifications": d.qualifications,
        "experience_years": d.experience_years,
        "languages": d.languages,
        "consultation_fee": float(d.consultation_fee or 0),
        "slot_duration_minutes": d.slot_duration_minutes,
        "room_number": d.room_number,
        "rating_avg": float(d.rating_avg or 0),
        "rating_count": int(d.rating_count or 0),
        "is_active": bool(d.is_active),
    }


@tool("get_doctor", "Look up a doctor by id or exact name.")
def _get_doctor(db, doctor_id: int | None = None, name: str = ""):
    stmt = select(models.Doctor)
    if doctor_id:
        stmt = stmt.where(models.Doctor.id == doctor_id)
    elif name.strip():
        stmt = stmt.where(func.lower(models.Doctor.full_name) == name.strip().lower())
    else:
        return grounded_no(server=SPEC.key, tool="get_doctor", what="doctor",
                           note="Provide doctor_id or name.")
    doctor = db.scalar(stmt)
    if doctor is None:
        return grounded_no(server=SPEC.key, tool="get_doctor", what="doctor")
    return _doctor_brief(doctor)


def _closest_specialty_name(db, text: str) -> str:
    """Fix small typos: 'orthopedics' -> 'Orthopaedics', 'dermatolgy' -> 'Dermatology'."""
    names = [n for n in db.scalars(select(models.Specialty.name).where(models.Specialty.is_active.is_(True)))]
    lowered = {n.lower(): n for n in names}
    close = difflib.get_close_matches(text.strip().lower(), list(lowered), n=1, cutoff=0.7)
    return lowered[close[0]] if close else ""


def _find_doctors(db, q: str, specialty_id, limit: int):
    stmt = select(models.Doctor).where(models.Doctor.is_active.is_(True))
    if q:
        # If the text is exactly a department name, filter by that department only.
        exact = db.scalar(select(models.Specialty).where(func.lower(models.Specialty.name) == q.lower()))
        if exact is not None:
            stmt = stmt.where(models.Doctor.specialty_id == exact.id)
        else:
            like = f"%{q}%"
            stmt = (stmt.outerjoin(models.Specialty, models.Doctor.specialty_id == models.Specialty.id)
                    .where(or_(models.Doctor.full_name.ilike(like),
                               models.Doctor.qualifications.ilike(like),
                               models.Specialty.name.ilike(like))))
    if specialty_id:
        stmt = stmt.where(models.Doctor.specialty_id == specialty_id)
    return db.scalars(stmt.order_by(models.Doctor.rating_avg.desc()).limit(limit)).all()


@tool("search_doctors", "Search doctors by name / specialty keyword (typos allowed).")
def _search_doctors(db, query: str = "", specialty_id: int | None = None, limit: int = 10):
    limit = max(1, min(int(limit), 50))
    q = (query or "").strip()
    rows = _find_doctors(db, q, specialty_id, limit)

    # Nothing found: maybe the person misspelt a department name.
    if not rows and q:
        fixed = _closest_specialty_name(db, q)
        if fixed:
            rows = _find_doctors(db, fixed, specialty_id, limit)

    if not rows:
        return grounded_no(server=SPEC.key, tool="search_doctors", what="doctor")
    return [_doctor_brief(d) for d in rows]


@tool("list_doctors", "List all active doctors (paged).")
def _list_doctors(db, offset: int = 0, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    total = int(db.scalar(select(func.count(models.Doctor.id)).where(models.Doctor.is_active.is_(True))) or 0)
    rows = db.scalars(
        select(models.Doctor).where(models.Doctor.is_active.is_(True))
        .order_by(models.Doctor.full_name).offset(max(0, offset)).limit(limit)
    ).all()
    return {"total": total, "offset": max(0, offset), "limit": limit,
            "doctors": [_doctor_brief(d) for d in rows]}


@tool("list_specialties", "List the hospital specialty master (with consultation fees).")
def _list_specialties(db, query: str = ""):
    stmt = select(models.Specialty).where(models.Specialty.is_active.is_(True))
    q = (query or "").strip()
    if q:
        stmt = stmt.where(models.Specialty.name.ilike(f"%{q}%"))
    rows = db.scalars(stmt.order_by(models.Specialty.name)).all()
    if not rows and q:
        fixed = _closest_specialty_name(db, q)
        if fixed:
            rows = db.scalars(select(models.Specialty).where(models.Specialty.name == fixed)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_specialties", what="specialty")
    return [{"id": s.id, "code": s.code, "name": s.name,
             "consultation_fee": float(s.consultation_fee or 0),
             "description": s.description} for s in rows]


@tool("doctor_schedule", "Weekly working hours + breaks for a doctor.")
def _doctor_schedule(db, doctor_id: int):
    doctor = db.get(models.Doctor, doctor_id)
    if doctor is None:
        return grounded_no(server=SPEC.key, tool="doctor_schedule", what="doctor")
    rows = db.scalars(
        select(models.DoctorSchedule)
        .where(models.DoctorSchedule.doctor_id == doctor_id, models.DoctorSchedule.is_active.is_(True))
        .order_by(models.DoctorSchedule.day_of_week, models.DoctorSchedule.start_time)
    ).all()
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {
        "doctor": _doctor_brief(doctor),
        "schedule": [{
            "day": days[s.day_of_week % 7],
            "start_time": str(s.start_time),
            "end_time": str(s.end_time),
            "break_start": str(s.break_start) if s.break_start else None,
            "break_end": str(s.break_end) if s.break_end else None,
            "slot_minutes": s.slot_duration_minutes,
        } for s in rows],
    }


@tool("doctor_availability", "Grounded availability summary for a doctor on a date (schedule - leaves - holidays - booked slots).")
def _doctor_availability(db, doctor_id: int, on_date: str = ""):
    doctor = db.get(models.Doctor, doctor_id)
    if doctor is None:
        return grounded_no(server=SPEC.key, tool="doctor_availability", what="doctor")
    day = _parse_date(on_date) or datetime.utcnow().date()

    on_leave = db.scalar(
        select(func.count(models.DoctorLeave.id)).where(
            models.DoctorLeave.doctor_id == doctor_id,
            models.DoctorLeave.status == "APPROVED",
            models.DoctorLeave.start_date <= day,
            models.DoctorLeave.end_date >= day,
        )
    ) or 0
    holiday = db.scalar(
        select(models.Holiday).where(models.Holiday.holiday_date == day)
    )
    slots_total = int(db.scalar(
        select(func.count(models.Slot.id)).where(models.Slot.doctor_id == doctor_id,
                                                 models.Slot.slot_date == day)
    ) or 0)
    slots_free = int(db.scalar(
        select(func.count(models.Slot.id)).where(
            models.Slot.doctor_id == doctor_id, models.Slot.slot_date == day,
            models.Slot.status == "AVAILABLE", models.Slot.booked_count < models.Slot.capacity)
    ) or 0)

    available = (on_leave == 0) and (holiday is None) and (slots_free > 0)
    result = {
        "doctor": _doctor_brief(doctor),
        "date": str(day),
        "on_leave": bool(on_leave),
        "holiday": holiday.name if holiday else None,
        "slots_total": slots_total,
        "slots_free": slots_free,
        "available": available,
    }
    return result


def _parse_date(value: str):
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


mcp = make_server(
    SPEC,
    "Doctor & Specialty Server. Every answer is read from the hospital "
    "database; unknown doctors return a grounded 'no', never a guess.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
