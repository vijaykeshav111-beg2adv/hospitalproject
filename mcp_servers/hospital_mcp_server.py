"""Read-only MCP tools for the Vijay Vargiya Hospital application.

Run from the project root with: python -m mcp_servers.hospital_mcp_server
The server deliberately exposes no patient records and no write operations.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastmcp import FastMCP
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Appointment, Doctor, Slot, Specialty

mcp = FastMCP("Vijay Vargiya Hospital Read-only MCP")


def _money(value):
    return float(value) if isinstance(value, Decimal) else value


@mcp.tool()
def search_doctors(name: str = "", specialty: str = "", limit: int = 10) -> dict:
    """Search active hospital doctors by partial name and/or specialty. Returns directory information only."""
    limit = max(1, min(int(limit), 25))
    with SessionLocal() as db:
        stmt = select(Doctor, Specialty).join(Specialty, Doctor.specialty_id == Specialty.id).where(
            Doctor.is_active.is_(True), Specialty.is_active.is_(True)
        )
        if name.strip():
            stmt = stmt.where(Doctor.full_name.ilike(f"%{name.strip()}%"))
        if specialty.strip():
            stmt = stmt.where(Specialty.name.ilike(f"%{specialty.strip()}%"))
        rows = db.execute(stmt.order_by(Doctor.full_name).limit(limit)).all()
        return {"count": len(rows), "doctors": [{
            "doctor_id": d.id,
            "name": d.full_name,
            "specialty": s.name,
            "qualifications": d.qualifications,
            "experience_years": d.experience_years,
            "languages": d.languages,
            "consultation_fee": _money(d.consultation_fee),
            "slot_duration_minutes": d.slot_duration_minutes,
            "rating_average": _money(d.rating_avg),
            "rating_count": d.rating_count,
        } for d, s in rows]}


@mcp.tool()
def list_specialties() -> dict:
    """List active hospital specialties and their displayed consultation fees."""
    with SessionLocal() as db:
        rows = db.scalars(select(Specialty).where(Specialty.is_active.is_(True)).order_by(Specialty.name)).all()
        return {"count": len(rows), "specialties": [{
            "id": s.id, "name": s.name, "description": s.description,
            "consultation_fee": _money(s.consultation_fee)
        } for s in rows]}


@mcp.tool()
def find_available_slots(appointment_date: str, doctor_id: int | None = None, specialty: str = "", limit: int = 20) -> dict:
    """Find unbooked slots on YYYY-MM-DD. This is informational only and does not reserve a slot."""
    try:
        requested_date = date.fromisoformat(appointment_date)
    except ValueError:
        return {"success": False, "error": "appointment_date must use YYYY-MM-DD format"}
    limit = max(1, min(int(limit), 50))
    with SessionLocal() as db:
        stmt = select(Slot, Doctor, Specialty).join(Doctor, Slot.doctor_id == Doctor.id).join(
            Specialty, Doctor.specialty_id == Specialty.id
        ).where(
            Slot.slot_date == requested_date,
            Slot.status == "AVAILABLE",
            Slot.booked_count < Slot.capacity,
            Doctor.is_active.is_(True),
            Specialty.is_active.is_(True),
        )
        if doctor_id is not None:
            stmt = stmt.where(Doctor.id == doctor_id)
        if specialty.strip():
            stmt = stmt.where(Specialty.name.ilike(f"%{specialty.strip()}%"))
        rows = db.execute(stmt.order_by(Slot.start_time).limit(limit)).all()
        return {"date": requested_date.isoformat(), "count": len(rows), "slots": [{
            "doctor_id": d.id, "doctor_name": d.full_name, "specialty": s.name,
            "start_time": slot.start_time.strftime("%H:%M"),
            "end_time": slot.end_time.strftime("%H:%M"),
            "consultation_fee": _money(d.consultation_fee),
            "note": "Availability can change; this tool does not hold or book the slot."
        } for slot, d, s in rows]}


@mcp.tool()
def get_public_hospital_stats() -> dict:
    """Return aggregate operational counts only, never patient names or medical records."""
    with SessionLocal() as db:
        doctors = db.scalar(select(func.count()).select_from(Doctor).where(Doctor.is_active.is_(True))) or 0
        specialties = db.scalar(select(func.count()).select_from(Specialty).where(Specialty.is_active.is_(True))) or 0
        today = date.today()
        available_slots = db.scalar(select(func.count()).select_from(Slot).where(
            Slot.slot_date == today, Slot.status == "AVAILABLE", Slot.booked_count < Slot.capacity
        )) or 0
        appointments_today = db.scalar(select(func.count()).select_from(Appointment).where(
            Appointment.appointment_date == today,
            Appointment.status.in_(["PENDING", "CONFIRMED"])
        )) or 0
        return {"date": today.isoformat(), "active_doctors": doctors,
                "active_specialties": specialties, "available_slots_today": available_slots,
                "pending_or_confirmed_appointments_today": appointments_today}


if __name__ == "__main__":
    # Use a separate port from the existing medical_mcp prototype (8001).
    mcp.run(transport="http", host="127.0.0.1", port=8010)
