"""Doctor system: profile, specialty, qualifications, fees, schedule, leave, statistics."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, get_optional_user, require_permission
from ..security import hash_password
from ..services import patients as patient_service, slots as slot_service

router = APIRouter(prefix="/doctors", tags=["Doctor System"])


def _out(d: models.Doctor) -> dict:
    return {
        "id": d.id, "doctor_code": d.doctor_code, "user_id": d.user_id, "full_name": d.full_name,
        "specialty_id": d.specialty_id, "specialty_name": d.specialty.name if d.specialty else None,
        "email": d.email, "phone": d.phone, "qualifications": d.qualifications,
        "experience_years": d.experience_years, "registration_no": d.registration_no, "bio": d.bio,
        "languages": d.languages, "consultation_fee": float(d.consultation_fee or 0),
        "follow_up_fee": float(d.follow_up_fee or 0),
        "slot_duration_minutes": d.slot_duration_minutes, "daily_capacity": d.daily_capacity,
        "room_number": d.room_number, "is_available_for_ai_booking": d.is_available_for_ai_booking,
        "is_active": d.is_active, "rating_avg": float(d.rating_avg or 0), "rating_count": d.rating_count or 0,
    }


@router.get("", response_model=list[schemas.DoctorOut], summary="List / search doctors")
def list_doctors(db: Session = Depends(get_db), specialty_id: int | None = None, search: str | None = None,
                 active_only: bool = True, ai_bookable_only: bool = False, limit: int = Query(100, le=300),
                 _: CurrentUser | None = Depends(get_optional_user)):
    stmt = select(models.Doctor)
    if specialty_id:
        stmt = stmt.where(models.Doctor.specialty_id == specialty_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(models.Doctor.full_name.like(like), models.Doctor.qualifications.like(like),
                              models.Doctor.doctor_code.like(like)))
    if active_only:
        stmt = stmt.where(models.Doctor.is_active.is_(True))
    if ai_bookable_only:
        stmt = stmt.where(models.Doctor.is_available_for_ai_booking.is_(True))
    return [_out(d) for d in db.scalars(stmt.order_by(models.Doctor.full_name).limit(limit))]


@router.post("", response_model=schemas.DoctorOut, status_code=201, summary="Create a doctor")
def create_doctor(payload: schemas.DoctorCreate, request: Request, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_permission("doctors:write"))):
    if not db.get(models.Specialty, payload.specialty_id):
        raise HTTPException(status_code=400, detail="Specialty not found")
    data = payload.model_dump(exclude={"create_login", "password"})
    doctor = models.Doctor(doctor_code=patient_service.next_doctor_code(db), **data)
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    if payload.create_login and payload.email:
        role = db.scalar(select(models.Role).where(models.Role.name == "DOCTOR"))
        existing = db.scalar(select(models.User).where(models.User.email == payload.email.lower()))
        if not existing:
            portal = models.User(
                uuid=uuid.uuid4().hex, full_name=payload.full_name, email=payload.email.lower(),
                phone=payload.phone, password_hash=hash_password(payload.password or "Doctor@123"),
                role_id=role.id, is_active=True, is_verified=True, must_change_password=True,
            )
            db.add(portal)
            db.commit()
            db.refresh(portal)
            doctor.user_id = portal.id
            db.commit()
            db.refresh(doctor)

    audit(db, action="DOCTOR_CREATE", resource="doctor", resource_id=doctor.id, user=user, request=request,
          new_value=_out(doctor))
    return _out(doctor)


@router.get("/me/profile", summary="Doctor dashboard profile (own)")
def my_doctor_profile(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    doctor = db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))
    if not doctor and user.role == "DOCTOR":
        raise HTTPException(status_code=404, detail="No doctor profile linked to this login")
    if not doctor:
        raise HTTPException(status_code=400, detail="Not a doctor account")
    from ..services import analytics

    return {"profile": _out(doctor), "dashboard": analytics.doctor_dashboard(db, doctor.id)}


@router.get("/{doctor_id}", response_model=schemas.DoctorOut, summary="Doctor profile")
def doctor_detail(doctor_id: int, db: Session = Depends(get_db),
                  _: CurrentUser | None = Depends(get_optional_user)):
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    return _out(doctor)


@router.patch("/{doctor_id}", response_model=schemas.DoctorOut, summary="Update doctor")
def update_doctor(doctor_id: int, payload: schemas.DoctorUpdate, request: Request,
                  db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_permission("doctors:write"))):
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    before = _out(doctor)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    db.commit()
    db.refresh(doctor)
    audit(db, action="DOCTOR_UPDATE", resource="doctor", resource_id=doctor_id, user=user, request=request,
          previous_value=before, new_value=_out(doctor))
    return _out(doctor)


@router.get("/{doctor_id}/statistics", summary="Doctor statistics")
def doctor_statistics(doctor_id: int, db: Session = Depends(get_db), days: int = 30,
                      _: CurrentUser | None = Depends(get_optional_user)):
    doctor = db.get(models.Doctor, doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")
    start = date.today() - timedelta(days=days)
    total = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.doctor_id == doctor_id, models.Appointment.appointment_date >= start)) or 0
    completed = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.doctor_id == doctor_id, models.Appointment.appointment_date >= start,
        models.Appointment.status == "COMPLETED")) or 0
    cancelled = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.doctor_id == doctor_id, models.Appointment.appointment_date >= start,
        models.Appointment.status == "CANCELLED")) or 0
    no_show = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.doctor_id == doctor_id, models.Appointment.appointment_date >= start,
        models.Appointment.status == "NO_SHOW")) or 0
    revenue = float(db.scalar(select(func.coalesce(func.sum(models.Payment.amount), 0)).where(
        models.Payment.status == "PAID",
        models.Payment.invoice_id.in_(select(models.Invoice.id).where(models.Invoice.doctor_id == doctor_id)),
        func.date(models.Payment.paid_at) >= start)) or 0)
    rating = float(db.scalar(select(func.coalesce(func.avg(models.Review.rating), 0)).where(
        models.Review.doctor_id == doctor_id, models.Review.status == "APPROVED")) or 0)
    utilisation = next((r for r in slot_service.slot_utilisation(db) if r["doctor_id"] == doctor_id), None)
    return {
        "doctor_id": doctor_id, "doctor_name": doctor.full_name, "window_days": days,
        "appointments": total, "completed": completed, "cancelled": cancelled, "no_shows": no_show,
        "completion_rate": round((completed / total) * 100, 1) if total else 0,
        "revenue": round(revenue, 2), "average_rating": round(rating, 2),
        "slot_utilisation": utilisation or {},
        "unique_patients": db.scalar(select(func.count(func.distinct(models.Appointment.patient_id))).where(
            models.Appointment.doctor_id == doctor_id, models.Appointment.appointment_date >= start)) or 0,
    }


@router.get("/{doctor_id}/availability", summary="Working days, hours, leave, holidays + open slots")
def doctor_availability(doctor_id: int, db: Session = Depends(get_db), days_ahead: int = 14,
                        _: CurrentUser | None = Depends(get_optional_user)):
    from ..services.ai_tools import ToolContext, get_available_slots, get_doctor_availability

    ctx = ToolContext(db)
    availability = get_doctor_availability(db, ctx, doctor_id=doctor_id, days_ahead=days_ahead)
    availability["slots"] = get_available_slots(db, ctx, doctor_id=doctor_id, days_ahead=days_ahead, limit=50)["slots"]
    return availability
