"""Notification service endpoints (In-App / Email / WhatsApp) + WhatsApp console."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..security import utcnow
from ..services import notify

router = APIRouter(prefix="/notifications", tags=["Notifications"])
wa_router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])


@router.get("", response_model=list[schemas.NotificationOut], summary="List notifications")
def list_notifications(db: Session = Depends(get_db), patient_id: int | None = None,
                       channel: str | None = None, status_filter: str | None = None,
                       limit: int = Query(100, le=500), user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Notification)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Notification.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("notifications:read"):
        stmt = stmt.where(models.Notification.user_id == user.id)
    if patient_id:
        stmt = stmt.where(models.Notification.patient_id == patient_id)
    if channel:
        stmt = stmt.where(models.Notification.channel == channel.upper())
    if status_filter:
        stmt = stmt.where(models.Notification.status == status_filter.upper())
    return list(db.scalars(stmt.order_by(models.Notification.created_at.desc()).limit(limit)))


@router.get("/my", response_model=list[schemas.NotificationOut], summary="My in-app notifications")
def my_notifications(db: Session = Depends(get_db), unread_only: bool = False,
                     user: CurrentUser = Depends(get_current_user)):
    patient = patient_for_user(db, user.id)
    stmt = select(models.Notification).where(
        (models.Notification.user_id == user.id)
        | (models.Notification.patient_id == (patient.id if patient else -1))
    )
    if unread_only:
        stmt = stmt.where(models.Notification.read_at.is_(None))
    return list(db.scalars(stmt.order_by(models.Notification.created_at.desc()).limit(50)))


@router.post("/{notification_id}/read", response_model=schemas.NotificationOut, summary="Mark as read")
def mark_read(notification_id: int, db: Session = Depends(get_db),
              user: CurrentUser = Depends(get_current_user)):
    notif = db.get(models.Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.read_at = utcnow()
    db.commit()
    db.refresh(notif)
    return notif


@router.post("", response_model=schemas.NotificationOut, status_code=201, summary="Queue / send a notification")
def create_notification(payload: schemas.NotificationCreate, request: Request, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(require_permission("notifications:write"))):
    patient = db.get(models.Patient, payload.patient_id) if payload.patient_id else None
    if payload.patient_id and not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    notif = notify.queue_notification(
        db, template=payload.template, context=payload.payload or {"message": payload.message},
        channel=payload.channel, patient=patient, user_id=payload.user_id, subject=payload.subject,
    )
    if payload.send_now:
        notify.dispatch_notification(db, notif)
    audit(db, action="NOTIFICATION_CREATE", resource="notification", resource_id=notif.id, user=user,
          request=request, new_value={"channel": notif.channel, "template": notif.template,
                                      "status": notif.status})
    return notif


@router.post("/{notification_id}/retry", response_model=schemas.NotificationOut, summary="Retry a failed send")
def retry(notification_id: int, request: Request, db: Session = Depends(get_db),
          user: CurrentUser = Depends(require_permission("notifications:write"))):
    notif = db.get(models.Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.status = "RETRYING"
    notif.retry_count = (notif.retry_count or 0) + 1
    db.commit()
    notify.dispatch_notification(db, notif)
    audit(db, action="NOTIFICATION_RETRY", resource="notification", resource_id=notification_id, user=user,
          request=request, new_value={"status": notif.status})
    return notif


@router.get("/templates", summary="WhatsApp / Email templates")
def templates(_: CurrentUser = Depends(require_permission("notifications:read"))):
    return [{"name": name, "preview": body[:200]} for name, body in notify.TEMPLATES.items()]


@router.get("/stats", summary="Delivery statistics")
def stats(db: Session = Depends(get_db), days: int = 30,
          _: CurrentUser = Depends(require_permission("notifications:read", "analytics:read"))):
    start = datetime.combine(date.today() - timedelta(days=days), datetime.min.time())
    rows = db.execute(
        select(models.Notification.channel, models.Notification.status, func.count(models.Notification.id))
        .where(models.Notification.created_at >= start)
        .group_by(models.Notification.channel, models.Notification.status)
    ).all()
    pending = db.scalar(select(func.count(models.Notification.id)).where(
        models.Notification.status == "PENDING")) or 0
    return {"window_days": days, "pending": pending,
            "breakdown": [{"channel": c, "status": s, "count": n} for c, s, n in rows]}


# ==========================================================================
# WhatsApp
# ==========================================================================
@wa_router.get("", response_model=list[schemas.WhatsappOut], summary="WhatsApp message log")
def wa_log(db: Session = Depends(get_db), patient_id: int | None = None, status_filter: str | None = None,
           limit: int = Query(100, le=500),
           _: CurrentUser = Depends(require_permission("notifications:read", "whatsapp:send", any_of=True))):
    stmt = select(models.WhatsappMessage)
    if patient_id:
        stmt = stmt.where(models.WhatsappMessage.patient_id == patient_id)
    if status_filter:
        stmt = stmt.where(models.WhatsappMessage.status == status_filter.upper())
    return list(db.scalars(stmt.order_by(models.WhatsappMessage.created_at.desc()).limit(limit)))


@wa_router.post("/send", response_model=schemas.WhatsappOut, status_code=201, summary="Send a WhatsApp message")
def send(payload: schemas.NotificationCreate, request: Request, db: Session = Depends(get_db),
         user: CurrentUser = Depends(require_permission("whatsapp:send"))):
    patient = db.get(models.Patient, payload.patient_id) if payload.patient_id else None
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    body = notify.render(payload.template, payload.payload or {"message": payload.message})
    wa = notify.send_whatsapp(db, to_number=patient.phone, template=payload.template, body=body,
                              patient_id=patient.id, payload=payload.payload)
    audit(db, action="WHATSAPP_SEND", resource="whatsapp_message", resource_id=wa.id, user=user,
          request=request, new_value={"to": wa.to_number, "template": wa.template, "status": wa.status})
    return wa


@wa_router.get("/status/summary", summary="WhatsApp delivery funnel")
def wa_summary(db: Session = Depends(get_db),
               _: CurrentUser = Depends(require_permission("notifications:read"))):
    rows = db.execute(
        select(models.WhatsappMessage.status, func.count(models.WhatsappMessage.id))
        .group_by(models.WhatsappMessage.status)
    ).all()
    by_template = db.execute(
        select(models.WhatsappMessage.template, func.count(models.WhatsappMessage.id))
        .group_by(models.WhatsappMessage.template).order_by(func.count(models.WhatsappMessage.id).desc())
    ).all()
    total = sum(c for _, c in rows)
    sent = sum(c for s, c in rows if s in {"SENT", "DELIVERED", "READ"})
    return {
        "provider": "twilio" if notify.settings.whatsapp_enabled else "console (WHATSAPP_ENABLED=false)",
        "total": total, "sent": sent,
        "delivery_rate": round((sent / total) * 100, 1) if total else 0,
        "by_status": {s: c for s, c in rows},
        "by_template": [{"template": t, "count": c} for t, c in by_template],
    }


@wa_router.post("/{message_id}/retry", response_model=schemas.WhatsappOut, summary="Retry a failed message")
def wa_retry(message_id: int, request: Request, db: Session = Depends(get_db),
             user: CurrentUser = Depends(require_permission("whatsapp:send"))):
    wa = db.get(models.WhatsappMessage, message_id)
    if not wa:
        raise HTTPException(status_code=404, detail="Message not found")
    wa.attempts += 1
    wa.status = "SENT"
    wa.error = None
    wa.sent_at = utcnow()
    db.commit()
    audit(db, action="WHATSAPP_RETRY", resource="whatsapp_message", resource_id=message_id, user=user,
          request=request)
    db.refresh(wa)
    return wa


@wa_router.post("/webhook/status", summary="Twilio status callback (delivery receipts)")
def webhook(message_id: int, status_value: str = Query(..., alias="status"),
            error: str | None = None, db: Session = Depends(get_db)):
    wa = db.get(models.WhatsappMessage, message_id)
    if not wa:
        raise HTTPException(status_code=404, detail="Message not found")
    wa.status = status_value.upper()
    wa.error = error
    now = utcnow()
    if wa.status in {"SENT", "DELIVERED", "READ"}:
        wa.sent_at = wa.sent_at or now
    if wa.status == "DELIVERED":
        wa.delivered_at = now
    if wa.status == "READ":
        wa.read_at = now
    db.commit()
    return {"success": True, "message_id": message_id, "status": wa.status}
