"""MCP server 04 - Prescription & Records Server  [USER tier].

Prescriptions + medicine lines, medicine master, consultations and
medical records.  Patients see only their own rows.
"""
from __future__ import annotations

from sqlalchemy import select

from app import models

from ..guard import grounded_no
from ..rbac import current_actor
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["prescriptions"]
TOOLS: dict = {}

STAFF_ROLES = {"SUPER_ADMIN", "ADMIN", "DOCTOR", "RECEPTIONIST", "ACCOUNTANT"}


def _visible_patient_id(db, requested: int | None) -> int | None:
    """PATIENT callers may only read their own rows (see patients._scope_patient_id)."""
    actor = current_actor()
    if actor.role == "PATIENT":
        row = db.scalar(select(models.Patient).where(models.Patient.user_id == actor.user_id))
        own = row.id if row else -1
        if requested is None:
            return own
        return requested if requested == own else -1
    return requested


def _prescription_brief(p: models.Prescription) -> dict:
    return {
        "id": p.id,
        "prescription_code": p.prescription_code,
        "patient_id": p.patient_id,
        "patient": p.patient.full_name if p.patient else None,
        "doctor": p.doctor.full_name if p.doctor else None,
        "diagnosis_summary": p.diagnosis_summary,
        "advice": p.advice,
        "status": p.status,
        "issued_at": str(p.issued_at),
        "valid_until": str(p.valid_until) if p.valid_until else None,
    }


@tool("get_prescription", "Look up one prescription (with medicine lines) by id or code.")
def _get_prescription(db, prescription_id: int | None = None, prescription_code: str = ""):
    stmt = select(models.Prescription)
    if prescription_id:
        stmt = stmt.where(models.Prescription.id == prescription_id)
    elif prescription_code.strip():
        stmt = stmt.where(models.Prescription.prescription_code == prescription_code.strip().upper())
    else:
        return grounded_no(server=SPEC.key, tool="get_prescription", what="prescription",
                           note="Provide prescription_id or prescription_code.")
    presc = db.scalar(stmt)
    if presc is None:
        return grounded_no(server=SPEC.key, tool="get_prescription", what="prescription")
    pid = _visible_patient_id(db, presc.patient_id)
    if current_actor().role == "PATIENT" and presc.patient_id != pid:
        return grounded_no(server=SPEC.key, tool="get_prescription", what="prescription")
    items = db.scalars(
        select(models.PrescriptionItem).where(models.PrescriptionItem.prescription_id == presc.id)
    ).all()
    brief = _prescription_brief(presc)
    brief["items"] = [{
        "medicine": i.medicine_name, "dosage": i.dosage, "frequency": i.frequency,
        "duration": i.duration, "instructions": i.instructions, "quantity": i.quantity,
    } for i in items]
    return brief


@tool("list_prescriptions", "List prescriptions for a patient (or all, for staff).")
def _list_prescriptions(db, patient_id: int | None = None, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    pid = _visible_patient_id(db, patient_id)
    stmt = select(models.Prescription)
    if pid:
        stmt = stmt.where(models.Prescription.patient_id == pid)
    elif current_actor().role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="list_prescriptions", what="prescription")
    rows = db.scalars(stmt.order_by(models.Prescription.issued_at.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_prescriptions", what="prescription")
    return [_prescription_brief(p) for p in rows]


@tool("list_medicines", "Search the medicine master by name / generic name.")
def _list_medicines(db, query: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.Medicine).where(models.Medicine.is_active.is_(True))
    q = (query or "").strip()
    if q:
        from sqlalchemy import or_

        like = f"%{q}%"
        stmt = stmt.where(or_(models.Medicine.name.ilike(like),
                              models.Medicine.generic_name.ilike(like)))
    rows = db.scalars(stmt.order_by(models.Medicine.name).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_medicines", what="medicine")
    return [{"id": m.id, "name": m.name, "generic_name": m.generic_name, "form": m.form,
             "strength": m.strength, "manufacturer": m.manufacturer,
             "default_dosage": m.default_dosage, "default_frequency": m.default_frequency,
             "default_duration": m.default_duration} for m in rows]


@tool("get_consultation", "Read one consultation (vitals, observations, advice).")
def _get_consultation(db, consultation_id: int):
    cons = db.get(models.Consultation, consultation_id)
    if cons is None:
        return grounded_no(server=SPEC.key, tool="get_consultation", what="consultation")
    pid = _visible_patient_id(db, cons.patient_id)
    if current_actor().role == "PATIENT" and cons.patient_id != pid:
        return grounded_no(server=SPEC.key, tool="get_consultation", what="consultation")
    return {
        "id": cons.id,
        "consultation_code": cons.consultation_code,
        "patient": cons.patient.full_name if cons.patient else None,
        "doctor": cons.doctor.full_name if cons.doctor else None,
        "status": cons.status,
        "chief_complaint": cons.chief_complaint,
        "vitals": cons.vitals,
        "observations": cons.observations,
        "diagnosis": cons.diagnosis,
        "advice": cons.advice,
        "follow_up_required": bool(cons.follow_up_required),
        "follow_up_date": str(cons.follow_up_date) if cons.follow_up_date else None,
    }


@tool("list_medical_records", "Longitudinal medical records for a patient.")
def _list_medical_records(db, patient_id: int | None = None, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    pid = _visible_patient_id(db, patient_id)
    if not pid:
        return grounded_no(server=SPEC.key, tool="list_medical_records", what="record",
                           note="Provide patient_id (staff) or sign in as the patient.")
    stmt = select(models.MedicalRecord).where(models.MedicalRecord.patient_id == pid)
    if current_actor().role == "PATIENT":
        stmt = stmt.where(models.MedicalRecord.is_visible_to_patient.is_(True))
    rows = db.scalars(stmt.order_by(models.MedicalRecord.recorded_at.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_medical_records", what="record")
    return [{"id": r.id, "record_type": r.record_type, "title": r.title,
             "description": r.description, "severity": r.severity,
             "recorded_at": str(r.recorded_at)} for r in rows]


mcp = make_server(
    SPEC,
    "Prescription & Records Server. Clinical rows only - never generated "
    "medical advice.  A missing record is answered as a grounded 'no'.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
