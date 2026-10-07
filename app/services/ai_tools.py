"""Controlled hospital tool layer exposed to the Groq assistant. The registry exposes 18 tools.

Every tool is a plain function:  fn(db, ctx, **kwargs) -> dict
`ctx` carries the runtime context (patient_id, conversation_id, user_id, caller).

Tools never give medical advice, never diagnose and never prescribe. They only
read records and operate the appointment/billing rails.
"""
from __future__ import annotations

import logging
import random
import string
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models
from ..codes import unique_code
from ..security import utcnow
from . import billing, notify, slots as slot_service

log = logging.getLogger("vvh.ai.tools")


# --------------------------------------------------------------------------
# tool context
# --------------------------------------------------------------------------
class ToolContext:
    def __init__(self, db: Session, *, patient_id: int | None = None, conversation_id: int | None = None,
                 user_id: int | None = None, caller: str = "AI", channel: str = "WEB"):
        self.db = db
        self.patient_id = patient_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.caller = caller
        self.channel = channel

    def patient(self) -> models.Patient | None:
        return self.db.get(models.Patient, self.patient_id) if self.patient_id else None

    def require_patient(self) -> models.Patient:
        patient = self.patient()
        if not patient:
            raise ValueError("No patient is identified in this conversation yet")
        return patient


def _appointment_code(db: Session) -> str:
    """Collision-safe appointment code for AI bookings."""
    return unique_code(db, prefix="APT", model=models.Appointment,
                       column=models.Appointment.appointment_code)


# ==========================================================================
# PATIENT CONTEXT TOOLS
# ==========================================================================
def get_patient_profile(db: Session, ctx: ToolContext, patient_id: int | None = None) -> dict:
    """Fetch the patient's demographics and contact profile."""
    pid = patient_id or ctx.patient_id
    patient = db.get(models.Patient, pid) if pid else None
    if not patient:
        return {"found": False, "message": "Patient not found"}
    return {
        "found": True,
        "patient_id": patient.id,
        "patient_code": patient.patient_code,
        "full_name": patient.full_name,
        "age": patient.age,
        "gender": patient.gender,
        "blood_group": patient.blood_group,
        "phone": patient.phone,
        "email": patient.email,
        "city": patient.city,
        "allergies": patient.allergies,
        "chronic_conditions": patient.chronic_conditions,
        "emergency_contact": {
            "name": patient.emergency_contact_name,
            "phone": patient.emergency_contact_phone,
            "relation": patient.emergency_contact_relation,
        },
    }


def get_patient_summary(db: Session, ctx: ToolContext, patient_id: int | None = None) -> dict:
    """Build the AI context bundle: profile + concerns + visits + active meds."""
    pid = patient_id or ctx.patient_id
    if not pid:
        return {"found": False}
    patient = db.get(models.Patient, pid)
    if not patient:
        return {"found": False}

    from .ai_engine import build_patient_context_summary  # local import avoids cycle
    summary = build_patient_context_summary(db, patient)
    summary["found"] = True
    return summary


