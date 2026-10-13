"""MCP server 01 - Patient Server  [USER tier].

Read-only patient domain: profiles, search, timeline and summary.
Row-level scoping: a PATIENT principal only ever sees their own rows.
"""
from __future__ import annotations

from sqlalchemy import func, or_, select

from app import models

from ..guard import grounded_no
from ..rbac import current_actor
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["patients"]
TOOLS: dict = {}


def _scope_patient_id(db, requested: int | None) -> int | None:
    """PATIENT callers are locked to their own patient row.

    * no id given      -> their own patient id
    * their own id     -> allowed
    * someone else's   -> -1 (a guaranteed, grounded "no")
    """
    actor = current_actor()
    if actor.role == "PATIENT":
        row = db.scalar(select(models.Patient).where(models.Patient.user_id == actor.user_id))
        own = row.id if row else -1
        if requested is None:
            return own
        return requested if requested == own else -1
    return requested


def _patient_brief(p: models.Patient) -> dict:
    return {
        "id": p.id,
        "patient_code": p.patient_code,
        "full_name": p.full_name,
        "age": p.age,
        "gender": p.gender,
        "blood_group": p.blood_group,
        "phone": p.phone,
        "email": p.email,
        "city": p.city,
        "allergies": p.allergies,
        "chronic_conditions": p.chronic_conditions,
        "is_active": bool(p.is_active),
    }


@tool("get_patient", "Look up one patient by id (or the caller's own record when id is omitted).")
def _get_patient(db, patient_id: int | None = None):
    pid = _scope_patient_id(db, patient_id)
    patient = db.get(models.Patient, pid) if pid else None
    if patient is None:
        return grounded_no(server=SPEC.key, tool="get_patient", what="patient")
    return _patient_brief(patient)


@tool("search_patients", "Search patients by name / phone / patient code.")
def _search_patients(db, query: str = "", limit: int = 10):
    limit = max(1, min(int(limit), 50))
    q = (query or "").strip()
    actor = current_actor()
    if actor.role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="search_patients", what="patient",
                           note="Patients may only read their own record.")
    stmt = select(models.Patient)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            models.Patient.full_name.ilike(like),
            models.Patient.phone.ilike(like),
            models.Patient.patient_code.ilike(like),
        ))
    rows = db.scalars(stmt.order_by(models.Patient.full_name).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="search_patients", what="patient")
    return [_patient_brief(p) for p in rows]


@tool("list_patients", "List registered patients (paged).")
def _list_patients(db, offset: int = 0, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    actor = current_actor()
    if actor.role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="list_patients", what="patient",
                           note="Patients may only read their own record.")
    total = db.scalar(select(func.count(models.Patient.id))) or 0
    rows = db.scalars(
        select(models.Patient).order_by(models.Patient.id).offset(max(0, offset)).limit(limit)
    ).all()
    return {"total": int(total), "offset": max(0, offset), "limit": limit,
            "patients": [_patient_brief(p) for p in rows]}


@tool("patient_timeline", "Chronological timeline of a patient's appointments, prescriptions and files.")
def _patient_timeline(db, patient_id: int | None = None, limit: int = 20):
    pid = _scope_patient_id(db, patient_id)
    if not pid or not db.get(models.Patient, pid):
        return grounded_no(server=SPEC.key, tool="patient_timeline", what="patient")
    limit = max(1, min(int(limit), 50))

    appts = db.scalars(
        select(models.Appointment)
        .where(models.Appointment.patient_id == pid)
        .order_by(models.Appointment.appointment_date.desc(), models.Appointment.start_time.desc())
        .limit(limit)
    ).all()
    presc = db.scalars(
        select(models.Prescription)
        .where(models.Prescription.patient_id == pid)
        .order_by(models.Prescription.issued_at.desc())
        .limit(limit)
    ).all()
    files = db.scalars(
        select(models.MedicalFile)
        .where(models.MedicalFile.patient_id == pid, models.MedicalFile.is_archived.is_(False))
        .order_by(models.MedicalFile.id.desc())
        .limit(limit)
    ).all()

    events: list[dict] = []
    for a in appts:
        events.append({"kind": "appointment", "date": str(a.appointment_date), "time": str(a.start_time),
                       "code": a.appointment_code, "status": a.status,
                       "doctor": a.doctor.full_name if a.doctor else None})
    for p in presc:
        events.append({"kind": "prescription", "date": str(p.issued_at)[:10], "code": p.prescription_code,
                       "status": p.status, "diagnosis_summary": p.diagnosis_summary})
    for f in files:
        events.append({"kind": "file", "date": str(f.id), "title": f.title, "category": f.category})
    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    return {"patient_id": pid, "events": events[:limit]}


@tool("patient_summary", "Compact clinical/administrative summary for one patient.")
def _patient_summary(db, patient_id: int | None = None):
    pid = _scope_patient_id(db, patient_id)
    patient = db.get(models.Patient, pid) if pid else None
    if patient is None:
        return grounded_no(server=SPEC.key, tool="patient_summary", what="patient")

    def _count(model, *where):
        return int(db.scalar(select(func.count(model.id)).where(*where)) or 0)

    return {
        "patient": _patient_brief(patient),
        "appointments_total": _count(models.Appointment, models.Appointment.patient_id == pid),
        "appointments_upcoming": _count(models.Appointment, models.Appointment.patient_id == pid,
                                        models.Appointment.status.in_(("PENDING", "CONFIRMED"))),
        "prescriptions": _count(models.Prescription, models.Prescription.patient_id == pid),
        "files": _count(models.MedicalFile, models.MedicalFile.patient_id == pid),
        "invoices_open": _count(models.Invoice, models.Invoice.patient_id == pid,
                                models.Invoice.balance_amount > 0),
    }


mcp = make_server(
    SPEC,
    "Patient Server. Answers come only from the hospital MySQL database - "
    "verified rows, never modelled guesses.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
