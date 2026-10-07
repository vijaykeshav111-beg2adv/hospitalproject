"""Reviews: request, rating, doctor/clinic review, moderation, doctor rating rollup."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..security import utcnow
from ..services import notify

router = APIRouter(prefix="/reviews", tags=["Reviews"])


def _out(db: Session, r: models.Review) -> dict:
    patient = db.get(models.Patient, r.patient_id)
    doctor = db.get(models.Doctor, r.doctor_id) if r.doctor_id else None
    return {
        "id": r.id, "patient_id": r.patient_id,
        "patient_name": (patient.full_name.split()[0] + " " + patient.full_name.split()[-1][:1] + "."
                         if patient and patient.full_name else None),
        "doctor_id": r.doctor_id, "doctor_name": doctor.full_name if doctor else None,
        "appointment_id": r.appointment_id, "review_type": r.review_type, "rating": r.rating,
        "title": r.title, "comment": r.comment, "status": r.status, "admin_note": r.admin_note,
        "created_at": r.created_at,
    }


@router.get("", response_model=list[schemas.ReviewOut], summary="List reviews")
def list_reviews(db: Session = Depends(get_db), doctor_id: int | None = None,
                 status_filter: str | None = "APPROVED", min_rating: int | None = None,
                 limit: int = Query(100, le=500), user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Review)
    if status_filter and status_filter.upper() != "ALL":
        stmt = stmt.where(models.Review.status == status_filter.upper())
    if doctor_id:
        stmt = stmt.where(models.Review.doctor_id == doctor_id)
    if min_rating:
        stmt = stmt.where(models.Review.rating >= min_rating)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Review.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("reviews:read"):
        raise HTTPException(status_code=403, detail="Missing permission: reviews:read")
    rows = db.scalars(stmt.order_by(models.Review.created_at.desc()).limit(limit)).all()
    return [_out(db, r) for r in rows]


@router.post("", response_model=schemas.ReviewOut, status_code=201, summary="Submit a review")
def create_review(payload: schemas.ReviewCreate, request: Request, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    patient = patient_for_user(db, user.id)
    if not patient and not user.has_permission("reviews:read"):
        raise HTTPException(status_code=400, detail="Only patients can submit reviews")
    patient_id = patient.id if patient else None
    if not patient_id:
        raise HTTPException(status_code=400, detail="No patient profile linked to this login")

    if payload.appointment_id:
        appointment = db.get(models.Appointment, payload.appointment_id)
        if not appointment or appointment.patient_id != patient_id:
            raise HTTPException(status_code=403, detail="Appointment does not belong to you")
        if appointment.status != "COMPLETED":
            raise HTTPException(status_code=409, detail="You can review only after the visit is completed")
        existing = db.scalar(select(models.Review).where(
            models.Review.appointment_id == payload.appointment_id))
        if existing:
            existing.rating = payload.rating
            existing.comment = payload.comment
            existing.title = payload.title
            existing.status = "PENDING"
            db.commit()
            db.refresh(existing)
            return _out(db, existing)

    review = models.Review(
        patient_id=patient_id, doctor_id=payload.doctor_id, appointment_id=payload.appointment_id,
        review_type=payload.review_type, rating=payload.rating, title=payload.title,
        comment=payload.comment, status="PENDING",
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    audit(db, action="REVIEW_SUBMIT", resource="review", resource_id=review.id, user=user, request=request,
          new_value={"rating": review.rating, "doctor_id": review.doctor_id,
                     "type": review.review_type})
    return _out(db, review)


@router.post("/request/{appointment_id}", summary="Send a review request after a visit")
def request_review(appointment_id: int, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("reviews:write", "notifications:write",
                                                                  any_of=True))):
    appointment = db.get(models.Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    doctor = db.get(models.Doctor, appointment.doctor_id)
    notify.notify_patient(db, appointment.patient, "REVIEW_REQUEST", {
        "patient_name": appointment.patient.full_name,
        "doctor_name": doctor.full_name if doctor else "our team",
        "link": "/ui/reviews",
    }, channels=["WHATSAPP", "IN_APP"], appointment_id=appointment.id)

    existing = db.scalar(select(models.Review).where(models.Review.appointment_id == appointment_id))
    if not existing:
        db.add(models.Review(patient_id=appointment.patient_id, doctor_id=appointment.doctor_id,
                             appointment_id=appointment_id, review_type="DOCTOR", rating=0,
                             status="REQUESTED", requested_at=utcnow()))
        db.commit()
    audit(db, action="REVIEW_REQUEST", resource="appointment", resource_id=appointment_id, user=user,
          request=request)
    return {"success": True, "appointment_id": appointment_id, "message": "Review request sent"}


@router.post("/{review_id}/moderate", response_model=schemas.ReviewOut, summary="Approve / reject a review")
def moderate(review_id: int, payload: schemas.ReviewModerate, request: Request, db: Session = Depends(get_db),
             user: CurrentUser = Depends(require_permission("reviews:moderate"))):
    review = db.get(models.Review, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    before = {"status": review.status}
    review.status = payload.status
    review.admin_note = payload.admin_note
    review.moderated_by = user.id
    review.moderated_at = utcnow()
    db.commit()

    # rollup doctor rating
    if review.doctor_id:
        doctor = db.get(models.Doctor, review.doctor_id)
        if doctor:
            avg, count = db.execute(
                select(func.coalesce(func.avg(models.Review.rating), 0), func.count(models.Review.id))
                .where(models.Review.doctor_id == review.doctor_id,
                       models.Review.status == "APPROVED", models.Review.rating > 0)
            ).one()
            doctor.rating_avg = round(float(avg), 2)
            doctor.rating_count = int(count)
            db.commit()

    audit(db, action="REVIEW_MODERATE", resource="review", resource_id=review_id, user=user, request=request,
          previous_value=before, new_value={"status": review.status, "note": payload.admin_note})
    return _out(db, review)


@router.get("/pending/moderation", summary="Reviews awaiting moderation")
def pending(db: Session = Depends(get_db), _: CurrentUser = Depends(require_permission("reviews:moderate"))):
    rows = db.scalars(select(models.Review).where(models.Review.status.in_(["PENDING", "REQUESTED"]))
                      .order_by(models.Review.created_at.desc())).all()
    return [_out(db, r) for r in rows]


@router.get("/doctor/{doctor_id}/summary", summary="Rating summary for a doctor")
def doctor_review_summary(doctor_id: int, db: Session = Depends(get_db),
                          _: CurrentUser = Depends(get_current_user)):
    rows = db.execute(
        select(models.Review.rating, func.count(models.Review.id))
        .where(models.Review.doctor_id == doctor_id, models.Review.status == "APPROVED")
        .group_by(models.Review.rating)
    ).all()
    total = sum(c for _, c in rows)
    avg = db.scalar(select(func.coalesce(func.avg(models.Review.rating), 0)).where(
        models.Review.doctor_id == doctor_id, models.Review.status == "APPROVED")) or 0
    return {
        "doctor_id": doctor_id, "total_reviews": total, "average_rating": round(float(avg), 2),
        "distribution": {str(r): c for r, c in rows},
        "recent_comments": [{"rating": r.rating, "comment": r.comment,
                             "at": r.created_at.isoformat() if r.created_at else None}
                            for r in db.scalars(select(models.Review).where(
                                models.Review.doctor_id == doctor_id,
                                models.Review.status == "APPROVED",
                                models.Review.comment.is_not(None)).order_by(
                                models.Review.created_at.desc()).limit(5))],
    }
