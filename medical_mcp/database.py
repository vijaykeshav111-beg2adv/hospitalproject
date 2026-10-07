"""MySQL-backed compatibility database for the original medical MCP prototype.

The original workshop used a small SQLite demo database. In the merged hospital
project these same prototype tools read the real Vijay Vargiya hospital MySQL
database, so there is only one source of patient/doctor data.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Doctor, Patient, Specialty


def init_db() -> None:
    """Compatibility no-op. The hospital application owns schema creation."""
    return None


def get_doctor(name: str):
    with SessionLocal() as db:
        doctor = db.scalar(
            select(Doctor).where(func.lower(Doctor.full_name) == name.strip().lower())
        )
        if not doctor:
            return None
        specialty = db.get(Specialty, doctor.specialty_id)
        return (
            doctor.id,
            doctor.full_name,
            specialty.name if specialty else "",
            doctor.slot_duration_minutes,
        )


def get_patient(patient_id: int):
    with SessionLocal() as db:
        patient = db.get(Patient, patient_id)
        if not patient:
            return None
        return (
            patient.id,
            patient.full_name,
            patient.age,
            patient.city,
            patient.blood_group,
        )
