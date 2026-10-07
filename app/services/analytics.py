"""Dashboard metrics + analytics aggregation (admin, doctor, patient, AI monitor)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..security import utcnow
from . import slots as slot_service


def _count(db: Session, model, *conditions) -> int:
    return db.scalar(select(func.count(model.id)).where(*conditions)) or 0


def _money(db: Session, column, *conditions) -> float:
    return float(db.scalar(select(func.coalesce(func.sum(column), 0)).where(*conditions)) or 0)


def admin_dashboard(db: Session, days: int = 30) -> dict:
    today = date.today()
    month_start = today.replace(day=1)
    window_start = today - timedelta(days=days)
    yesterday = today - timedelta(days=1)

    total_patients = _count(db, models.Patient, models.Patient.is_active.is_(True))
    new_patients_month = _count(db, models.Patient,
                                func.date(models.Patient.created_at) >= month_start)
    new_patients_today = _count(db, models.Patient, func.date(models.Patient.created_at) == today)
    doctors = _count(db, models.Doctor, models.Doctor.is_active.is_(True))
    todays_appointments = _count(db, models.Appointment, models.Appointment.appointment_date == today)
    upcoming = _count(db, models.Appointment,
                      models.Appointment.appointment_date > today,
                      models.Appointment.status.in_(["PENDING", "CONFIRMED"]))
    cancelled = _count(db, models.Appointment,
                       models.Appointment.appointment_date >= window_start,
                       models.Appointment.status == "CANCELLED")
    no_shows = _count(db, models.Appointment,
                      models.Appointment.appointment_date >= window_start,
                      models.Appointment.status == "NO_SHOW")
    completed = _count(db, models.Appointment,
                       models.Appointment.appointment_date >= window_start,
                       models.Appointment.status == "COMPLETED")

    revenue_month = _money(db, models.Payment.amount,
                           models.Payment.status == "PAID",
                           func.date(models.Payment.paid_at) >= month_start)
    revenue_today = _money(db, models.Payment.amount,
                           models.Payment.status == "PAID",
                           func.date(models.Payment.paid_at) == today)
    pending_payments = _money(db, models.Invoice.balance_amount,
                              models.Invoice.status.in_(["UNPAID", "PARTIAL"]))

    reviews_pending = _count(db, models.Review, models.Review.status == "PENDING")
    avg_rating = db.scalar(select(func.coalesce(func.avg(models.Review.rating), 0)).where(
        models.Review.status == "APPROVED", models.Review.rating > 0)) or 0

    wa_total = _count(db, models.WhatsappMessage)
    wa_sent = _count(db, models.WhatsappMessage,
                     models.WhatsappMessage.status.in_(["SENT", "DELIVERED", "READ"]))
    wa_failed = _count(db, models.WhatsappMessage, models.WhatsappMessage.status == "FAILED")

    ai_conversations = _count(db, models.AIConversation)
    ai_active = _count(db, models.AIConversation, models.AIConversation.status == "ACTIVE")
    ai_bookings = db.scalar(select(func.coalesce(func.sum(models.AIConversation.successful_bookings), 0))) or 0
    ai_failed = db.scalar(select(func.coalesce(func.sum(models.AIConversation.failed_bookings), 0))) or 0
    ai_escalations = _count(db, models.AIEscalation, models.AIEscalation.status == "OPEN")
    ai_errors = db.scalar(select(func.coalesce(func.sum(models.AIConversation.error_count), 0))) or 0
    avg_resp = db.scalar(select(func.coalesce(func.avg(models.AIConversation.avg_response_ms), 0))) or 0
    ai_tool_calls = _count(db, models.AIToolCall)

    # time series
    appt_trend = _appointment_trend(db, window_start, today)
    revenue_trend = _revenue_trend(db, window_start, today)
    patient_growth = _patient_growth(db, window_start, today)
    specialty_demand = popular_specialties(db, window_start)
    doctor_util = slot_service.slot_utilisation(db)[:8]

    total_slots = _count(db, models.Slot,
                         models.Slot.slot_date >= today,
                         models.Slot.status.in_(["AVAILABLE", "BOOKED", "HELD"]))
    booked_slots = _count(db, models.Slot, models.Slot.slot_date >= today, models.Slot.status == "BOOKED")

    return {
        "generated_at": utcnow(),
        "cards": {
            "total_patients": total_patients,
            "new_patients_month": new_patients_month,
            "new_patients_today": new_patients_today,
            "doctors": doctors,
            "todays_appointments": todays_appointments,
            "upcoming_appointments": upcoming,
            "cancelled_appointments": cancelled,
            "no_shows": no_shows,
            "completed_appointments": completed,
            "revenue_month": round(revenue_month, 2),
            "revenue_today": round(revenue_today, 2),
            "pending_payments": round(pending_payments, 2),
            "reviews_pending": reviews_pending,
            "avg_rating": round(float(avg_rating), 2),
            "whatsapp_total": wa_total,
            "whatsapp_sent": wa_sent,
            "whatsapp_failed": wa_failed,
            "whatsapp_delivery_rate": round((wa_sent / wa_total) * 100, 1) if wa_total else 0,
            "ai_conversations": ai_conversations,
            "ai_active_conversations": ai_active,
            "ai_bookings": int(ai_bookings),
            "ai_failed_bookings": int(ai_failed),
            "ai_escalations_open": ai_escalations,
            "ai_errors": int(ai_errors),
            "ai_tool_calls": ai_tool_calls,
            "ai_avg_response_ms": int(avg_resp),
            "ai_booking_rate": round((float(ai_bookings) / ai_conversations) * 100, 1) if ai_conversations else 0,
            "slot_utilisation": round((booked_slots / total_slots) * 100, 1) if total_slots else 0,
            "cancellation_rate": round((cancelled / completed) * 100, 1) if completed else 0,
            "no_show_rate": round((no_shows / completed) * 100, 1) if completed else 0,
        },
        "charts": {
            "appointment_trend": appt_trend,
            "revenue_trend": revenue_trend,
            "patient_growth": patient_growth,
            "popular_specialties": specialty_demand,
            "doctor_utilisation": doctor_util,
            "appointment_status_breakdown": _status_breakdown(db, window_start),
            "ai_intent_breakdown": ai_intent_breakdown(db, window_start),
        },
    }


def _appointment_trend(db: Session, start: date, end: date) -> list[dict]:
    rows = db.execute(
        select(models.Appointment.appointment_date, func.count(models.Appointment.id))
        .where(models.Appointment.appointment_date >= start, models.Appointment.appointment_date <= end)
        .group_by(models.Appointment.appointment_date)
        .order_by(models.Appointment.appointment_date)
    ).all()
    data = {d.isoformat(): c for d, c in rows}
    out, cursor = [], start
    while cursor <= end:
        out.append({"date": cursor.isoformat(), "count": data.get(cursor.isoformat(), 0)})
        cursor += timedelta(days=1)
    return out


def _revenue_trend(db: Session, start: date, end: date) -> list[dict]:
    rows = db.execute(
        select(func.date(models.Payment.paid_at), func.coalesce(func.sum(models.Payment.amount), 0))
        .where(
            models.Payment.status == "PAID",
            func.date(models.Payment.paid_at) >= start,
            func.date(models.Payment.paid_at) <= end,
        )
        .group_by(func.date(models.Payment.paid_at))
        .order_by(func.date(models.Payment.paid_at))
    ).all()
    data = {str(d): float(v) for d, v in rows}
    out, cursor = [], start
    while cursor <= end:
        out.append({"date": cursor.isoformat(), "revenue": data.get(cursor.isoformat(), 0.0)})
        cursor += timedelta(days=1)
    return out


def _patient_growth(db: Session, start: date, end: date) -> list[dict]:
    rows = db.execute(
        select(func.date(models.Patient.created_at), func.count(models.Patient.id))
        .where(func.date(models.Patient.created_at) >= start, func.date(models.Patient.created_at) <= end)
        .group_by(func.date(models.Patient.created_at))
        .order_by(func.date(models.Patient.created_at))
    ).all()
    data = {str(d): c for d, c in rows}
    out, cursor, running = [], start, 0
    while cursor <= end:
        running += data.get(cursor.isoformat(), 0)
        out.append({"date": cursor.isoformat(), "new": data.get(cursor.isoformat(), 0), "cumulative": running})
        cursor += timedelta(days=1)
    return out


def popular_specialties(db: Session, start: date | None = None, limit: int = 8) -> list[dict]:
    stmt = (
        select(models.Specialty.name, func.count(models.Appointment.id))
        .join(models.Appointment, models.Appointment.specialty_id == models.Specialty.id)
        .group_by(models.Specialty.name)
        .order_by(func.count(models.Appointment.id).desc())
        .limit(limit)
    )
    if start:
        stmt = stmt.where(models.Appointment.appointment_date >= start)
    return [{"specialty": name, "appointments": count} for name, count in db.execute(stmt).all()]


def _status_breakdown(db: Session, start: date) -> list[dict]:
    rows = db.execute(
        select(models.Appointment.status, func.count(models.Appointment.id))
        .where(models.Appointment.appointment_date >= start)
        .group_by(models.Appointment.status)
    ).all()
    return [{"status": s, "count": c} for s, c in rows]


def ai_intent_breakdown(db: Session, start: date, limit: int = 10) -> list[dict]:
    rows = db.execute(
        select(models.AIConversation.primary_intent, func.count(models.AIConversation.id))
        .where(models.AIConversation.started_at >= datetime.combine(start, datetime.min.time()))
        .group_by(models.AIConversation.primary_intent)
        .order_by(func.count(models.AIConversation.id).desc())
        .limit(limit)
    ).all()
    return [{"intent": i or "UNKNOWN", "count": c} for i, c in rows]


def doctor_dashboard(db: Session, doctor_id: int) -> dict:
    today = date.today()
    todays = db.scalars(
        select(models.Appointment).where(
            models.Appointment.doctor_id == doctor_id,
            models.Appointment.appointment_date == today,
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        ).order_by(models.Appointment.start_time)
    ).all()
    upcoming = db.scalars(
        select(models.Appointment).where(
            models.Appointment.doctor_id == doctor_id,
            models.Appointment.appointment_date > today,
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        ).order_by(models.Appointment.appointment_date, models.Appointment.start_time).limit(20)
    ).all()
    schedule = db.scalars(select(models.DoctorSchedule).where(
        models.DoctorSchedule.doctor_id == doctor_id)).all()
    leaves = db.scalars(select(models.DoctorLeave).where(
        models.DoctorLeave.doctor_id == doctor_id,
        models.DoctorLeave.end_date >= today,
    )).all()
    follow_ups = db.scalars(select(models.Consultation).where(
        models.Consultation.doctor_id == doctor_id,
        models.Consultation.follow_up_required.is_(True),
        models.Consultation.follow_up_date >= today,
    ).order_by(models.Consultation.follow_up_date)).all()

    return {
        "generated_at": utcnow(),
        "cards": {
            "today_appointments": len(todays),
            "today_patients": len({a.patient_id for a in todays}),
            "upcoming_appointments": len(upcoming),
            "completed_today": _count(db, models.Appointment,
                                      models.Appointment.doctor_id == doctor_id,
                                      models.Appointment.appointment_date == today,
                                      models.Appointment.status == "COMPLETED"),
            "pending_follow_ups": len(follow_ups),
            "patients_seen_month": _count(
                db, models.Appointment,
                models.Appointment.doctor_id == doctor_id,
                models.Appointment.status == "COMPLETED",
                models.Appointment.appointment_date >= today.replace(day=1),
            ),
            "revenue_month": round(_money(
                db, models.Payment.amount, models.Payment.status == "PAID",
                models.Payment.invoice_id.in_(
                    select(models.Invoice.id).where(models.Invoice.doctor_id == doctor_id)
                ),
                func.date(models.Payment.paid_at) >= today.replace(day=1),
            ), 2),
            "rating": round(float(db.scalar(select(func.coalesce(func.avg(models.Review.rating), 0)).where(
                models.Review.doctor_id == doctor_id, models.Review.status == "APPROVED")) or 0), 2),
        },
        "today_schedule": [_appointment_row(a) for a in todays],
        "upcoming": [_appointment_row(a) for a in upcoming],
        "working_days": [{
            "day": slot_service.DAY_NAMES[s.day_of_week],
            "start": s.start_time.strftime("%H:%M"), "end": s.end_time.strftime("%H:%M"),
            "break": (f"{s.break_start.strftime('%H:%M')}-{s.break_end.strftime('%H:%M')}"
                      if s.break_start and s.break_end else None),
        } for s in schedule],
        "leaves": [{"type": l.leave_type, "from": l.start_date.isoformat(),
                    "to": (l.end_date or l.start_date).isoformat(), "reason": l.reason} for l in leaves],
        "follow_ups": [{
            "patient_id": c.patient_id,
            "patient_name": db.get(models.Patient, c.patient_id).full_name if db.get(models.Patient, c.patient_id) else None,
            "date": c.follow_up_date.isoformat() if c.follow_up_date else None,
            "notes": c.follow_up_notes,
        } for c in follow_ups],
    }


def _appointment_row(a: models.Appointment) -> dict:
    return {
        "id": a.id, "code": a.appointment_code, "patient_id": a.patient_id,
        "patient_name": a.patient.full_name if a.patient else None,
        "patient_code": a.patient.patient_code if a.patient else None,
        "phone": a.patient.phone if a.patient else None,
        "time": a.start_time.strftime("%H:%M"), "token": a.token_number,
        "status": a.status, "queue_status": a.queue_status, "source": a.source,
        "reason": a.reason, "date": a.appointment_date.isoformat(),
    }


def patient_dashboard(db: Session, patient: models.Patient) -> dict:
    from . import patients as patient_service

    profile = patient_service.full_profile(db, patient)
    notifications = db.scalars(
        select(models.Notification).where(models.Notification.patient_id == patient.id)
        .order_by(models.Notification.created_at.desc()).limit(10)
    ).all()
    return {
        "generated_at": utcnow(),
        "cards": profile["stats"],
        "upcoming": [_appointment_row(a) for a in profile["upcoming_appointments"]],
        "previous": [_appointment_row(a) for a in profile["previous_appointments"][:10]],
        "prescriptions": [{"id": p.id, "code": p.prescription_code,
                           "issued_at": p.issued_at.isoformat(),
                           "doctor": db.get(models.Doctor, p.doctor_id).full_name if p.doctor_id else None}
                          for p in profile["prescriptions"][:5]],
        "invoices": [{"id": i.id, "number": i.invoice_number, "total": float(i.total_amount),
                      "balance": float(i.balance_amount), "status": i.status} for i in profile["invoices"][:5]],
        "notifications": [{"id": n.id, "channel": n.channel, "message": n.message,
                           "status": n.status, "created_at": n.created_at.isoformat() if n.created_at else None}
                          for n in notifications],
    }


def reception_desk(db: Session) -> dict:
    today = date.today()
    queue = db.scalars(
        select(models.Appointment).where(
            models.Appointment.appointment_date == today,
            models.Appointment.status.in_(["PENDING", "CONFIRMED", "COMPLETED"]),
        ).order_by(models.Appointment.start_time)
    ).all()
    return {
        "generated_at": utcnow(),
        "cards": {
            "today_total": len(queue),
            "waiting": len([a for a in queue if a.queue_status == "WAITING"]),
            "checked_in": len([a for a in queue if a.queue_status == "CHECKED_IN"]),
            "in_consultation": len([a for a in queue if a.queue_status == "IN_CONSULTATION"]),
            "completed": len([a for a in queue if a.queue_status == "COMPLETED"]),
            "no_show": _count(db, models.Appointment, models.Appointment.appointment_date == today,
                              models.Appointment.status == "NO_SHOW"),
            "collected_today": round(_money(db, models.Payment.amount, models.Payment.status == "PAID",
                                            func.date(models.Payment.paid_at) == today), 2),
            "pending_payments": round(_money(db, models.Invoice.balance_amount,
                                             models.Invoice.status.in_(["UNPAID", "PARTIAL"])), 2),
        },
        "queue": [_appointment_row(a) | {
            "waiting_minutes": int((utcnow() - a.checked_in_at).total_seconds() // 60)
            if a.checked_in_at and a.queue_status in {"CHECKED_IN", "IN_CONSULTATION"} else None,
            "invoice_id": db.scalar(select(models.Invoice.id).where(models.Invoice.appointment_id == a.id)),
        } for a in queue],
        "doctor_availability_today": _doctors_today(db, today),
    }


def _doctors_today(db: Session, day: date) -> list[dict]:
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.is_active.is_(True))).all()
    out = []
    for d in doctors:
        schedules = db.scalars(select(models.DoctorSchedule).where(
            models.DoctorSchedule.doctor_id == d.id,
            models.DoctorSchedule.day_of_week == day.weekday(),
            models.DoctorSchedule.is_active.is_(True))).all()
        free = db.scalar(select(func.count(models.Slot.id)).where(
            models.Slot.doctor_id == d.id, models.Slot.slot_date == day,
            models.Slot.status == "AVAILABLE")) or 0
        leave = db.scalar(select(func.count(models.DoctorLeave.id)).where(
            models.DoctorLeave.doctor_id == d.id,
            models.DoctorLeave.start_date <= day, models.DoctorLeave.end_date >= day,
            models.DoctorLeave.status == "APPROVED")) or 0
        out.append({
            "doctor_id": d.id, "name": d.full_name,
            "specialty": d.specialty.name if d.specialty else None,
            "working": bool(schedules) and not leave,
            "hours": [f"{s.start_time.strftime('%H:%M')}-{s.end_time.strftime('%H:%M')}" for s in schedules],
            "on_leave": bool(leave), "free_slots_today": free,
        })
    return out


def ai_monitor(db: Session, days: int = 14, limit: int = 50) -> dict:
    start = datetime.combine(date.today() - timedelta(days=days), datetime.min.time())
    conversations = db.scalars(
        select(models.AIConversation).where(models.AIConversation.started_at >= start)
        .order_by(models.AIConversation.started_at.desc()).limit(limit)
    ).all()
    tool_stats = db.execute(
        select(models.AIToolCall.tool_name, models.AIToolCall.status, func.count(models.AIToolCall.id))
        .where(models.AIToolCall.created_at >= start)
        .group_by(models.AIToolCall.tool_name, models.AIToolCall.status)
    ).all()
    escalations = db.scalars(
        select(models.AIEscalation).order_by(models.AIEscalation.created_at.desc()).limit(limit)
    ).all()
    errors = db.scalars(
        select(models.AIToolCall).where(models.AIToolCall.status == "FAILED",
                                        models.AIToolCall.created_at >= start)
        .order_by(models.AIToolCall.created_at.desc()).limit(25)
    ).all()
    response_times = db.execute(
        select(func.date(models.AIMessage.created_at), func.coalesce(func.avg(models.AIMessage.latency_ms), 0))
        .where(models.AIMessage.latency_ms.is_not(None), models.AIMessage.created_at >= start)
        .group_by(func.date(models.AIMessage.created_at))
        .order_by(func.date(models.AIMessage.created_at))
    ).all()

    return {
        "generated_at": utcnow(),
        "cards": {
            "conversations": len(conversations),
            "active": len([c for c in conversations if c.status == "ACTIVE"]),
            "escalated": len([c for c in conversations if c.status == "ESCALATED"]),
            "booking_attempts": sum(c.booking_attempts or 0 for c in conversations),
            "successful_bookings": sum(c.successful_bookings or 0 for c in conversations),
            "failed_bookings": sum(c.failed_bookings or 0 for c in conversations),
            "escalation_count": sum(c.escalation_count or 0 for c in conversations),
            "tool_calls": sum(c.tool_call_count or 0 for c in conversations),
            "errors": sum(c.error_count or 0 for c in conversations),
            "avg_response_ms": int(sum(c.avg_response_ms or 0 for c in conversations) / len(conversations))
            if conversations else 0,
        },
        "conversations": [{
            "id": c.id, "session_id": c.session_id, "patient": c.patient.full_name if c.patient else "Anonymous",
            "channel": c.channel, "status": c.status, "stage": c.stage, "intent": c.primary_intent,
            "messages": c.message_count, "tools": c.tool_call_count,
            "bookings": c.successful_bookings, "failed": c.failed_bookings,
            "escalations": c.escalation_count, "avg_response_ms": c.avg_response_ms,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "concern": c.concern_summary,
        } for c in conversations],
        "tool_usage": [{"tool": t, "status": s, "count": c} for t, s, c in tool_stats],
        "escalations": [{
            "id": e.id, "level": e.level, "reason": e.reason, "status": e.status,
            "conversation_id": e.conversation_id, "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in escalations],
        "errors": [{"tool": e.tool_name, "error": e.error, "at": e.created_at.isoformat()} for e in errors],
        "response_time_trend": [{"date": str(d), "avg_ms": int(v)} for d, v in response_times],
    }
