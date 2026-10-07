"""Specialty master: description, doctors, concern mapping, availability, demand."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, get_optional_user, require_permission
from ..services import slots as slot_service

router = APIRouter(prefix="/specialties", tags=["Specialty"])


def _out(db: Session, sp: models.Specialty) -> dict:
    doctors = db.scalar(select(func.count(models.Doctor.id)).where(
        models.Doctor.specialty_id == sp.id, models.Doctor.is_active.is_(True))) or 0
    demand = db.scalar(select(func.count(models.Appointment.id)).where(
        models.Appointment.specialty_id == sp.id,
        models.Appointment.appointment_date >= date.today() - timedelta(days=30))) or 0
    concerns = list(db.scalars(select(models.SpecialtyConcern).where(
        models.SpecialtyConcern.specialty_id == sp.id)))
    return {
        "id": sp.id, "code": sp.code, "name": sp.name, "description": sp.description,
        "consultation_fee": float(sp.consultation_fee or 0), "is_active": sp.is_active,
        "doctor_count": doctors, "appointment_demand": demand,
        "concerns": [c.keyword for c in concerns],
    }


@router.get("", response_model=list[schemas.SpecialtyOut], summary="List specialties")
def list_specialties(db: Session = Depends(get_db), active_only: bool = True, search: str | None = None,
                     _: CurrentUser | None = Depends(get_optional_user)):
    stmt = select(models.Specialty)
    if active_only:
        stmt = stmt.where(models.Specialty.is_active.is_(True))
    if search:
        stmt = stmt.where(models.Specialty.name.like(f"%{search}%"))
    return [_out(db, sp) for sp in db.scalars(stmt.order_by(models.Specialty.name))]


@router.post("", response_model=schemas.SpecialtyOut, status_code=201, summary="Create specialty")
def create_specialty(payload: schemas.SpecialtyCreate, request: Request, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_permission("specialties:write"))):
    if db.scalar(select(models.Specialty).where(models.Specialty.name == payload.name)):
        raise HTTPException(status_code=409, detail="Specialty name already exists")
    data = payload.model_dump(exclude={"concerns"})
    specialty = models.Specialty(**data)
    db.add(specialty)
    db.commit()
    db.refresh(specialty)
    for keyword in payload.concerns:
        db.add(models.SpecialtyConcern(specialty_id=specialty.id, keyword=keyword.strip().lower()))
    db.commit()
    audit(db, action="SPECIALTY_CREATE", resource="specialty", resource_id=specialty.id, user=user,
          request=request, new_value={"name": specialty.name, "concerns": payload.concerns})
    return _out(db, specialty)


@router.get("/{specialty_id}", response_model=schemas.SpecialtyOut, summary="Specialty detail")
def specialty_detail(specialty_id: int, db: Session = Depends(get_db),
                     _: CurrentUser | None = Depends(get_optional_user)):
    specialty = db.get(models.Specialty, specialty_id)
    if not specialty:
        raise HTTPException(status_code=404, detail="Specialty not found")
    return _out(db, specialty)


@router.patch("/{specialty_id}", response_model=schemas.SpecialtyOut, summary="Update specialty")
def update_specialty(specialty_id: int, payload: schemas.SpecialtyCreate, request: Request,
                     db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_permission("specialties:write"))):
    specialty = db.get(models.Specialty, specialty_id)
    if not specialty:
        raise HTTPException(status_code=404, detail="Specialty not found")
    for field, value in payload.model_dump(exclude={"concerns"}).items():
        setattr(specialty, field, value)
    db.commit()
    audit(db, action="SPECIALTY_UPDATE", resource="specialty", resource_id=specialty_id, user=user,
          request=request, new_value=payload.model_dump())
    return _out(db, specialty)


@router.post("/{specialty_id}/concerns", summary="Add symptom / concern keyword mapping")
def add_concern(specialty_id: int, payload: schemas.SpecialtyConcernCreate, request: Request,
                db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("specialties:write"))):
    if not db.get(models.Specialty, specialty_id):
        raise HTTPException(status_code=404, detail="Specialty not found")
    keyword = payload.keyword.strip().lower()
    exists = db.scalar(select(models.SpecialtyConcern).where(
        models.SpecialtyConcern.specialty_id == specialty_id, models.SpecialtyConcern.keyword == keyword))
    if exists:
        exists.weight = payload.weight
    else:
        db.add(models.SpecialtyConcern(specialty_id=specialty_id, keyword=keyword, weight=payload.weight))
    db.commit()
    audit(db, action="SPECIALTY_CONCERN_ADD", resource="specialty_concern", resource_id=specialty_id,
          user=user, request=request, new_value={"keyword": keyword, "weight": payload.weight})
    return {"success": True, "specialty_id": specialty_id, "keyword": keyword}


@router.delete("/concerns/{concern_id}", summary="Remove a concern mapping")
def delete_concern(concern_id: int, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("specialties:write"))):
    row = db.get(models.SpecialtyConcern, concern_id)
    if not row:
        raise HTTPException(status_code=404, detail="Concern mapping not found")
    db.delete(row)
    db.commit()
    audit(db, action="SPECIALTY_CONCERN_DELETE", resource="specialty_concern", resource_id=concern_id,
          user=user, request=request)
    return {"success": True}


@router.get("/{specialty_id}/doctors", summary="Doctors in a specialty")
def specialty_doctors(specialty_id: int, db: Session = Depends(get_db),
                      _: CurrentUser | None = Depends(get_optional_user)):
    doctors = db.scalars(select(models.Doctor).where(
        models.Doctor.specialty_id == specialty_id, models.Doctor.is_active.is_(True))).all()
    return [{"id": d.id, "name": d.full_name, "qualifications": d.qualifications,
             "experience_years": d.experience_years, "fee": float(d.consultation_fee or 0),
             "rating": float(d.rating_avg or 0), "languages": d.languages} for d in doctors]


@router.get("/{specialty_id}/availability", summary="Availability + open slots in a specialty")
def specialty_availability(specialty_id: int, db: Session = Depends(get_db), days_ahead: int = 7,
                           _: CurrentUser | None = Depends(get_optional_user)):
    rows = slot_service.available_slots(db, specialty_id=specialty_id,
                                        from_date=date.today(),
                                        to_date=date.today() + timedelta(days=days_ahead), limit=60)
    return {"specialty_id": specialty_id, "count": len(rows),
            "slots": [{**r, "slot_date": r["slot_date"].isoformat(),
                       "start_time": r["start_time"].strftime("%H:%M"),
                       "end_time": r["end_time"].strftime("%H:%M")} for r in rows]}


@router.get("/{specialty_id}/demand", summary="Appointment demand for a specialty")
def specialty_demand(specialty_id: int, db: Session = Depends(get_db), days: int = 30,
                     _: CurrentUser = Depends(get_current_user)):
    start = date.today() - timedelta(days=days)
    rows = db.execute(
        select(models.Appointment.appointment_date, func.count(models.Appointment.id))
        .where(models.Appointment.specialty_id == specialty_id, models.Appointment.appointment_date >= start)
        .group_by(models.Appointment.appointment_date).order_by(models.Appointment.appointment_date)
    ).all()
    by_doctor = db.execute(
        select(models.Doctor.full_name, func.count(models.Appointment.id))
        .join(models.Appointment, models.Appointment.doctor_id == models.Doctor.id)
        .where(models.Appointment.specialty_id == specialty_id, models.Appointment.appointment_date >= start)
        .group_by(models.Doctor.full_name)
    ).all()
    return {
        "specialty_id": specialty_id, "window_days": days,
        "total_appointments": sum(c for _, c in rows),
        "daily": [{"date": d.isoformat(), "count": c} for d, c in rows],
        "by_doctor": [{"doctor": n, "count": c} for n, c in by_doctor],
    }
