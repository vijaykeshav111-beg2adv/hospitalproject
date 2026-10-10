"""Background worker jobs (APScheduler).

Runs inside the FastAPI process by default - see app/main.py. Each job records a
job_runs row so the admin dashboard can show job health.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import and_, func, or_, select

from .. import models
from ..config import settings
from ..database import SessionLocal
from ..security import utcnow
from . import notify, slots

log = logging.getLogger("vvh.jobs")


def _run(db, name: str, fn):
    run = models.JobRun(job_name=name, status="RUNNING")
    db.add(run)
    db.commit()
    db.refresh(run)
    started = utcnow()
    try:
        details = fn(db)
        run.status = "SUCCESS"
        run.details = details if isinstance(details, (dict, list)) else {"result": details}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run.status = "FAILED"
        run.error = str(exc)[:500]
        log.exception("Job %s failed", name)
    run.finished_at = utcnow()
    run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
    db.commit()
    return run


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def job_generate_tomorrow_slots(db) -> dict:
    """Generate slots for tomorrow (and top-up the rolling window)."""
    result = slots.generate_all_slots(db, start_date=date.today() + timedelta(days=1), days_ahead=2)
    return result


def job_expire_old_slots(db) -> dict:
    released = slots.expire_holds(db)
    expired = slots.expire_past_slots(db)
    stale = db.scalars(
        select(models.Notification).where(
            models.Notification.status == "PENDING",
            models.Notification.scheduled_at < utcnow() - timedelta(days=7),
        )
    ).all()
    cancelled = 0
    for notif in stale:
        notif.status = "FAILED"
        notif.error = "expired before dispatch"
        cancelled += 1
    db.commit()
    return {"holds_released": released, "slots_expired": expired, "notifications_expired": cancelled}


def job_appointment_reminders(db) -> dict:
    """Reminder ~24h before the appointment."""
    target = date.today() + timedelta(days=1)
    appts = db.scalars(
        select(models.Appointment).where(
            models.Appointment.appointment_date == target,
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
            models.Appointment.reminder_sent_at.is_(None),
        )
    ).all()
    sent = 0
    for appt in appts:
        context = _appointment_context(appt)
        notify.notify_patient(db, appt.patient, "APPOINTMENT_REMINDER", context,
                              channels=["WHATSAPP", "EMAIL", "IN_APP"], appointment_id=appt.id)
        appt.reminder_sent_at = utcnow()
        sent += 1
    db.commit()
    return {"date": target.isoformat(), "reminders_sent": sent}


def job_follow_up_reminders(db) -> dict:
    """Follow-ups falling due in the next 2 days."""
    today = date.today()
    consultations = db.scalars(
        select(models.Consultation).where(
            models.Consultation.follow_up_required.is_(True),
            models.Consultation.follow_up_date.is_not(None),
            models.Consultation.follow_up_date >= today,
            models.Consultation.follow_up_date <= today + timedelta(days=2),
        )
    ).all()
    sent = 0
    for c in consultations:
        patient = db.get(models.Patient, c.patient_id)
        doctor = db.get(models.Doctor, c.doctor_id)
        if not patient:
            continue
        notify.notify_patient(db, patient, "FOLLOW_UP_REMINDER", {
            "patient_name": patient.full_name,
            "doctor_name": doctor.full_name if doctor else "your doctor",
            "date": c.follow_up_date.strftime("%d %b %Y"),
        }, channels=["WHATSAPP", "EMAIL", "IN_APP"])
        sent += 1
    return {"follow_ups": sent}


def job_payment_reminders(db) -> dict:
    unpaid = db.scalars(
        select(models.Invoice).where(
            models.Invoice.status.in_(["UNPAID", "PARTIAL"]),
            models.Invoice.balance_amount > 0,
            or_(
                models.Invoice.due_date.is_(None),
                models.Invoice.due_date <= date.today() + timedelta(days=1),
            ),
        ).limit(100)
    ).all()
    sent = 0
    for inv in unpaid:
        patient = db.get(models.Patient, inv.patient_id)
        if not patient:
            continue
        notify.notify_patient(db, patient, "PAYMENT_REMINDER", {
            "patient_name": patient.full_name,
            "amount": f"{float(inv.balance_amount):.2f}",
            "invoice": inv.invoice_number,
        }, channels=["WHATSAPP"])
        sent += 1
    return {"payment_reminders": sent}


def job_doctor_daily_digest(db) -> dict:
    target = date.today() + timedelta(days=1)
    doctors = db.scalars(select(models.Doctor).where(models.Doctor.is_active.is_(True))).all()
    sent = 0
    for doctor in doctors:
        appts = db.scalars(
            select(models.Appointment).where(
                models.Appointment.doctor_id == doctor.id,
                models.Appointment.appointment_date == target,
                models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
            ).order_by(models.Appointment.start_time)
        ).all()
        if not appts or not doctor.phone:
            continue
        notify.send_whatsapp(
            db, to_number=doctor.phone, template="DOCTOR_NOTIFICATION",
            body=notify.render("DOCTOR_NOTIFICATION", {
                "doctor_name": doctor.full_name,
                "count": len(appts),
                "time": appts[0].start_time.strftime("%H:%M"),
            }),
        )
        sent += 1
    return {"doctor_digests": sent}


def job_review_requests(db) -> dict:
    """Ask for a review the day after a completed visit."""
    yesterday = date.today() - timedelta(days=1)
    appts = db.scalars(
        select(models.Appointment).where(
            models.Appointment.appointment_date == yesterday,
            models.Appointment.status == "COMPLETED",
        )
    ).all()
    sent = 0
    for appt in appts:
        already = db.scalar(select(func.count(models.Review.id)).where(models.Review.appointment_id == appt.id))
        if already:
            continue
        notify.notify_patient(db, appt.patient, "REVIEW_REQUEST", {
            "patient_name": appt.patient.full_name,
            "doctor_name": appt.doctor.full_name if appt.doctor else "our team",
            "link": "/ui/reviews",
        }, channels=["WHATSAPP"])
        db.add(models.Review(
            patient_id=appt.patient_id, doctor_id=appt.doctor_id, appointment_id=appt.id,
            review_type="DOCTOR", rating=0, status="REQUESTED", requested_at=utcnow(),
        ))
        sent += 1
    db.commit()
    return {"review_requests": sent}


def job_cleanup_temp_data(db) -> dict:
    """Purge expired holds, reset stale counters and drop old job runs."""
    from ..config import settings as cfg
    import os
    from pathlib import Path

    deleted_temp = 0
    temp_dir = cfg.storage_dir / "tmp"
    if temp_dir.exists():
        for f in temp_dir.iterdir():
            if f.is_file() and (utcnow() - __import__("datetime").datetime.utcfromtimestamp(f.stat().st_mtime)).days > 1:
                f.unlink()
                deleted_temp += 1

    old_runs = db.scalars(
        select(models.JobRun).where(models.JobRun.started_at < utcnow() - timedelta(days=30))
    ).all()
    for r in old_runs:
        db.delete(r)
    expired_tokens = db.scalars(
        select(models.PasswordResetToken).where(models.PasswordResetToken.expires_at < utcnow() - timedelta(days=7))
    ).all()
    for t in expired_tokens:
        db.delete(t)
    db.commit()
    return {"temp_files_deleted": deleted_temp, "job_runs_pruned": len(old_runs),
            "reset_tokens_pruned": len(expired_tokens)}


def job_close_stale_sessions(db) -> dict:
    sessions = db.scalars(
        select(models.UserSession).where(
            models.UserSession.is_revoked.is_(False),
            models.UserSession.expires_at < utcnow(),
        )
    ).all()
    for s in sessions:
        s.is_revoked = True
        s.revoked_at = utcnow()
        s.revoked_reason = "expired"
    db.commit()
    return {"sessions_closed": len(sessions)}


def job_noshow_sweep(db) -> dict:
    """Mark yesterday's unchecked appointments as NO_SHOW."""
    target = date.today() - timedelta(days=1)
    appts = db.scalars(
        select(models.Appointment).where(
            models.Appointment.appointment_date == target,
            models.Appointment.status.in_(["PENDING", "CONFIRMED"]),
        )
    ).all()
    for appt in appts:
        appt.status = "NO_SHOW"
        appt.queue_status = "NO_SHOW"
    db.commit()
    return {"no_shows": len(appts)}


