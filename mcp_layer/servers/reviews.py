"""MCP server 07 - Review Server  [ADMIN tier].

Post-visit reviews, moderation queue and per-doctor rating aggregates.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app import models
from app.security import utcnow

from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["reviews"]
TOOLS: dict = {}

VALID_MODERATION = {"APPROVED", "REJECTED", "PENDING", "FLAGGED"}


def _review_brief(r: models.Review) -> dict:
    return {
        "id": r.id,
        "patient": r.patient.full_name if r.patient else None,
        "doctor": r.doctor.full_name if r.doctor else None,
        "doctor_id": r.doctor_id,
        "rating": r.rating,
        "title": r.title,
        "comment": r.comment,
        "status": r.status,
        "created_at": str(r.created_at) if getattr(r, "created_at", None) else None,
    }


@tool("list_reviews", "List reviews filtered by status / doctor / rating.")
def _list_reviews(db, status: str = "", doctor_id: int | None = None, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.Review)
    if (status or "").strip().upper() in VALID_MODERATION:
        stmt = stmt.where(models.Review.status == status.strip().upper())
    if doctor_id:
        stmt = stmt.where(models.Review.doctor_id == doctor_id)
    rows = db.scalars(stmt.order_by(models.Review.id.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_reviews", what="review")
    return [_review_brief(r) for r in rows]


@tool("get_review", "Read one review in full.")
def _get_review(db, review_id: int):
    r = db.get(models.Review, review_id)
    if r is None:
        return grounded_no(server=SPEC.key, tool="get_review", what="review")
    return _review_brief(r)


@tool("moderate_review", "Approve / reject / flag a review (admin moderation).", write=True)
def _moderate_review(db, review_id: int, decision: str = "APPROVED", admin_note: str = ""):
    r = db.get(models.Review, review_id)
    dec = (decision or "").strip().upper()
    if r is None or dec not in VALID_MODERATION:
        return grounded_no(server=SPEC.key, tool="moderate_review", what="review/decision",
                           note="decision must be one of APPROVED / REJECTED / PENDING / FLAGGED.")
    r.status = dec
    r.moderated_at = utcnow()
    r.admin_note = admin_note or None
    return {"moderated": True, "review": _review_brief(r)}


@tool("doctor_rating_summary", "Average rating and count per doctor (from stored aggregates + reviews).")
def _doctor_rating_summary(db, doctor_id: int | None = None):
    stmt = select(models.Doctor)
    if doctor_id:
        stmt = stmt.where(models.Doctor.id == doctor_id)
    doctors = db.scalars(stmt.order_by(models.Doctor.rating_avg.desc())).all()
    rows = []
    for d in doctors:
        live_avg = db.scalar(
            select(func.avg(models.Review.rating)).where(
                models.Review.doctor_id == d.id, models.Review.status == "APPROVED")
        )
        live_cnt = db.scalar(
            select(func.count(models.Review.id)).where(
                models.Review.doctor_id == d.id, models.Review.status == "APPROVED")
        ) or 0
        rows.append({
            "doctor_id": d.id,
            "doctor": d.full_name,
            "rating_avg": float(live_avg) if live_avg is not None else float(d.rating_avg or 0),
            "rating_count": int(live_cnt),
        })
    if not rows:
        return grounded_no(server=SPEC.key, tool="doctor_rating_summary", what="doctor")
    return rows


mcp = make_server(
    SPEC,
    "Review Server (ADMIN tier). Moderation writes are real DB updates and "
    "are fully audited.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
