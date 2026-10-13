"""MCP server 06 - Notification Server  [ADMIN tier].

WhatsApp / Email / In-App notification queue: list, inspect, send, stats.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app import models
from app.security import utcnow

from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["notifications"]
TOOLS: dict = {}

VALID_CHANNELS = {"WHATSAPP", "EMAIL", "IN_APP", "SMS"}


def _notif_brief(n: models.Notification) -> dict:
    return {
        "id": n.id,
        "patient_id": n.patient_id,
        "patient": n.patient.full_name if n.patient else None,
        "channel": n.channel,
        "template": n.template,
        "subject": n.subject,
        "message": n.message[:200],
        "status": n.status,
        "retry_count": n.retry_count,
        "sent_at": str(n.sent_at) if n.sent_at else None,
    }


@tool("list_notifications", "List notification attempts (filter by channel / status).")
def _list_notifications(db, channel: str = "", status: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.Notification)
    if (channel or "").strip().upper() in VALID_CHANNELS:
        stmt = stmt.where(models.Notification.channel == channel.strip().upper())
    if (status or "").strip():
        stmt = stmt.where(models.Notification.status == status.strip().upper())
    rows = db.scalars(stmt.order_by(models.Notification.id.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_notifications", what="notification")
    return [_notif_brief(n) for n in rows]


@tool("get_notification", "Read one notification record in full.")
def _get_notification(db, notification_id: int):
    n = db.get(models.Notification, notification_id)
    if n is None:
        return grounded_no(server=SPEC.key, tool="get_notification", what="notification")
    brief = _notif_brief(n)
    brief["message"] = n.message
    brief["payload"] = n.payload
    brief["error"] = n.error
    return brief


@tool("send_notification", "Queue a notification for a patient (IN_APP/EMAIL/WHATSAPP/SMS).", write=True)
def _send_notification(db, patient_id: int, message: str, channel: str = "IN_APP",
                       template: str = "CUSTOM", subject: str = ""):
    patient = db.get(models.Patient, patient_id)
    if patient is None or not (message or "").strip():
        return grounded_no(server=SPEC.key, tool="send_notification", what="patient/message",
                           note="A real patient_id and non-empty message are required.")
    ch = (channel or "IN_APP").strip().upper()
    if ch not in VALID_CHANNELS:
        ch = "IN_APP"
    n = models.Notification(
        user_id=patient.user_id,
        patient_id=patient.id,
        channel=ch,
        template=template or "CUSTOM",
        subject=subject or None,
        message=message.strip(),
        status="PENDING",
        scheduled_at=utcnow(),
    )
    db.add(n)
    db.flush()
    return {"queued": True, "notification": _notif_brief(n),
            "note": "Queued in the notifications table; the platform's delivery job sends it."}


@tool("notification_stats", "Delivery counts by channel and status.")
def _notification_stats(db):
    rows = db.execute(
        select(models.Notification.channel, models.Notification.status, func.count(models.Notification.id))
        .group_by(models.Notification.channel, models.Notification.status)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="notification_stats", what="notification")
    stats: dict[str, dict] = {}
    for channel, status, count in rows:
        stats.setdefault(channel, {})[status] = int(count)
    return stats


mcp = make_server(
    SPEC,
    "Notification Server (ADMIN tier). Queues messages into the platform's "
    "notification service - it never fabricates delivery confirmations.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
