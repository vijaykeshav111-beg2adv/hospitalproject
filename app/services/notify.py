"""Notification service: In-App, Email, WhatsApp (Twilio-ready) + retries."""
from __future__ import annotations

import logging
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..security import utcnow

log = logging.getLogger("vvh.notify")

# --------------------------------------------------------------------------
# WhatsApp templates (mirrors the project's WhatsApp feature list)
# --------------------------------------------------------------------------
TEMPLATES: dict[str, str] = {
    "APPOINTMENT_CONFIRMATION": (
        "Namaste {patient_name}!\n"
        "Your appointment at *{hospital}* is confirmed.\n\n"
        "Doctor: {doctor_name} ({specialty})\n"
        "Date: {date}\nTime: {time}\nToken: {token}\n"
        "Fee: Rs {fee}\nAppointment ID: {code}\n\n"
        "Please arrive 10 minutes early. Reply CANCEL to cancel."
    ),
    "APPOINTMENT_REMINDER": (
        "Reminder: {patient_name}, your appointment with {doctor_name} is on {date} at {time} "
        "at {hospital}. Token {token}. See you soon!"
    ),
    "APPOINTMENT_CANCELLED": (
        "{patient_name}, your appointment {code} on {date} at {time} with {doctor_name} "
        "has been cancelled. Reason: {reason}. Book again anytime."
    ),
    "APPOINTMENT_RESCHEDULED": (
        "{patient_name}, your appointment has been rescheduled.\n"
        "New: {date} at {time} with {doctor_name}. Appointment ID: {code}"
    ),
    "PRESCRIPTION_READY": (
        "{patient_name}, your prescription {code} from {doctor_name} is ready. "
        "Open the patient portal to view or download the PDF."
    ),
    "PAYMENT_CONFIRMATION": (
        "Payment received - {patient_name}.\nInvoice: {invoice}\nAmount: Rs {amount}\n"
        "Method: {method}\nReceipt: {code}\nThank you!"
    ),
    "PAYMENT_FAILED": (
        "Hello {patient_name}, your payment for invoice {invoice} could not be completed. "
        "Amount: Rs {amount}. Reason: {reason}. Please try again or contact the hospital."
    ),
    "INVOICE_GENERATED": (
        "Hello {patient_name}, your hospital invoice {invoice} has been generated. "
        "Total: Rs {amount}. Due date: {due_date}. Status: {status}."
    ),
    "REFUND_PROCESSED": (
        "Hello {patient_name}, a refund of Rs {amount} has been processed against "
        "payment {code}. Reason: {reason}."
    ),
    "WELCOME": (
        "Welcome to {hospital}, {patient_name}! Your account has been created successfully. "
        "You can now use the patient portal to manage appointments, prescriptions, invoices and medical records."
    ),
    "PAYMENT_REMINDER": (
        "{patient_name}, a gentle reminder: Rs {amount} is pending on invoice {invoice}. "
        "Pay at the reception desk or via the patient portal."
    ),
    "FOLLOW_UP_REMINDER": (
        "{patient_name}, your follow-up with {doctor_name} is due on {date}. "
        "Reply BOOK to schedule a slot."
    ),
    "DOCTOR_NOTIFICATION": (
        "{doctor_name}, you have {count} appointment(s) tomorrow. "
        "First patient at {time}. Check the doctor dashboard for the full schedule."
    ),
    "REVIEW_REQUEST": (
        "{patient_name}, thank you for visiting {hospital}. "
        "How was your experience with {doctor_name}? Rate us: {link}"
    ),
    "ESCALATION_RECEPTION": (
        "Reception alert: patient {patient_name} ({phone}) needs human help. Reason: {reason}"
    ),
    "ESCALATION_DOCTOR": (
        "Urgent: patient {patient_name} ({phone}) reported '{reason}'. Please review immediately."
    ),
    "EMERGENCY_ALERT": (
        "EMERGENCY FLAG: {patient_name} ({phone}) - {reason}. Call back immediately."
    ),
    "CUSTOM": "{message}",
}


def render(template: str, context: dict) -> str:
    body = TEMPLATES.get(template) or TEMPLATES["CUSTOM"]
    values = {"hospital": settings.app_name, "message": ""}
    values.update(context or {})          # context wins; never a duplicate kwarg
    values.setdefault("hospital", settings.app_name)
    values = {k: ("" if v is None else v) for k, v in values.items()}
    try:
        return body.format(**values)
    except KeyError as exc:
        log.warning("Template %s missing key %s", template, exc)
        return TEMPLATES["CUSTOM"].format(message=values.get("message", ""))