def _appointment_context(appt: models.Appointment) -> dict:
    return {
        "patient_name": appt.patient.full_name if appt.patient else "Patient",
        "doctor_name": appt.doctor.full_name if appt.doctor else "Doctor",
        "specialty": appt.specialty.name if appt.specialty else "",
        "date": appt.appointment_date.strftime("%d %b %Y"),
        "time": appt.start_time.strftime("%H:%M"),
        "token": appt.token_number or "-",
        "fee": f"{float(appt.doctor.consultation_fee or 0):.2f}" if appt.doctor else "0",
        "code": appt.appointment_code,
        "reason": appt.cancellation_reason or "",
    }


# --------------------------------------------------------------------------
# registry + scheduler
# --------------------------------------------------------------------------
JOBS = {
    "generate_tomorrow_slots": (job_generate_tomorrow_slots, "cron", {"hour": 23, "minute": 30}),
    "expire_old_slots": (job_expire_old_slots, "interval", {"minutes": 5}),
    "appointment_reminders": (job_appointment_reminders, "cron", {"hour": 18, "minute": 0}),
    "follow_up_reminders": (job_follow_up_reminders, "cron", {"hour": 9, "minute": 0}),
    "payment_reminders": (job_payment_reminders, "cron", {"hour": 11, "minute": 0}),
    "doctor_daily_digest": (job_doctor_daily_digest, "cron", {"hour": 20, "minute": 0}),
    "review_requests": (job_review_requests, "cron", {"hour": 10, "minute": 30}),
    "cleanup_temp_data": (job_cleanup_temp_data, "cron", {"hour": 2, "minute": 0}),
    "close_stale_sessions": (job_close_stale_sessions, "interval", {"minutes": 30}),
    "noshow_sweep": (job_noshow_sweep, "cron", {"hour": 1, "minute": 0}),
    "whatsapp_retry": (lambda db: {"retried": notify.retry_failed_whatsapp(db)}, "interval", {"minutes": 10}),
    "notification_retry": (lambda db: {"retried": notify.retry_failed_notifications(db)}, "interval", {"minutes": 15}),
}

_scheduler: BackgroundScheduler | None = None


def run_job_by_name(name: str) -> dict:
    if name not in JOBS:
        raise KeyError(f"Unknown job '{name}'. Available: {sorted(JOBS)}")
    fn, _, _ = JOBS[name]
    db = SessionLocal()
    try:
        run = _run(db, name, fn)
        return {
            "job": name, "status": run.status, "duration_ms": run.duration_ms,
            "details": run.details, "error": run.error,
        }
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.enable_scheduler:
        log.info("Scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    if _scheduler:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    def wrapper(fn, name):
        def _job():
            db = SessionLocal()
            try:
                _run(db, name, fn)
            finally:
                db.close()
        return _job

    for name, (fn, kind, kwargs) in JOBS.items():
        if kind == "cron":
            _scheduler.add_job(wrapper(fn, name), "cron", id=name, replace_existing=True, **kwargs)
        else:
            _scheduler.add_job(wrapper(fn, name), "interval", id=name, replace_existing=True, **kwargs)
    _scheduler.start()
    log.info("Scheduler started with %d jobs", len(JOBS))
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
