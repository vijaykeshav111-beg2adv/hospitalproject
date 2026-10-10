"""Payments: cash / UPI / card / online, statuses, refunds and history."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..services import billing, notify

router = APIRouter(prefix="/payments", tags=["Payments"])


def _out(db: Session, p: models.Payment) -> dict:
    patient = db.get(models.Patient, p.patient_id)
    invoice = db.get(models.Invoice, p.invoice_id) if p.invoice_id else None
    return {
        "id": p.id, "payment_code": p.payment_code, "invoice_id": p.invoice_id,
        "invoice_number": invoice.invoice_number if invoice else None,
        "patient_id": p.patient_id, "patient_name": patient.full_name if patient else None,
        "amount": float(p.amount), "method": p.method, "status": p.status,
        "gateway": p.gateway, "transaction_reference": p.transaction_reference,
        "paid_at": p.paid_at, "notes": p.notes, "created_at": p.created_at,
    }


@router.get("", response_model=list[schemas.PaymentOut], summary="List payments")
def list_payments(db: Session = Depends(get_db), patient_id: int | None = None,
                  method: str | None = None, status_filter: str | None = None, limit: int = Query(100, le=500),
                  user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Payment)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Payment.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("payments:read"):
        raise HTTPException(status_code=403, detail="Missing permission: payments:read")
    if patient_id:
        stmt = stmt.where(models.Payment.patient_id == patient_id)
    if method:
        stmt = stmt.where(models.Payment.method == method.upper())
    if status_filter:
        stmt = stmt.where(models.Payment.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(models.Payment.created_at.desc()).limit(limit)).all()
    return [_out(db, p) for p in rows]


@router.post("", response_model=schemas.PaymentOut, status_code=201, summary="Collect / record a payment")
def create_payment(payload: schemas.PaymentCreate, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("payments:write"))):
    invoice = db.get(models.Invoice, payload.invoice_id) if payload.invoice_id else None
    patient_id = payload.patient_id or (invoice.patient_id if invoice else None)
    patient = db.get(models.Patient, patient_id) if patient_id else None
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found (or not derivable from the invoice)")
    if payload.amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be greater than zero")
    if invoice and payload.amount > float(invoice.balance_amount) + 0.001:
        raise HTTPException(status_code=422,
                            detail=f"Amount exceeds the outstanding balance {float(invoice.balance_amount):.2f}")

    payment = billing.record_payment(
        db, patient=patient, amount=payload.amount, method=payload.method, invoice=invoice,
        status=payload.status, transaction_reference=payload.transaction_reference,
        gateway=payload.gateway, collected_by=user.id, notes=payload.notes,
    )

    if payload.notify_patient and payment.status == "PAID":
        notify.notify_patient(db, patient, "PAYMENT_CONFIRMATION",
                              billing.payment_confirmation_context(payment, invoice, patient),
                              channels=["WHATSAPP", "EMAIL", "IN_APP"])

    audit(db, action="PAYMENT_CREATE", resource="payment", resource_id=payment.id, user=user, request=request,
          new_value={"code": payment.payment_code, "amount": float(payment.amount),
                     "method": payment.method, "status": payment.status,
                     "invoice_id": payment.invoice_id})
    return _out(db, payment)


@router.post("/online/initiate", summary="Initiate an online payment (gateway stub)")
def initiate_online(payload: schemas.PaymentCreate, request: Request, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    """Creates a PENDING online payment and returns a pseudo gateway order reference.
    Wire this to Razorpay/PhonePe/Stripe in production."""
    invoice = db.get(models.Invoice, payload.invoice_id) if payload.invoice_id else None
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or invoice.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    patient = db.get(models.Patient, invoice.patient_id)
    payment = billing.record_payment(
        db, patient=patient, amount=payload.amount or float(invoice.balance_amount), method="ONLINE",
        invoice=invoice, status="PENDING", gateway="gateway_stub",
        transaction_reference=f"ORDER-{invoice.invoice_number}", collected_by=user.id,
        notes="Online payment initiated - awaiting gateway callback",
    )
    audit(db, action="PAYMENT_ONLINE_INIT", resource="payment", resource_id=payment.id, user=user,
          request=request, new_value={"invoice": invoice.invoice_number, "amount": float(payment.amount)})
    return {"payment": _out(db, payment), "gateway_reference": payment.transaction_reference,
            "callback_url": f"/api/v1/payments/{payment.id}/gateway-callback"}


@router.post("/{payment_id}/gateway-callback", summary="Gateway webhook / callback (stub)")
def gateway_callback(payment_id: int, request: Request, status_value: str = "PAID",
                     gateway_reference: str | None = None, db: Session = Depends(get_db)):
    payment = db.get(models.Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    payment.status = status_value.upper()
    payment.gateway_reference = gateway_reference
    payment.paid_at = billing.utcnow() if payment.status == "PAID" else None
    db.commit()
    if payment.invoice_id:
        invoice = db.get(models.Invoice, payment.invoice_id)
        billing.sync_invoice(db, invoice)
        patient = db.get(models.Patient, payment.patient_id)
        if patient and payment.status == "PAID":
            notify.notify_patient(db, patient, "PAYMENT_CONFIRMATION",
                                  billing.payment_confirmation_context(payment, invoice, patient),
                                  channels=["WHATSAPP", "EMAIL", "IN_APP"])
        elif patient and payment.status in {"FAILED", "DECLINED", "CANCELLED"}:
            notify.notify_patient(
                db,
                patient,
                "PAYMENT_FAILED",
                {
                    "patient_name": patient.full_name,
                    "invoice": invoice.invoice_number if invoice else "-",
                    "amount": f"{float(payment.amount):.2f}",
                    "reason": payment.failure_reason or "Payment was not completed",
                },
                channels=["WHATSAPP", "EMAIL", "IN_APP"],
            )
    audit(db, action="PAYMENT_GATEWAY_CALLBACK", resource="payment", resource_id=payment_id,
          new_value={"status": payment.status})
    return {"success": True, **_out(db, payment)}


@router.get("/{payment_id}", response_model=schemas.PaymentOut, summary="Payment detail")
def payment_detail(payment_id: int, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    payment = db.get(models.Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or payment.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return _out(db, payment)


@router.post("/{payment_id}/refund", response_model=schemas.RefundOut, summary="Process a refund")
def refund(payment_id: int, payload: schemas.RefundCreate, request: Request, db: Session = Depends(get_db),
           user: CurrentUser = Depends(require_permission("refunds:write"))):
    payment = db.get(models.Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.status not in {"PAID", "REFUNDED"}:
        raise HTTPException(status_code=409, detail=f"Cannot refund a {payment.status} payment")
    try:
        record = billing.process_refund(db, payment, amount=payload.amount, reason=payload.reason,
                                        processed_by=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    patient = db.get(models.Patient, payment.patient_id)
    notify.notify_patient(
        db,
        patient,
        "REFUND_PROCESSED",
        {
            "patient_name": patient.full_name,
            "amount": f"{float(record.amount):.2f}",
            "code": payment.payment_code,
            "reason": payload.reason or "Refund processed",
        },
        channels=["WHATSAPP", "EMAIL", "IN_APP"],
    )
    audit(db, action="PAYMENT_REFUND", resource="payment", resource_id=payment_id, user=user, request=request,
          new_value={"refund_code": record.refund_code, "amount": float(record.amount),
                     "reason": payload.reason})
    return record


@router.get("/refunds/all", response_model=list[schemas.RefundOut], summary="List refunds")
def list_refunds(db: Session = Depends(get_db), limit: int = 100,
                 _: CurrentUser = Depends(require_permission("refunds:write"))):
    return list(db.scalars(select(models.Refund).order_by(models.Refund.created_at.desc()).limit(limit)))


@router.get("/reports/collection", summary="Collection summary by method")
def collection_report(db: Session = Depends(get_db), days: int = 30,
                      _: CurrentUser = Depends(require_permission("analytics:read", "payments:read"))):
    from datetime import date, timedelta
    start = date.today() - timedelta(days=days)
    rows = db.execute(
        select(models.Payment.method, models.Payment.status,
               func.count(models.Payment.id), func.coalesce(func.sum(models.Payment.amount), 0))
        .where(func.date(models.Payment.created_at) >= start)
        .group_by(models.Payment.method, models.Payment.status)
    ).all()
    return {
        "window_days": days,
        "breakdown": [{"method": m, "status": s, "count": c, "amount": float(a)} for m, s, c, a in rows],
        "total_collected": float(db.scalar(select(func.coalesce(func.sum(models.Payment.amount), 0)).where(
            models.Payment.status == "PAID", func.date(models.Payment.created_at) >= start)) or 0),
        "total_refunded": float(db.scalar(select(func.coalesce(func.sum(models.Refund.amount), 0)).where(
            func.date(models.Refund.created_at) >= start)) or 0),
    }