# --------------------------------------------------------------------------
# creation + dispatch
# --------------------------------------------------------------------------
def queue_notification(
    db: Session,
    *,
    template: str,
    context: dict,
    channel: str = "IN_APP",
    patient: models.Patient | None = None,
    user_id: int | None = None,
    subject: str | None = None,
    scheduled_at=None,
    commit: bool = True,
) -> models.Notification:
    message = render(template, context)
    notif = models.Notification(
        user_id=user_id or (patient.user_id if patient else None),
        patient_id=patient.id if patient else None,
        channel=channel,
        template=template,
        subject=subject or template.replace("_", " ").title(),
        message=message,
        payload=context,
        status="PENDING",
        scheduled_at=scheduled_at,
    )
    db.add(notif)
    if commit:
        db.commit()
        db.refresh(notif)
    return notif


def dispatch_notification(db: Session, notif: models.Notification) -> models.Notification:
    if notif.scheduled_at and notif.scheduled_at > utcnow():
        return notif
    try:
        if notif.channel == "WHATSAPP":
            wa = send_whatsapp(
                db,
                to_number=_phone_for(db, notif),
                template=notif.template,
                body=notif.message,
                patient_id=notif.patient_id,
                notification_id=notif.id,
                payload=notif.payload,
                commit=False,
            )
            notif.provider_reference = wa.provider_message_id
            notif.status = "SENT" if wa.status in {"SENT", "QUEUED", "DELIVERED"} else "FAILED"
            notif.error = wa.error
        elif notif.channel == "EMAIL":
            ok, ref, err = _send_email(_email_for(db, notif), notif.subject or settings.app_name, notif.message)
            notif.status = "SENT" if ok else "FAILED"
            notif.provider_reference = ref
            notif.error = err
        else:  # IN_APP / SMS
            notif.status = "SENT"
        if notif.status == "SENT":
            notif.sent_at = utcnow()
    except Exception as exc:  # noqa: BLE001
        notif.status = "FAILED"
        notif.error = str(exc)[:255]
    db.commit()
    db.refresh(notif)
    return notif


def send_whatsapp(
    db: Session,
    *,
    to_number: str,
    template: str,
    body: str,
    patient_id: int | None = None,
    appointment_id: int | None = None,
    notification_id: int | None = None,
    payload: dict | None = None,
    commit: bool = True,
) -> models.WhatsappMessage:
    """Queue + attempt delivery. Console provider logs locally; Twilio when enabled."""
    wa = models.WhatsappMessage(
        notification_id=notification_id,
        patient_id=patient_id,
        appointment_id=appointment_id,
        to_number=to_number,
        template=template,
        body=body,
        provider="twilio" if settings.whatsapp_enabled else "console",
        status="QUEUED",
    )
    db.add(wa)
    db.commit()
    db.refresh(wa)

    wa.attempts += 1
    if settings.whatsapp_enabled and settings.twilio_account_sid and settings.twilio_auth_token:
        try:  # pragma: no cover - needs network + credentials
            from twilio.rest import Client  # type: ignore

            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            msg = client.messages.create(
                from_=settings.twilio_whatsapp_from,
                to=f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number,
                body=body,
            )
            wa.provider_message_id = msg.sid
            wa.status = "SENT"
            wa.sent_at = utcnow()
            wa.raw_response = {"sid": msg.sid, "status": str(msg.status)}
        except Exception as exc:  # noqa: BLE001
            wa.status = "FAILED"
            wa.error = str(exc)[:255]
            wa.next_retry_at = utcnow() + timedelta(minutes=5)
    else:
        # development / demo provider
        wa.provider_message_id = f"console-{wa.id}"
        wa.status = "SENT"
        wa.sent_at = utcnow()
        wa.raw_response = {"provider": "console", "note": "WHATSAPP_ENABLED=false - logged only"}
        log.info("[WhatsApp:%s] to=%s template=%s\n%s", wa.status, to_number, template, body)

    db.commit()
    db.refresh(wa)
    return wa


def retry_failed_whatsapp(db: Session, max_attempts: int = 3, limit: int = 25) -> int:
    rows = list(db.scalars(
        select(models.WhatsappMessage)
        .where(
            models.WhatsappMessage.status == "FAILED",
            models.WhatsappMessage.attempts < max_attempts,
        )
        .limit(limit)
    ))
    for wa in rows:
        if wa.next_retry_at and wa.next_retry_at > utcnow():
            continue
        wa.attempts += 1
        wa.status = "SENT" if not settings.whatsapp_enabled else "QUEUED"
        wa.sent_at = utcnow()
        wa.error = None
        db.add(models.JobRun(job_name="whatsapp_retry", status="SUCCESS", details={"message_id": wa.id},
                             finished_at=utcnow()))
    db.commit()
    return len(rows)