def get_patient_concerns(db: Session, ctx: ToolContext, patient_id: int | None = None,
                         limit: int = 10) -> dict:
    """List the patient's recorded concerns and their history."""
    pid = patient_id or ctx.patient_id
    rows = db.scalars(
        select(models.AIConcern)
        .where(models.AIConcern.patient_id == pid)
        .order_by(models.AIConcern.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "count": len(rows),
        "concerns": [{
            "id": c.id,
            "concern": c.concern_text,
            "keywords": c.keywords,
            "specialty_id": c.specialty_id,
            "urgency": c.urgency,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        } for c in rows],
    }


def get_patient_appointments(db: Session, ctx: ToolContext, patient_id: int | None = None,
                             upcoming_only: bool = False, limit: int = 10) -> dict:
    """List past and upcoming appointments (previous appointments context)."""
    pid = patient_id or ctx.patient_id
    stmt = (
        select(models.Appointment)
        .where(models.Appointment.patient_id == pid)
        .order_by(models.Appointment.appointment_date.desc(), models.Appointment.start_time.desc())
        .limit(limit)
    )
    if upcoming_only:
        stmt = stmt.where(
            models.Appointment.appointment_date >= date.today(),
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        ).order_by(models.Appointment.appointment_date.asc())
    rows = db.scalars(stmt).all()
    return {
        "count": len(rows),
        "appointments": [{
            "id": a.id,
            "code": a.appointment_code,
            "doctor": a.doctor.full_name if a.doctor else None,
            "specialty": a.specialty.name if a.specialty else None,
            "date": a.appointment_date.isoformat(),
            "time": a.start_time.strftime("%H:%M"),
            "status": a.status,
            "queue_status": a.queue_status,
            "token": a.token_number,
            "reason": a.reason,
        } for a in rows],
    }


def get_patient_prescriptions(db: Session, ctx: ToolContext, patient_id: int | None = None,
                              limit: int = 5) -> dict:
    """Fetch recent prescriptions and their medicines."""
    pid = patient_id or ctx.patient_id
    rows = db.scalars(
        select(models.Prescription)
        .where(models.Prescription.patient_id == pid)
        .order_by(models.Prescription.issued_at.desc())
        .limit(limit)
    ).all()
    return {
        "count": len(rows),
        "prescriptions": [{
            "id": p.id,
            "code": p.prescription_code,
            "doctor": db.get(models.Doctor, p.doctor_id).full_name if p.doctor_id else None,
            "issued_at": p.issued_at.isoformat() if p.issued_at else None,
            "diagnosis": p.diagnosis_summary,
            "pdf_url": f"/api/v1/prescriptions/{p.id}/pdf",
            "items": [{
                "medicine": i.medicine_name, "dosage": i.dosage, "frequency": i.frequency,
                "duration": i.duration, "instructions": i.instructions,
            } for i in db.scalars(select(models.PrescriptionItem).where(
                models.PrescriptionItem.prescription_id == p.id))],
        } for p in rows],
    }


def get_patient_files(db: Session, ctx: ToolContext, patient_id: int | None = None,
                      category: str | None = None, limit: int = 10) -> dict:
    """Look up lab reports, scans, images and other medical files."""
    pid = patient_id or ctx.patient_id
    stmt = select(models.MedicalFile).where(
        models.MedicalFile.patient_id == pid,
        models.MedicalFile.is_archived.is_(False),
    ).order_by(models.MedicalFile.created_at.desc()).limit(limit)
    if category:
        stmt = stmt.where(models.MedicalFile.category == category)
    rows = db.scalars(stmt).all()
    return {
        "count": len(rows),
        "files": [{
            "id": f.id,
            "category": f.category,
            "title": f.title,
            "uploaded_at": f.created_at.isoformat() if f.created_at else None,
            "url": f"/api/v1/medical-files/{f.id}/download",
            "size_kb": round((f.size_bytes or 0) / 1024, 1),
        } for f in rows],
    }


# ==========================================================================
# ROUTING TOOLS
# ==========================================================================
def search_specialties(db: Session, ctx: ToolContext, query: str = "", limit: int = 6) -> dict:
    """Find specialties by keyword, concern text or name."""
    stmt = select(models.Specialty).where(models.Specialty.is_active.is_(True))
    rows = db.scalars(stmt).all()
    q = (query or "").lower().strip()
    scored: list[tuple[int, models.Specialty]] = []
    for sp in rows:
        score = 0
        if q and q in sp.name.lower():
            score += 5
        if q and sp.description and q in sp.description.lower():
            score += 2
        keywords = db.scalars(
            select(models.SpecialtyConcern).where(models.SpecialtyConcern.specialty_id == sp.id)
        ).all()
        for kw in keywords:
            if q and kw.keyword.lower() in q:
                score += 3 * (kw.weight or 1)
        scored.append((score, sp))
    scored.sort(key=lambda t: -t[0])
    chosen = [sp for score, sp in scored if score > 0][:limit] or [sp for _, sp in scored[:limit]]
    return {
        "count": len(chosen),
        "specialties": [{
            "id": sp.id,
            "code": sp.code,
            "name": sp.name,
            "description": sp.description,
            "consultation_fee": float(sp.consultation_fee or 0),
            "doctor_count": db.scalar(select(func.count(models.Doctor.id)).where(
                models.Doctor.specialty_id == sp.id, models.Doctor.is_active.is_(True))) or 0,
            "concerns": [k.keyword for k in db.scalars(select(models.SpecialtyConcern).where(
                models.SpecialtyConcern.specialty_id == sp.id))],
        } for sp in chosen],
    }


def search_doctors(db: Session, ctx: ToolContext, specialty_id: int | None = None,
                   query: str = "", limit: int = 5, only_ai_bookable: bool = True) -> dict:
    """Find doctors, optionally filtered by specialty or name."""
    stmt = select(models.Doctor).where(models.Doctor.is_active.is_(True))
    if specialty_id:
        stmt = stmt.where(models.Doctor.specialty_id == specialty_id)
    if only_ai_bookable:
        stmt = stmt.where(models.Doctor.is_available_for_ai_booking.is_(True))
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(models.Doctor.full_name.like(like), models.Doctor.qualifications.like(like)))
    rows = db.scalars(stmt.limit(limit)).all()
    out = []
    for doc in rows:
        next_slot = db.scalar(
            select(models.Slot)
            .where(
                models.Slot.doctor_id == doc.id,
                models.Slot.status == "AVAILABLE",
                models.Slot.slot_date >= date.today(),
            )
            .order_by(models.Slot.slot_date, models.Slot.start_time)
            .limit(1)
        )
        out.append({
            "id": doc.id,
            "doctor_code": doc.doctor_code,
            "name": doc.full_name,
            "specialty_id": doc.specialty_id,
            "specialty": doc.specialty.name if doc.specialty else None,
            "qualifications": doc.qualifications,
            "experience_years": doc.experience_years,
            "languages": doc.languages,
            "fee": float(doc.consultation_fee or 0),
            "rating": float(doc.rating_avg or 0),
            "rating_count": doc.rating_count or 0,
            "next_available": (
                f"{next_slot.slot_date.isoformat()} {next_slot.start_time.strftime('%H:%M')}"
                if next_slot else None
            ),
        })
    return {"count": len(out), "doctors": out}


