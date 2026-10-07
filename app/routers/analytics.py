"""Dashboards + analytics: admin, doctor, patient, reception, AI, growth and trends."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import CurrentUser, current_patient, get_current_user, patient_for_user, require_permission
from ..services import analytics

router = APIRouter(prefix="/analytics", tags=["Analytics & Dashboards"])
admin_router = APIRouter(prefix="/admin", tags=["Admin & Jobs"])


# ==========================================================================
# dashboards
# ==========================================================================
@admin_router.get("/dashboard", summary="Admin dashboard (cards + charts)")
def admin_dashboard(db: Session = Depends(get_db), days: int = 30,
                    _: CurrentUser = Depends(require_permission("dashboard:admin"))):
    return analytics.admin_dashboard(db, days=days)


@admin_router.get("/dashboard/doctor", summary="Doctor dashboard")
def doctor_dashboard(db: Session = Depends(get_db), doctor_id: int | None = None,
                     user: CurrentUser = Depends(require_permission("dashboard:doctor"))):
    if not doctor_id:
        doctor = db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))
        if not doctor:
            raise HTTPException(status_code=400, detail="Pass doctor_id (this login has no doctor profile)")
        doctor_id = doctor.id
    return analytics.doctor_dashboard(db, doctor_id)


@admin_router.get("/dashboard/patient", summary="Patient dashboard")
def patient_dashboard(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    patient = patient_for_user(db, user.id)
    if not patient:
        raise HTTPException(status_code=404, detail="No patient profile linked to this login")
    return analytics.patient_dashboard(db, patient)


@admin_router.get("/dashboard/reception", summary="Reception desk dashboard")
def reception(db: Session = Depends(get_db),
              _: CurrentUser = Depends(require_permission("dashboard:reception"))):
    return analytics.reception_desk(db)


# ==========================================================================
# analytics
# ==========================================================================
@router.get("/overview", summary="Analytics overview")
def overview(db: Session = Depends(get_db), days: int = 30,
             _: CurrentUser = Depends(require_permission("analytics:read"))):
    return analytics.admin_dashboard(db, days=days)


@router.get("/patient-growth", summary="New + cumulative patients")
def patient_growth(db: Session = Depends(get_db), days: int = 30,
                   _: CurrentUser = Depends(require_permission("analytics:read"))):
    today = date.today()
    return analytics._patient_growth(db, today - timedelta(days=days), today)


@router.get("/appointments/trends", summary="Appointment trends + status mix")
def appointment_trends(db: Session = Depends(get_db), days: int = 30,
                       _: CurrentUser = Depends(require_permission("analytics:read"))):
    today = date.today()
    start = today - timedelta(days=days)
    return {
        "trend": analytics._appointment_trend(db, start, today),
        "status_breakdown": analytics._status_breakdown(db, start),
        "cancellation_rate": _rate(db, start, "CANCELLED"),
        "no_show_rate": _rate(db, start, "NO_SHOW"),
    }


def _rate(db: Session, start: date, status_value: str) -> float:
    total = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.appointment_date >= start)) or 0
    count = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.appointment_date >= start,
        models.Appointment.status == status_value)) or 0
    return round((count / total) * 100, 1) if total else 0


@router.get("/revenue", summary="Revenue trend + method split")
def revenue(db: Session = Depends(get_db), days: int = 30,
            _: CurrentUser = Depends(require_permission("analytics:read"))):
    today = date.today()
    start = today - timedelta(days=days)
    by_method = db.execute(
        select(models.Payment.method, func.coalesce(func.sum(models.Payment.amount), 0))
        .where(models.Payment.status == "PAID", func.date(models.Payment.paid_at) >= start)
        .group_by(models.Payment.method)
    ).all()
    return {
        "trend": analytics._revenue_trend(db, start, today),
        "by_method": [{"method": m, "amount": float(a)} for m, a in by_method],
        "pending": float(db.scalar(select(func.coalesce(func.sum(models.Invoice.balance_amount), 0)).where(
            models.Invoice.status.in_(["UNPAID", "PARTIAL"]))) or 0),
        "refunds": float(db.scalar(select(func.coalesce(func.sum(models.Refund.amount), 0)).where(
            func.date(models.Refund.created_at) >= start)) or 0),
    }


@router.get("/specialty-demand", summary="Popular specialties by appointments")
def specialty_demand(db: Session = Depends(get_db), days: int = 30,
                     _: CurrentUser = Depends(require_permission("analytics:read"))):
    today = date.today()
    demand = analytics.popular_specialties(db, today - timedelta(days=days))
    for row in demand:
        specialty = db.scalar(select(models.Specialty).where(models.Specialty.name == row["specialty"]))
        row["doctors"] = db.scalar(select(func.count(models.Doctor.id)).where(
            models.Doctor.specialty_id == specialty.id)) if specialty else 0
        row["concerns"] = [c.keyword for c in db.scalars(select(models.SpecialtyConcern).where(
            models.SpecialtyConcern.specialty_id == specialty.id).limit(6))] if specialty else []
    return demand


@router.get("/doctor-utilisation", summary="Doctor / slot utilisation")
def doctor_utilisation(db: Session = Depends(get_db),
                       _: CurrentUser = Depends(require_permission("analytics:read"))):
    from ..services import slots as slot_service

    return slot_service.slot_utilisation(db)


@router.get("/ai", summary="AI analytics: booking rate, escalations, response time")
def ai_analytics(db: Session = Depends(get_db), days: int = 14,
                 _: CurrentUser = Depends(require_permission("ai:admin"))):
    monitor = analytics.ai_monitor(db, days=days)
    return {"cards": monitor["cards"], "intent_breakdown": analytics.ai_intent_breakdown(
        db, date.today() - timedelta(days=days)), "response_time_trend": monitor["response_time_trend"]}


@router.get("/whatsapp", summary="WhatsApp delivery analytics")
def whatsapp_analytics(db: Session = Depends(get_db), days: int = 30,
                       _: CurrentUser = Depends(require_permission("analytics:read"))):
    start = date.today() - timedelta(days=days)
    rows = db.execute(
        select(models.WhatsappMessage.status, func.count(models.WhatsappMessage.id))
        .where(func.date(models.WhatsappMessage.created_at) >= start)
        .group_by(models.WhatsappMessage.status)
    ).all()
    total = sum(c for _, c in rows)
    sent = sum(c for s, c in rows if s in {"SENT", "DELIVERED", "READ"})
    return {
        "window_days": days, "total": total,
        "delivery_rate": round((sent / total) * 100, 1) if total else 0,
        "breakdown": [{"status": s, "count": c} for s, c in rows],
    }


@router.get("/summary/csv", summary="Flat KPI export (CSV)")
def kpi_csv(db: Session = Depends(get_db), days: int = 30,
            _: CurrentUser = Depends(require_permission("analytics:read"))):
    from fastapi.responses import PlainTextResponse

    data = analytics.admin_dashboard(db, days=days)["cards"]
    lines = ["metric,value"] + [f"{k},{v}" for k, v in data.items()]
    return PlainTextResponse("\n".join(lines), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=vvh_kpis.csv"})