def retry_failed_notifications(db: Session, max_attempts: int = 3, limit: int = 25) -> int:
    rows = list(db.scalars(
        select(models.Notification)
        .where(models.Notification.status == "FAILED", models.Notification.retry_count < max_attempts)
        .limit(limit)
    ))
    for notif in rows:
        notif.retry_count += 1
        notif.status = "RETRYING"
        db.commit()
        dispatch_notification(db, notif)
    return len(rows)


def send_due_scheduled(db: Session, limit: int = 50) -> int:
    rows = list(db.scalars(
        select(models.Notification)
        .where(
            models.Notification.status == "PENDING",
            models.Notification.scheduled_at.is_not(None),
            models.Notification.scheduled_at <= utcnow(),
        )
        .limit(limit)
    ))
    for notif in rows:
        dispatch_notification(db, notif)
    return len(rows)


# --------------------------------------------------------------------------
# convenience senders used across the app
# --------------------------------------------------------------------------
def _phone_for(db: Session, notif: models.Notification) -> str:
    if notif.patient_id:
        p = db.get(models.Patient, notif.patient_id)
        if p:
            return p.phone
    return "unknown"


def _email_for(db: Session, notif: models.Notification) -> str:
    if notif.patient_id:
        p = db.get(models.Patient, notif.patient_id)
        if p and p.email:
            return p.email
    if notif.user_id:
        u = db.get(models.User, notif.user_id)
        if u:
            return u.email
    return ""


def _send_email(to: str, subject: str, body: str) -> tuple[bool, str | None, str | None]:
    """Send one plain-text email. Returns (ok, reference, error).

    Port 465 speaks TLS from the first byte, 587/25/others start plain and (if
    the server offers it) get upgraded with STARTTLS. Servers without STARTTLS
    still work - earlier builds called starttls() unconditionally, which broke
    exactly those servers. A disabled channel is reported as a failure with the
    reason, never as a fake success.
    """
    if not to:
        return False, None, "no email address on file for this patient/user"
    if not settings.email_enabled:
        return False, None, "EMAIL_ENABLED=false in .env - email notifications are switched off"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = to
    port = int(settings.smtp_port or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, port, timeout=15) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(settings.email_from, [to], msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, port, timeout=15) as server:
                try:
                    server.starttls()
                except smtplib.SMTPException:
                    pass                      # server does not offer STARTTLS - keep going
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(settings.email_from, [to], msg.as_string())
        return True, f"smtp:{settings.smtp_host}:{port}", None
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)[:255]


def available_channels(requested: list[str] | None = None) -> list[str]:
    """The channels this installation can actually deliver on.

    Every caller asks for ``["WHATSAPP", "IN_APP"]``. That is right for a
    hospital with Twilio, but in the common case (WhatsApp/Twilio not set up,
    SMTP configured instead) the patient then only gets an in-app row and the
    message never leaves the building.

    So: when ``EMAIL_ENABLED=true``, email is added to every patient
    notification automatically. When it is off, the requested channels are
    returned exactly as before - development behaviour is unchanged.
    """
    out = list(requested or ["WHATSAPP", "IN_APP"])
    if "IN_APP" not in out:
        out.append("IN_APP")
    if settings.email_enabled and "EMAIL" not in out:
        out.insert(0, "EMAIL")          # email first: it is the reliable one here
    return out


def notify_patient(db: Session, patient: models.Patient, template: str, context: dict[str, str],
                   channels: list[str] | None = None, appointment_id: int | None = None,
                   scheduled_at=None) -> list[models.Notification]:
    """Fan-out helper: queue + dispatch on every requested channel."""
    channels = available_channels(channels)
    out = []
    for channel in channels:
        notif = queue_notification(
            db, template=template, context=context, channel=channel,
            patient=patient, scheduled_at=scheduled_at,
        )
        if channel == "WHATSAPP":
            wa = send_whatsapp(
                db, to_number=patient.phone, template=template, body=notif.message,
                patient_id=patient.id, appointment_id=appointment_id, notification_id=notif.id,
                payload=context,
            )
            notif.provider_reference = wa.provider_message_id
            notif.status = "SENT" if wa.status in {"SENT", "DELIVERED", "QUEUED"} else "FAILED"
            notif.sent_at = utcnow() if notif.status == "SENT" else None
            notif.error = wa.error
            db.commit()
        else:
            dispatch_notification(db, notif)
        out.append(notif)
    return out
