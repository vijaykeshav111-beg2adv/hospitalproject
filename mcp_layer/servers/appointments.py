"""MCP server 03 - Appointment & Slot Server  [USER tier].

Slot search, booking/cancel/reschedule (write tools) and the reception
queue board.  Write tools are RBAC-checked at USER tier but still audit-
logged; destructive changes require the actor to own the appointment
(patient) or be staff.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import func, select

from app import models
from app.security import utcnow

from ..guard import grounded_no
from ..rbac import current_actor
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["appointments"]
TOOLS: dict = {}

STAFF_ROLES = {"SUPER_ADMIN", "ADMIN", "DOCTOR", "RECEPTIONIST", "ACCOUNTANT"}


def _slot_brief(s: models.Slot, doctor_name: str | None = None) -> dict:
    return {
        "id": s.id,
        "doctor_id": s.doctor_id,
        "doctor": doctor_name,
        "date": str(s.slot_date),
        "start_time": str(s.start_time),
        "end_time": str(s.end_time),
        "status": s.status,
        "capacity": s.capacity,
        "booked_count": s.booked_count,
    }


def _appt_brief(a: models.Appointment) -> dict:
    return {
        "id": a.id,
        "appointment_code": a.appointment_code,
        "patient": a.patient.full_name if a.patient else None,
        "patient_id": a.patient_id,
        "doctor": a.doctor.full_name if a.doctor else None,
        "doctor_id": a.doctor_id,
        "specialty": a.specialty.name if a.specialty else None,
        "date": str(a.appointment_date),
        "start_time": str(a.start_time),
        "token_number": a.token_number,
        "status": a.status,
        "queue_status": a.queue_status,
        "source": a.source,
        "reason": a.reason,
    }


def _owns(db, appointment: models.Appointment) -> bool:
    actor = current_actor()
    if actor.role in STAFF_ROLES:
        return True
    patient = db.scalar(select(models.Patient).where(models.Patient.user_id == actor.user_id))
    return bool(patient and patient.id == appointment.patient_id)


@tool("get_available_slots", "Free slots for a doctor (optionally on a date).")
def _get_available_slots(db, doctor_id: int | None = None, on_date: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.Slot).where(
        models.Slot.status == "AVAILABLE",
        models.Slot.booked_count < models.Slot.capacity,
    )
    if doctor_id:
        stmt = stmt.where(models.Slot.doctor_id == doctor_id)
    day = (on_date or "").strip()[:10]
    if day:
        stmt = stmt.where(models.Slot.slot_date == day)
    else:
        stmt = stmt.where(models.Slot.slot_date >= utcnow().date())
    rows = db.scalars(stmt.order_by(models.Slot.slot_date, models.Slot.start_time).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="get_available_slots", what="slot")
    names = {d.id: d.full_name for d in db.scalars(
        select(models.Doctor).where(models.Doctor.id.in_({s.doctor_id for s in rows}))).all()}
    return [_slot_brief(s, names.get(s.doctor_id)) for s in rows]


@tool("get_appointment", "Look up one appointment by id or appointment code.")
def _get_appointment(db, appointment_id: int | None = None, appointment_code: str = ""):
    stmt = select(models.Appointment)
    if appointment_id:
        stmt = stmt.where(models.Appointment.id == appointment_id)
    elif appointment_code.strip():
        stmt = stmt.where(models.Appointment.appointment_code == appointment_code.strip().upper())
    else:
        return grounded_no(server=SPEC.key, tool="get_appointment", what="appointment",
                           note="Provide appointment_id or appointment_code.")
    appt = db.scalar(stmt)
    if appt is None or not _owns(db, appt):
        return grounded_no(server=SPEC.key, tool="get_appointment", what="appointment")
    return _appt_brief(appt)


@tool("list_appointments", "List appointments filtered by doctor / patient / date / status.")
def _list_appointments(db, doctor_id: int | None = None, patient_id: int | None = None,
                       on_date: str = "", status: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    actor = current_actor()
    stmt = select(models.Appointment)
    if actor.role == "PATIENT":
        patient = db.scalar(select(models.Patient).where(models.Patient.user_id == actor.user_id))
        stmt = stmt.where(models.Appointment.patient_id == patient.id if patient else False)
    elif patient_id:
        stmt = stmt.where(models.Appointment.patient_id == patient_id)
    if doctor_id:
        stmt = stmt.where(models.Appointment.doctor_id == doctor_id)
    if (on_date or "").strip():
        stmt = stmt.where(models.Appointment.appointment_date == on_date.strip()[:10])
    if (status or "").strip():
        stmt = stmt.where(models.Appointment.status == status.strip().upper())
    rows = db.scalars(
        stmt.order_by(models.Appointment.appointment_date.desc(), models.Appointment.start_time)
        .limit(limit)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_appointments", what="appointment")
    return [_appt_brief(a) for a in rows]


@tool("book_appointment", "Book an appointment for a patient in an available slot.", write=True)
def _book_appointment(db, patient_id: int, slot_id: int, reason: str = ""):
    slot = db.get(models.Slot, slot_id)
    patient = db.get(models.Patient, patient_id)
    if slot is None or patient is None:
        return grounded_no(server=SPEC.key, tool="book_appointment", what="slot/patient")
    if slot.status != "AVAILABLE" or slot.booked_count >= slot.capacity:
        return grounded_no(server=SPEC.key, tool="book_appointment", what="free slot",
                           note="Slot is not available any more.")

    from app.security import utcnow as _utcnow

    prefix = _utcnow().strftime("%Y%m%d")
    code = f"APT{prefix}{secrets.token_hex(3).upper()}"
    doctor = db.get(models.Doctor, slot.doctor_id)
    appointment = models.Appointment(
        appointment_code=code,
        patient_id=patient.id,
        doctor_id=slot.doctor_id,
        specialty_id=doctor.specialty_id if doctor else None,
        slot_id=slot.id,
        appointment_date=slot.slot_date,
        start_time=slot.start_time,
        end_time=slot.end_time,
        token_number=int(db.scalar(
            select(func.count(models.Appointment.id)).where(
                models.Appointment.doctor_id == slot.doctor_id,
                models.Appointment.appointment_date == slot.slot_date)) or 0) + 1,
        status="CONFIRMED",
        queue_status="WAITING",
        source="MCP",
        reason=reason or None,
        booked_by=current_actor().user_id,
    )
    slot.booked_count += 1
    if slot.booked_count >= slot.capacity:
        slot.status = "BOOKED"
    db.add(appointment)
    db.flush()
    return {"booked": True, "appointment": _appt_brief(appointment)}


@tool("cancel_appointment", "Cancel an appointment (owner or staff).", write=True)
def _cancel_appointment(db, appointment_id: int, reason: str = ""):
    appt = db.get(models.Appointment, appointment_id)
    if appt is None or not _owns(db, appt):
        return grounded_no(server=SPEC.key, tool="cancel_appointment", what="appointment")
    if appt.status in ("CANCELLED", "COMPLETED"):
        return {"ok": True, "grounded": True, "generated": True, "answer": "no",
                "note": f"Appointment already {appt.status.lower()}.", "data": None}
    appt.status = "CANCELLED"
    appt.cancellation_reason = reason or "Cancelled via MCP"
    appt.cancelled_by = current_actor().user_id
    if appt.slot_id:
        slot = db.get(models.Slot, appt.slot_id)
        if slot and slot.booked_count > 0:
            slot.booked_count -= 1
            if slot.status == "BOOKED":
                slot.status = "AVAILABLE"
    return {"cancelled": True, "appointment": _appt_brief(appt)}


@tool("reschedule_appointment", "Move an appointment to another available slot.", write=True)
def _reschedule_appointment(db, appointment_id: int, new_slot_id: int):
    appt = db.get(models.Appointment, appointment_id)
    new_slot = db.get(models.Slot, new_slot_id)
    if appt is None or new_slot is None or not _owns(db, appt):
        return grounded_no(server=SPEC.key, tool="reschedule_appointment", what="appointment/slot")
    if new_slot.status != "AVAILABLE" or new_slot.booked_count >= new_slot.capacity:
        return grounded_no(server=SPEC.key, tool="reschedule_appointment", what="free slot",
                           note="Target slot is not available.")
    old_slot = db.get(models.Slot, appt.slot_id) if appt.slot_id else None
    if old_slot and old_slot.booked_count > 0:
        old_slot.booked_count -= 1
        if old_slot.status == "BOOKED":
            old_slot.status = "AVAILABLE"
    appt.slot_id = new_slot.id
    appt.doctor_id = new_slot.doctor_id
    appt.appointment_date = new_slot.slot_date
    appt.start_time = new_slot.start_time
    appt.end_time = new_slot.end_time
    appt.status = "CONFIRMED"
    new_slot.booked_count += 1
    if new_slot.booked_count >= new_slot.capacity:
        new_slot.status = "BOOKED"
    return {"rescheduled": True, "appointment": _appt_brief(appt)}


@tool("queue_board", "Live reception queue for today (or a given date).")
def _queue_board(db, on_date: str = ""):
    day = (on_date or "").strip()[:10] or str(utcnow().date())
    rows = db.scalars(
        select(models.Appointment).where(models.Appointment.appointment_date == day)
        .order_by(models.Appointment.token_number)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="queue_board", what="queue entry")
    return {"date": day, "queue": [_appt_brief(a) for a in rows],
            "waiting": sum(1 for a in rows if a.queue_status == "WAITING"),
            "in_consultation": sum(1 for a in rows if a.queue_status == "IN_CONSULTATION")}


mcp = make_server(
    SPEC,
    "Appointment & Slot Server. Booking writes go through the same MySQL "
    "tables the web portal uses; everything is audit-logged.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