def get_doctor_availability(db: Session, ctx: ToolContext, doctor_id: int,
                            days_ahead: int = 7) -> dict:
    """Working days/hours, breaks, leaves and holidays for a doctor."""
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        return {"found": False}
    schedules = db.scalars(select(models.DoctorSchedule).where(
        models.DoctorSchedule.doctor_id == doctor_id,
        models.DoctorSchedule.is_active.is_(True),
    )).all()
    today = date.today()
    leaves = db.scalars(select(models.DoctorLeave).where(
        models.DoctorLeave.doctor_id == doctor_id,
        models.DoctorLeave.end_date >= today,
        models.DoctorLeave.status == "APPROVED",
    )).all()
    holidays = db.scalars(select(models.Holiday).where(models.Holiday.holiday_date >= today)).all()
    slot_counts = db.execute(
        select(models.Slot.slot_date, func.count(models.Slot.id))
        .where(
            models.Slot.doctor_id == doctor_id,
            models.Slot.status == "AVAILABLE",
            models.Slot.slot_date >= today,
            models.Slot.slot_date <= today + timedelta(days=days_ahead),
        )
        .group_by(models.Slot.slot_date)
    ).all()
    return {
        "found": True,
        "doctor_id": doctor_id,
        "doctor_name": doctor.full_name,
        "slot_duration_minutes": doctor.slot_duration_minutes,
        "daily_capacity": doctor.daily_capacity,
        "working_days": [{
            "day": slot_service.DAY_NAMES[s.day_of_week],
            "start": s.start_time.strftime("%H:%M"),
            "end": s.end_time.strftime("%H:%M"),
            "break": f"{s.break_start.strftime('%H:%M')}-{s.break_end.strftime('%H:%M')}"
            if s.break_start and s.break_end else None,
            "capacity_per_slot": s.capacity_per_slot,
        } for s in schedules],
        "leaves": [{
            "type": l.leave_type,
            "from": l.start_date.isoformat(),
            "to": l.end_date.isoformat(),
            "reason": l.reason,
        } for l in leaves],
        "holidays": [{"date": h.holiday_date.isoformat(), "name": h.name} for h in holidays],
        "open_slots_per_day": {d.isoformat(): c for d, c in slot_counts},
    }


