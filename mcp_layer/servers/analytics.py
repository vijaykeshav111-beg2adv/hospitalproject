"""MCP server 08 - Analytics & Reports Server  [ADMIN tier].

Grounded management reports: revenue, appointments, doctor load, patient
growth, AI monitor rollups and the daily dashboard KPI block.  Uses the
reporting views where they exist and falls back to live aggregates.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, text

from app import models
from app.security import utcnow

from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["analytics"]
TOOLS: dict = {}


def _days(n: int) -> tuple[str, str]:
    end = utcnow().date()
    start = end - timedelta(days=max(1, min(n, 365)))
    return str(start), str(end)


@tool("revenue_report", "Revenue summary for the last N days (paid amounts + invoices issued).")
def _revenue_report(db, days: int = 30):
    start, end = _days(days)
    paid = db.scalar(
        select(func.coalesce(func.sum(models.Payment.amount), 0)).where(
            models.Payment.status == "PAID",
            models.Payment.paid_at >= start,
            models.Payment.paid_at <= end + " 23:59:59",
        )
    ) or 0
    invoiced = db.scalar(
        select(func.coalesce(func.sum(models.Invoice.total_amount), 0)).where(
            models.Invoice.issued_at >= start,
            models.Invoice.issued_at <= end + " 23:59:59",
        )
    ) or 0
    outstanding = db.scalar(
        select(func.coalesce(func.sum(models.Invoice.balance_amount), 0)).where(
            models.Invoice.status.in_(("UNPAID", "PARTIAL")))
    ) or 0
    by_method = {row[0]: float(row[1] or 0) for row in db.execute(
        select(models.Payment.method, func.sum(models.Payment.amount))
        .where(models.Payment.status == "PAID", models.Payment.paid_at >= start)
        .group_by(models.Payment.method)
    ).all()}
    return {
        "window": {"from": start, "to": end},
        "paid_total": float(paid),
        "invoiced_total": float(invoiced),
        "outstanding_total": float(outstanding),
        "by_method": by_method,
    }


@tool("appointments_report", "Appointment volume and status mix for the last N days.")
def _appointments_report(db, days: int = 30):
    start, end = _days(days)
    rows = db.execute(
        select(models.Appointment.status, func.count(models.Appointment.id))
        .where(models.Appointment.appointment_date >= start,
               models.Appointment.appointment_date <= end)
        .group_by(models.Appointment.status)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="appointments_report", what="appointment")
    by_day = {str(row[0]): int(row[1]) for row in db.execute(
        select(models.Appointment.appointment_date, func.count(models.Appointment.id))
        .where(models.Appointment.appointment_date >= start,
               models.Appointment.appointment_date <= end)
        .group_by(models.Appointment.appointment_date)
        .order_by(models.Appointment.appointment_date)
    ).all()}
    return {"window": {"from": start, "to": end},
            "by_status": {str(r[0]): int(r[1]) for r in rows},
            "by_day": by_day,
            "total": sum(int(r[1]) for r in rows)}


@tool("doctor_load", "Appointments per doctor over the last N days.")
def _doctor_load(db, days: int = 30, limit: int = 20):
    start, end = _days(days)
    limit = max(1, min(int(limit), 100))
    rows = db.execute(
        select(models.Doctor.full_name, models.Doctor.id,
               func.count(models.Appointment.id).label("appts"))
        .join(models.Appointment, models.Appointment.doctor_id == models.Doctor.id)
        .where(models.Appointment.appointment_date >= start,
               models.Appointment.appointment_date <= end)
        .group_by(models.Doctor.id, models.Doctor.full_name)
        .order_by(func.count(models.Appointment.id).desc())
        .limit(limit)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="doctor_load", what="appointment")
    return [{"doctor": r[0], "doctor_id": r[1], "appointments": int(r[2])} for r in rows]


@tool("patient_growth", "New patient registrations per month.")
def _patient_growth(db, months: int = 6):
    months = max(1, min(int(months), 24))
    rows = db.execute(text("""
        SELECT DATE_FORMAT(created_at, '%Y-%m') AS ym, COUNT(*) AS n
        FROM patients
        WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL :m MONTH)
        GROUP BY ym ORDER BY ym
    """), {"m": months}).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="patient_growth", what="patient")
    return {str(r[0]): int(r[1]) for r in rows}


@tool("ai_monitor_summary", "AI front-desk rollups: conversations, tool calls, failures, escalations.")
def _ai_monitor_summary(db, days: int = 7):
    start, end = _days(days)
    conv = db.scalar(
        select(func.count(models.AIConversation.id)).where(models.AIConversation.started_at >= start)
    ) or 0
    tools_failed = db.scalar(
        select(func.count(models.AIToolCall.id)).where(models.AIToolCall.status != "SUCCESS")
    ) or 0
    tools_total = db.scalar(select(func.count(models.AIToolCall.id))) or 0
    escalations = db.scalar(
        select(func.count(models.AIEscalation.id)).where(models.AIEscalation.created_at >= start)
    ) or 0
    return {"window": {"from": start, "to": end},
            "conversations": int(conv),
            "tool_calls_total": int(tools_total),
            "tool_calls_failed": int(tools_failed),
            "escalations": int(escalations)}


@tool("daily_dashboard", "One KPI block for the admin dashboard (today).")
def _daily_dashboard(db):
    today = str(utcnow().date())
    appts_today = db.scalar(
        select(func.count(models.Appointment.id)).where(models.Appointment.appointment_date == today)
    ) or 0
    waiting = db.scalar(
        select(func.count(models.Appointment.id)).where(
            models.Appointment.appointment_date == today,
            models.Appointment.queue_status == "WAITING")
    ) or 0
    collected_today = db.scalar(
        select(func.coalesce(func.sum(models.Payment.amount), 0)).where(
            models.Payment.status == "PAID", models.Payment.paid_at >= today)
    ) or 0
    patients_total = db.scalar(select(func.count(models.Patient.id))) or 0
    doctors_active = db.scalar(
        select(func.count(models.Doctor.id)).where(models.Doctor.is_active.is_(True))) or 0
    return {
        "date": today,
        "appointments_today": int(appts_today),
        "waiting_now": int(waiting),
        "collected_today": float(collected_today),
        "patients_total": int(patients_total),
        "doctors_active": int(doctors_active),
    }


mcp = make_server(
    SPEC,
    "Analytics & Reports Server (ADMIN tier). Reports are computed from "
    "stored rows only - empty windows return a grounded 'no', not zeros "
    "invented as facts.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
