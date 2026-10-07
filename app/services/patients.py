"""Patient service: registration, ID generation, search and history aggregation."""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..security import hash_password, utcnow


def next_patient_code(db: Session) -> str:
    year = date.today().year
    count = db.scalar(select(func.count(models.Patient.id))) or 0
    return f"VVH-{year}-{count + 1:05d}"


def next_doctor_code(db: Session) -> str:
    count = db.scalar(select(func.count(models.Doctor.id))) or 0
    return f"DOC-{count + 1:04d}"


def _age_from_dob(dob: date | None) -> int | None:
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def create_patient_record(
    db: Session,
    *,
    full_name: str,
    phone: str,
    email: str | None = None,
    date_of_birth: date | None = None,
    age: int | None = None,
    gender: str | None = None,
    blood_group: str | None = None,
    address_line: str | None = None,
    city: str | None = None,
    state: str | None = None,
    pincode: str | None = None,
    alternate_phone: str | None = None,
    emergency_contact_name: str | None = None,
    emergency_contact_phone: str | None = None,
    emergency_contact_relation: str | None = None,
    allergies: str | None = None,
    chronic_conditions: str | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    created_by: int | None = None,
    source: str = "STAFF",
) -> models.Patient:
    patient = models.Patient(
        patient_code=next_patient_code(db),
        user_id=user_id,
        full_name=full_name.strip(),
        phone=phone.strip(),
        email=email.lower().strip() if email else None,
        date_of_birth=date_of_birth,
        age=age or _age_from_dob(date_of_birth),
        gender=gender,
        blood_group=blood_group,
        address_line=address_line,
        city=city,
        state=state,
        pincode=pincode,
        alternate_phone=alternate_phone,
        emergency_contact_name=emergency_contact_name,
        emergency_contact_phone=emergency_contact_phone,
        emergency_contact_relation=emergency_contact_relation,
        allergies=allergies,
        chronic_conditions=chronic_conditions,
        notes=notes or f"Registered via {source}",
        created_by=created_by,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def create_patient_with_login(db: Session, *, password: str | None = None, role_name: str = "PATIENT",
                              **kwargs) -> tuple[models.Patient, models.User | None]:
    """Register a patient and (optionally) a portal login in one shot."""
    email = kwargs.get("email")
    phone = kwargs.get("phone")
    user = None
    if email:
        existing = db.scalar(select(models.User).where(models.User.email == email.lower()))
        if existing:
            user = existing
    if not user and email:
        role = db.scalar(select(models.Role).where(models.Role.name == role_name))
        user = models.User(
            uuid=__import__("uuid").uuid4().hex,
            full_name=kwargs.get("full_name", "Patient"),
            email=email.lower(),
            phone=phone,
            password_hash=hash_password(password or f"Vvh@{phone[-4:] if phone else '0000'}"),
            role_id=role.id,
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    patient = create_patient_record(db, user_id=user.id if user else None, **kwargs)
    return patient, user


def search_patients(db: Session, query: str = "", *, limit: int = 25, active_only: bool = True) -> list[models.Patient]:
    stmt = select(models.Patient)
    if query:
        like = f"%{query.strip()}%"
        digits = "".join(ch for ch in query if ch.isdigit())
        conditions = [
            models.Patient.full_name.like(like),
            models.Patient.patient_code.like(like),
            models.Patient.email.like(like),
        ]
        if digits:
            conditions.append(models.Patient.phone.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conditions))
    if active_only:
        stmt = stmt.where(models.Patient.is_active.is_(True))
    return list(db.scalars(stmt.order_by(models.Patient.created_at.desc()).limit(limit)))


def patient_stats(db: Session, patient_id: int) -> dict:
    visits = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.patient_id == patient_id)) or 0
    completed = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.patient_id == patient_id, models.Appointment.status == "COMPLETED")) or 0
    upcoming = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.patient_id == patient_id,
        models.Appointment.appointment_date >= date.today(),
        models.Appointment.status.in_(["PENDING", "CONFIRMED"]))) or 0
    consultations = db.scalar(select(func.count(models.Consultation.id)).where(
        models.Consultation.patient_id == patient_id)) or 0
    prescriptions = db.scalar(select(func.count(models.Prescription.id)).where(
        models.Prescription.patient_id == patient_id)) or 0
    files = db.scalar(select(func.count(models.MedicalFile.id)).where(
        models.MedicalFile.patient_id == patient_id, models.MedicalFile.is_archived.is_(False))) or 0
    billed = float(db.scalar(select(func.coalesce(func.sum(models.Invoice.total_amount), 0)).where(
        models.Invoice.patient_id == patient_id)) or 0)
    due = float(db.scalar(select(func.coalesce(func.sum(models.Invoice.balance_amount), 0)).where(
        models.Invoice.patient_id == patient_id, models.Invoice.status.in_(["UNPAID", "PARTIAL"]))) or 0)
    reviews = db.scalar(select(func.count(models.Review.id)).where(
        models.Review.patient_id == patient_id)) or 0
    ai_conversations = db.scalar(select(func.count(models.AIConversation.id)).where(
        models.AIConversation.patient_id == patient_id)) or 0
    return {
        "total_appointments": visits,
        "completed_appointments": completed,
        "upcoming_appointments": upcoming,
        "consultations": consultations,
        "prescriptions": prescriptions,
        "medical_files": files,
        "total_billed": round(billed, 2),
        "outstanding_amount": round(due, 2),
        "reviews": reviews,
        "ai_conversations": ai_conversations,
        "last_visit": _last_visit(db, patient_id),
    }