def get_available_slots(db: Session, ctx: ToolContext, doctor_id: int | None = None,
                        specialty_id: int | None = None, days_ahead: int = 7,
                        limit: int = 6) -> dict:
    """Live slot search across the next N days."""
    results = slot_service.available_slots(
        db,
        doctor_id=doctor_id,
        specialty_id=specialty_id,
        from_date=date.today(),
        to_date=date.today() + timedelta(days=days_ahead),
        limit=limit,
    )
    return {"count": len(results), "slots": [{**r, "slot_date": r["slot_date"].isoformat(),
                                              "start_time": r["start_time"].strftime("%H:%M"),
                                              "end_time": r["end_time"].strftime("%H:%M")} for r in results]}


# ==========================================================================
# BOOKING TOOLS
# ==========================================================================
def hold_slot(db: Session, ctx: ToolContext, slot_id: int, minutes: int = 10) -> dict:
    """Temporarily hold a slot while the patient confirms."""
    slot = db.get(models.Slot, slot_id)
    if not slot:
        return {"success": False, "error": "Slot not found"}
    try:
        slot = slot_service.hold_slot(db, slot, patient_id=ctx.patient_id,
                                      conversation_id=ctx.conversation_id, minutes=minutes)
    except ValueError as exc:
        alt = slot_service.available_slots(db, doctor_id=slot.doctor_id, limit=3)
        return {"success": False, "error": str(exc), "alternatives": alt}
    return {
        "success": True,
        "slot_id": slot.id,
        "hold_token": slot.hold_token,
        "expires_at": slot.hold_expires_at.isoformat() if slot.hold_expires_at else None,
        "label": f"{slot.slot_date.strftime('%a %d %b')} {slot_service.slot_label(slot)}",
    }


def release_slot(db: Session, ctx: ToolContext, slot_id: int) -> dict:
    """Release a held slot back to the pool."""
    slot = db.get(models.Slot, slot_id)
    if not slot:
        return {"success": False, "error": "Slot not found"}
    slot_service.release_slot(db, slot, actor=ctx.caller, conversation_id=ctx.conversation_id)
    return {"success": True, "slot_id": slot_id, "message": "Slot released"}


def book_appointment(
    db: Session,
    ctx: ToolContext,
    slot_id: int,
    patient_id: int | None = None,
    reason: str | None = None,
    concern_summary: str | None = None,
    hold_token: str | None = None,
    source: str = "AI",
    notify_patient: bool = True,
) -> dict:
    """Book an appointment on a specific slot (validates hold + availability)."""
    pid = patient_id or ctx.patient_id
    patient = db.get(models.Patient, pid) if pid else None
    if not patient:
        return {"success": False, "error": "Patient must be identified before booking"}
    slot = db.get(models.Slot, slot_id)
    if not slot:
        return {"success": False, "error": "Slot not found"}
    if slot.status == "BOOKED" or slot.booked_count >= slot.capacity:
        alternatives = slot_service.available_slots(db, doctor_id=slot.doctor_id, limit=3)
        return {"success": False, "error": "That slot just got booked", "alternatives": alternatives}
    if slot.status == "HELD" and slot.hold_token and hold_token and slot.hold_token != hold_token:
        if slot.hold_expires_at and slot.hold_expires_at > utcnow():
            return {"success": False, "error": "Slot is held by another booking session"}

    doctor = db.get(models.Doctor, slot.doctor_id)
    token_number = (db.scalar(
        select(func.coalesce(func.max(models.Appointment.token_number), 0)).where(
            models.Appointment.doctor_id == doctor.id,
            models.Appointment.appointment_date == slot.slot_date,
        )
    ) or 0) + 1

    appointment = models.Appointment(
        appointment_code=_appointment_code(db),
        patient_id=patient.id,
        doctor_id=doctor.id,
        specialty_id=doctor.specialty_id,
        slot_id=slot.id,
        appointment_date=slot.slot_date,
        start_time=slot.start_time,
        end_time=slot.end_time,
        token_number=token_number,
        status="CONFIRMED",
        queue_status="WAITING",
        source=source,
        reason=reason or concern_summary,
        ai_concern_summary=concern_summary,
        booked_by=ctx.user_id,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    slot_service.mark_slot_booked(db, slot, conversation_id=ctx.conversation_id)

    invoice = billing.ensure_consultation_invoice(db, appointment, doctor)

    if notify_patient:
        notify.notify_patient(db, patient, "APPOINTMENT_CONFIRMATION", {
            "patient_name": patient.full_name,
            "doctor_name": doctor.full_name,
            "specialty": doctor.specialty.name if doctor.specialty else "",
            "date": appointment.appointment_date.strftime("%d %b %Y"),
            "time": appointment.start_time.strftime("%H:%M"),
            "token": appointment.token_number,
            "fee": f"{float(doctor.consultation_fee or 0):.2f}",
            "code": appointment.appointment_code,
        }, channels=["WHATSAPP", "IN_APP"], appointment_id=appointment.id)

    return {
        "success": True,
        "appointment_id": appointment.id,
        "appointment_code": appointment.appointment_code,
        "doctor": doctor.full_name,
        "specialty": doctor.specialty.name if doctor.specialty else None,
        "date": appointment.appointment_date.isoformat(),
        "time": appointment.start_time.strftime("%H:%M"),
        "token": appointment.token_number,
        "fee": float(doctor.consultation_fee or 0),
        "status": appointment.status,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
    }


def cancel_appointment(db: Session, ctx: ToolContext, appointment_id: int, reason: str | None = None) -> dict:
    """Cancel an appointment and free its slot."""
    appointment = db.get(models.Appointment, appointment_id)
    if not appointment:
        return {"success": False, "error": "Appointment not found"}
    if appointment.status in {"CANCELLED", "COMPLETED"}:
        return {"success": False, "error": f"Appointment is already {appointment.status}"}
    appointment.status = "CANCELLED"
    appointment.queue_status = "NO_SHOW" if appointment.checked_in_at else "WAITING"
    appointment.cancellation_reason = reason or "Cancelled via AI assistant"
    appointment.cancelled_by = ctx.user_id
    db.commit()

    if appointment.slot_id:
        slot = db.get(models.Slot, appointment.slot_id)
        if slot:
            slot_service.free_slot(db, slot)

    notify.notify_patient(db, appointment.patient, "APPOINTMENT_CANCELLED", {
        "patient_name": appointment.patient.full_name,
        "code": appointment.appointment_code,
        "date": appointment.appointment_date.strftime("%d %b %Y"),
        "time": appointment.start_time.strftime("%H:%M"),
        "doctor_name": appointment.doctor.full_name if appointment.doctor else "",
        "reason": appointment.cancellation_reason,
    }, channels=["WHATSAPP", "IN_APP"], appointment_id=appointment.id)

    return {
        "success": True,
        "appointment_id": appointment.id,
        "appointment_code": appointment.appointment_code,
        "status": appointment.status,
        "message": "Appointment cancelled and slot released",
    }


def reschedule_appointment(db: Session, ctx: ToolContext, appointment_id: int, new_slot_id: int,
                           reason: str | None = None) -> dict:
    """Move an appointment to a new slot (old slot is released)."""
    appointment = db.get(models.Appointment, appointment_id)
    if not appointment:
        return {"success": False, "error": "Appointment not found"}
    new_slot = db.get(models.Slot, new_slot_id)
    if not new_slot:
        return {"success": False, "error": "New slot not found"}
    if new_slot.status == "BOOKED" or new_slot.booked_count >= new_slot.capacity:
        return {"success": False, "error": "New slot is not available",
                "alternatives": slot_service.available_slots(db, doctor_id=new_slot.doctor_id, limit=3)}

    old_snapshot = {"date": appointment.appointment_date.isoformat(),
                    "time": appointment.start_time.strftime("%H:%M"),
                    "doctor_id": appointment.doctor_id}

    if appointment.slot_id:
        old_slot = db.get(models.Slot, appointment.slot_id)
        if old_slot:
            slot_service.free_slot(db, old_slot)

    appointment.rescheduled_from_id = appointment.id
    appointment.slot_id = new_slot.id
    appointment.doctor_id = new_slot.doctor_id
    appointment.appointment_date = new_slot.slot_date
    appointment.start_time = new_slot.start_time
    appointment.end_time = new_slot.end_time
    appointment.status = "CONFIRMED"
    appointment.queue_status = "WAITING"
    appointment.reason = appointment.reason or reason
    doctor = db.get(models.Doctor, new_slot.doctor_id)
    appointment.specialty_id = doctor.specialty_id if doctor else appointment.specialty_id
    appointment.token_number = (db.scalar(
        select(func.coalesce(func.max(models.Appointment.token_number), 0)).where(
            models.Appointment.doctor_id == new_slot.doctor_id,
            models.Appointment.appointment_date == new_slot.slot_date,
        )
    ) or 0) + 1
    db.commit()
    slot_service.mark_slot_booked(db, new_slot, conversation_id=ctx.conversation_id)

    notify.notify_patient(db, appointment.patient, "APPOINTMENT_RESCHEDULED", {
        "patient_name": appointment.patient.full_name,
        "date": appointment.appointment_date.strftime("%d %b %Y"),
        "time": appointment.start_time.strftime("%H:%M"),
        "doctor_name": doctor.full_name if doctor else "",
        "code": appointment.appointment_code,
    }, channels=["WHATSAPP", "IN_APP"], appointment_id=appointment.id)

    return {
        "success": True,
        "appointment_id": appointment.id,
        "appointment_code": appointment.appointment_code,
        "previous": old_snapshot,
        "date": appointment.appointment_date.isoformat(),
        "time": appointment.start_time.strftime("%H:%M"),
        "doctor": doctor.full_name if doctor else None,
        "token": appointment.token_number,
    }


# ==========================================================================
# BILLING TOOLS
# ==========================================================================
def get_invoice(db: Session, ctx: ToolContext, invoice_id: int | None = None,
                patient_id: int | None = None, appointment_id: int | None = None) -> dict:
    """Look up invoice details (items, discount, tax, totals)."""
    pid = patient_id or ctx.patient_id
    invoice = None
    if invoice_id:
        invoice = db.get(models.Invoice, invoice_id)
    elif appointment_id:
        invoice = db.scalar(select(models.Invoice).where(models.Invoice.appointment_id == appointment_id))
    else:
        invoice = db.scalar(
            select(models.Invoice).where(models.Invoice.patient_id == pid)
            .order_by(models.Invoice.issued_at.desc()).limit(1)
        )
    if not invoice:
        return {"found": False, "message": "No invoice found"}
    items = db.scalars(select(models.InvoiceItem).where(models.InvoiceItem.invoice_id == invoice.id)).all()
    return {
        "found": True,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat(),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "items": [{"description": i.description, "qty": i.quantity, "amount": float(i.amount)} for i in items],
        "subtotal": float(invoice.subtotal),
        "discount": float(invoice.discount_amount),
        "tax": float(invoice.tax_amount),
        "total": float(invoice.total_amount),
        "paid": float(invoice.paid_amount),
        "balance": float(invoice.balance_amount),
        "receipt_url": f"/api/v1/invoices/{invoice.id}/receipt",
    }


def get_payment_status(db: Session, ctx: ToolContext, patient_id: int | None = None,
                       invoice_id: int | None = None) -> dict:
    """Payment history + outstanding balance."""
    pid = patient_id or ctx.patient_id
    stmt = select(models.Payment).where(models.Payment.patient_id == pid)
    if invoice_id:
        stmt = stmt.where(models.Payment.invoice_id == invoice_id)
    payments = db.scalars(stmt.order_by(models.Payment.created_at.desc()).limit(10)).all()
    outstanding = db.scalar(
        select(func.coalesce(func.sum(models.Invoice.balance_amount), 0)).where(
            models.Invoice.patient_id == pid,
            models.Invoice.status.in_(["UNPAID", "PARTIAL"]),
        )
    ) or 0
    return {
        "outstanding_amount": float(outstanding),
        "payments": [{
            "payment_code": p.payment_code,
            "amount": float(p.amount),
            "method": p.method,
            "status": p.status,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "invoice_id": p.invoice_id,
        } for p in payments],
    }


# ==========================================================================
# NOTIFICATION TOOL
# ==========================================================================
def send_notification(db: Session, ctx: ToolContext, template: str = "CUSTOM", message: str | None = None,
                      channel: str = "WHATSAPP", context: dict | None = None,
                      patient_id: int | None = None) -> dict:
    """Send a WhatsApp / Email / In-App notification to the patient."""
    pid = patient_id or ctx.patient_id
    patient = db.get(models.Patient, pid) if pid else None
    if not patient:
        return {"success": False, "error": "Patient must be identified"}
    ctx_data = context or {}
    ctx_data.setdefault("patient_name", patient.full_name)
    if message:
        ctx_data["message"] = message
    notif = notify.notify_patient(
        db, patient, template, ctx_data,
        channels=[channel, "IN_APP"] if channel != "IN_APP" else ["IN_APP"],
    )
    return {
        "success": True,
        "sent": [{"channel": n.channel, "template": n.template, "status": n.status} for n in notif],
    }


# ==========================================================================
# registry
# ==========================================================================
TOOLS: dict[str, dict] = {
    "get_patient_profile": {"fn": get_patient_profile, "category": "patient", "description": "Get the authenticated patient's profile.", "parameters": {"patient_id": "int|null"}},
    "get_patient_summary": {"fn": get_patient_summary, "category": "patient", "description": "Get the AI context bundle: profile, concerns, visits, prescriptions, files and outstanding balance.", "parameters": {"patient_id": "int|null"}},
    "get_patient_concerns": {"fn": get_patient_concerns, "category": "patient", "description": "List the authenticated patient's recorded concerns and their history.", "parameters": {"patient_id": "int|null", "limit": "int"}},
    "get_patient_appointments": {"fn": get_patient_appointments, "category": "patient", "description": "Get the authenticated patient's past and upcoming appointments.", "parameters": {"patient_id": "int|null", "upcoming_only": "bool", "limit": "int"}},
    "get_patient_prescriptions": {"fn": get_patient_prescriptions, "category": "patient", "description": "Get the authenticated patient's recent prescriptions and medicines.", "parameters": {"patient_id": "int|null", "limit": "int"}},
    "get_patient_files": {"fn": get_patient_files, "category": "patient", "description": "Get the authenticated patient's lab reports, scans and other medical files.", "parameters": {"patient_id": "int|null", "category": "string|null", "limit": "int"}},
    "search_specialties": {"fn": search_specialties, "category": "routing", "description": "Search active specialties.", "parameters": {"query": "string", "limit": "int"}},
    "search_doctors": {"fn": search_doctors, "category": "routing", "description": "Search doctors by specialty or name.", "parameters": {"specialty_id": "int|null", "query": "string", "limit": "int"}},
    "get_doctor_availability": {"fn": get_doctor_availability, "category": "routing", "description": "Get a doctor's working schedule and leave/holiday availability.", "parameters": {"doctor_id": "int", "days_ahead": "int"}},
    "get_available_slots": {"fn": get_available_slots, "category": "booking", "description": "Find live available appointment slots.", "parameters": {"doctor_id": "int|null", "specialty_id": "int|null", "days_ahead": "int", "limit": "int"}},
    "hold_slot": {"fn": hold_slot, "category": "booking", "description": "Temporarily hold a selected appointment slot.", "parameters": {"slot_id": "int", "minutes": "int"}},
    "release_slot": {"fn": release_slot, "category": "booking", "description": "Release a slot held by this conversation.", "parameters": {"slot_id": "int"}},
    "book_appointment": {"fn": book_appointment, "category": "booking", "description": "Book an appointment for the authenticated patient.", "parameters": {"slot_id": "int", "patient_id": "int|null", "reason": "string|null", "hold_token": "string|null", "notify_patient": "bool"}},
    "cancel_appointment": {"fn": cancel_appointment, "category": "booking", "description": "Cancel an authenticated patient's appointment.", "parameters": {"appointment_id": "int", "reason": "string|null"}},
    "reschedule_appointment": {"fn": reschedule_appointment, "category": "booking", "description": "Reschedule an authenticated patient's appointment.", "parameters": {"appointment_id": "int", "new_slot_id": "int", "reason": "string|null"}},
    "get_invoice": {"fn": get_invoice, "category": "billing", "description": "Get an authenticated patient's invoice and balance.", "parameters": {"invoice_id": "int|null", "patient_id": "int|null", "appointment_id": "int|null"}},
    "get_payment_status": {"fn": get_payment_status, "category": "billing", "description": "Get an authenticated patient's payment status and outstanding amount.", "parameters": {"patient_id": "int|null", "invoice_id": "int|null"}},
    "send_notification": {"fn": send_notification, "category": "notification", "description": "Send an allowed hospital notification to the authenticated patient.", "parameters": {"template": "string", "message": "string|null", "channel": "string", "patient_id": "int|null"}},
}

# tools that mutate data - flagged in the AI monitor
WRITE_TOOLS = {"hold_slot", "release_slot", "book_appointment", "cancel_appointment",
               "reschedule_appointment", "send_notification"}


def list_tools() -> list[dict]:
    return [{
        "name": name,
        "description": meta["description"],
        "parameters": meta.get("parameters", {}),
        "category": meta.get("category", "general"),
        "is_write": name in WRITE_TOOLS,
    } for name, meta in TOOLS.items()]


def invoke_tool(db: Session, ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Invoke a tool by name, recording the call in ai_tool_calls + audit trail."""
    if name not in TOOLS:
        raise KeyError(f"Unknown tool '{name}'")
    fn = TOOLS[name]["fn"]
    started = utcnow()
    status, error, result = "SUCCESS", None, None
    try:
        result = fn(db, ctx, **arguments)
        if isinstance(result, dict) and result.get("success") is False:
            status = "FAILED"
            error = str(result.get("error"))[:255]
    except Exception as exc:  # noqa: BLE001
        status, error = "FAILED", str(exc)[:255]
        result = {"success": False, "error": str(exc)}
        log.exception("Tool %s failed", name)
    duration_ms = int((utcnow() - started).total_seconds() * 1000)

    summary = result
    try:
        import json
        summary = json.dumps(result, default=str)[:500]
    except Exception:  # noqa: BLE001
        summary = str(result)[:500]

    db.add(models.AIToolCall(
        conversation_id=ctx.conversation_id,
        tool_name=name,
        arguments=arguments,
        result_summary=summary,
        status=status,
        error=error,
        duration_ms=duration_ms,
    ))
    db.commit()
    return result