def _last_visit(db: Session, patient_id: int) -> str | None:
    appt = db.scalar(
        select(models.Appointment).where(
            models.Appointment.patient_id == patient_id,
            models.Appointment.status == "COMPLETED",
        ).order_by(models.Appointment.appointment_date.desc()).limit(1)
    )
    return appt.appointment_date.isoformat() if appt else None


def full_profile(db: Session, patient: models.Patient) -> dict:
    """Everything the patient-profile screen (and AI context) needs."""
    appointments = db.scalars(
        select(models.Appointment).where(models.Appointment.patient_id == patient.id)
        .order_by(models.Appointment.appointment_date.desc(), models.Appointment.start_time.desc())
    ).all()
    upcoming = [a for a in appointments
                if a.appointment_date >= date.today() and a.status in {"PENDING", "CONFIRMED"}]
    previous = [a for a in appointments if a not in upcoming]

    consultations = db.scalars(
        select(models.Consultation).where(models.Consultation.patient_id == patient.id)
        .order_by(models.Consultation.created_at.desc())
    ).all()
    records = db.scalars(
        select(models.MedicalRecord).where(models.MedicalRecord.patient_id == patient.id)
        .order_by(models.MedicalRecord.recorded_at.desc()).limit(50)
    ).all()
    files = db.scalars(
        select(models.MedicalFile).where(
            models.MedicalFile.patient_id == patient.id,
            models.MedicalFile.is_archived.is_(False),
        ).order_by(models.MedicalFile.created_at.desc())
    ).all()
    prescriptions = db.scalars(
        select(models.Prescription).where(models.Prescription.patient_id == patient.id)
        .order_by(models.Prescription.issued_at.desc())
    ).all()
    invoices = db.scalars(
        select(models.Invoice).where(models.Invoice.patient_id == patient.id)
        .order_by(models.Invoice.issued_at.desc())
    ).all()
    payments = db.scalars(
        select(models.Payment).where(models.Payment.patient_id == patient.id)
        .order_by(models.Payment.created_at.desc())
    ).all()
    reviews = db.scalars(
        select(models.Review).where(models.Review.patient_id == patient.id)
        .order_by(models.Review.created_at.desc())
    ).all()
    conversations = db.scalars(
        select(models.AIConversation).where(models.AIConversation.patient_id == patient.id)
        .order_by(models.AIConversation.started_at.desc()).limit(20)
    ).all()

    return {
        "upcoming_appointments": upcoming,
        "previous_appointments": previous,
        "consultations": consultations,
        "medical_records": records,
        "prescriptions": prescriptions,
        "invoices": invoices,
        "payments": payments,
        "reviews": reviews,
        "files": files,
        "ai_conversations": conversations,
        "stats": patient_stats(db, patient.id),
    }


def add_medical_record(db: Session, *, patient_id: int, title: str, record_type: str = "NOTE",
                       description: str | None = None, consultation_id: int | None = None,
                       appointment_id: int | None = None, doctor_id: int | None = None,
                       values_json: dict | None = None, severity: str = "NORMAL",
                       entered_by: int | None = None, visible: bool = True,
                       supersedes_id: int | None = None) -> models.MedicalRecord:
    version = 1
    if supersedes_id:
        old = db.get(models.MedicalRecord, supersedes_id)
        if old:
            version = (old.version or 1) + 1
    record = models.MedicalRecord(
        patient_id=patient_id, consultation_id=consultation_id, appointment_id=appointment_id,
        doctor_id=doctor_id, record_type=record_type, title=title, description=description,
        values_json=values_json, severity=severity, entered_by=entered_by,
        is_visible_to_patient=visible, recorded_at=utcnow(), version=version,
        supersedes_id=supersedes_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
