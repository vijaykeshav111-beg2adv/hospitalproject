"""Billing: invoices, invoice items, payments, receipts and refunds."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..codes import unique_code
from ..security import utcnow
from . import notify

MONEY = Decimal("0.01")


def d(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


def new_invoice_number(db: Session) -> str:
    """Unique invoice number (invoice_number is UNIQUE in MySQL)."""
    return unique_code(
        db,
        prefix="INV",
        model=models.Invoice,
        column=models.Invoice.invoice_number,
        stamp_format="%Y%m",
    )


def new_payment_code(db: Session) -> str:
    """Unique payment code (payment_code is UNIQUE in MySQL)."""
    return unique_code(
        db,
        prefix="PAY",
        model=models.Payment,
        column=models.Payment.payment_code,
    )


def new_refund_code(db: Session) -> str:
    """Unique refund code (refund_code is UNIQUE in MySQL)."""
    return unique_code(
        db,
        prefix="RFD",
        model=models.Refund,
        column=models.Refund.refund_code,
    )


def recalc_invoice(invoice: models.Invoice) -> models.Invoice:
    subtotal = sum(
        (d(i.amount) for i in invoice.items),
        Decimal("0.00"),
    )

    discount = d(invoice.discount_amount)

    taxable = max(
        Decimal("0.00"),
        subtotal - discount,
    )

    tax_amount = (
        taxable * d(invoice.tax_percent) / Decimal("100")
    ).quantize(MONEY)

    total = (
        taxable + tax_amount
    ).quantize(MONEY)

    paid = sum(
        (
            d(p.amount)
            for p in getattr(invoice, "_payments", [])
            if p.status == "PAID"
        ),
        Decimal("0.00"),
    )

    invoice.subtotal = subtotal
    invoice.tax_amount = tax_amount
    invoice.total_amount = total
    invoice.paid_amount = paid

    invoice.balance_amount = max(
        Decimal("0.00"),
        total - paid - d(invoice.refunded_amount),
    )

    if invoice.refunded_amount and invoice.paid_amount <= invoice.refunded_amount:
        invoice.status = "REFUNDED"

    elif invoice.balance_amount <= 0 and total > 0:
        invoice.status = "PAID"

    elif invoice.paid_amount > 0:
        invoice.status = "PARTIAL"

    elif invoice.status not in {"CANCELLED", "REFUNDED"}:
        invoice.status = "UNPAID"

    return invoice


def _attach_payments(
    db: Session,
    invoice: models.Invoice,
) -> models.Invoice:
    invoice._payments = list(
        db.scalars(
            select(models.Payment).where(
                models.Payment.invoice_id == invoice.id
            )
        )
    )

    return invoice


def sync_invoice(
    db: Session,
    invoice: models.Invoice,
) -> models.Invoice:
    _attach_payments(db, invoice)

    recalc_invoice(invoice)

    db.commit()
    db.refresh(invoice)

    return invoice


def create_invoice(
    db: Session,
    *,
    patient: models.Patient,
    items: list[dict],
    appointment_id: int | None = None,
    consultation_id: int | None = None,
    doctor_id: int | None = None,
    discount_amount: float = 0,
    discount_reason: str | None = None,
    tax_percent: float = 0,
    notes: str | None = None,
    due_in_days: int = 7,
    issued_by: int | None = None,
) -> models.Invoice:

    invoice = models.Invoice(
        invoice_number=new_invoice_number(db),
        patient_id=patient.id,
        appointment_id=appointment_id,
        consultation_id=consultation_id,
        doctor_id=doctor_id,
        discount_amount=d(discount_amount),
        discount_reason=discount_reason,
        tax_percent=d(tax_percent),
        notes=notes,
        issued_by=issued_by,
        due_date=date.today() + timedelta(days=due_in_days),
    )

    db.add(invoice)
    db.flush()

    for item in items:
        qty = int(item.get("quantity", 1))

        unit = d(
            item.get(
                "unit_price",
                0,
            )
        )

        db.add(
            models.InvoiceItem(
                invoice_id=invoice.id,
                item_type=item.get(
                    "item_type",
                    "CONSULTATION",
                ),
                description=item["description"],
                quantity=qty,
                unit_price=unit,
                amount=(unit * qty).quantize(MONEY),
            )
        )

    db.commit()
    db.refresh(invoice)

    return sync_invoice(
        db,
        invoice,
    )


def invoice_for_appointment(
    db: Session,
    appointment: models.Appointment,
) -> models.Invoice | None:

    return db.scalar(
        select(models.Invoice).where(
            models.Invoice.appointment_id == appointment.id
        )
    )


def ensure_consultation_invoice(
    db: Session,
    appointment: models.Appointment,
    doctor: models.Doctor,
) -> models.Invoice:

    existing = invoice_for_appointment(
        db,
        appointment,
    )

    if existing:
        return existing

    return create_invoice(
        db,
        patient=appointment.patient,
        appointment_id=appointment.id,
        doctor_id=doctor.id,
        items=[
            {
                "item_type": "CONSULTATION",
                "description": (
                    f"Consultation - "
                    f"{doctor.full_name} "
                    f"({doctor.specialty.name if doctor.specialty else ''})"
                ),
                "quantity": 1,
                "unit_price": float(
                    doctor.consultation_fee or 0
                ),
            }
        ],
        notes=(
            f"Auto-generated for appointment "
            f"{appointment.appointment_code}"
        ),
    )


def record_payment(
    db: Session,
    *,
    patient: models.Patient,
    amount: float,
    method: str = "CASH",
    invoice: models.Invoice | None = None,
    status: str = "PAID",
    transaction_reference: str | None = None,
    gateway: str | None = None,
    collected_by: int | None = None,
    notes: str | None = None,
) -> models.Payment:

    payment = models.Payment(
        payment_code=new_payment_code(db),
        invoice_id=invoice.id if invoice else None,
        patient_id=patient.id,
        amount=d(amount),
        method=method,
        status=status,
        gateway=gateway,
        transaction_reference=transaction_reference,
        collected_by=collected_by,
        notes=notes,
        paid_at=utcnow() if status == "PAID" else None,
        failure_reason=None if status == "PAID" else notes,
    )

    db.add(payment)

    db.commit()
    db.refresh(payment)

    if invoice:
        sync_invoice(
            db,
            invoice,
        )

    return payment


def process_refund(
    db: Session,
    payment: models.Payment,
    amount: float | None = None,
    reason: str | None = None,
    processed_by: int | None = None,
) -> models.Refund:

    amount = d(
        amount
        if amount is not None
        else payment.amount
    )

    if amount > d(payment.amount):
        raise ValueError(
            "Refund cannot exceed the paid amount"
        )

    refund = models.Refund(
        refund_code=new_refund_code(db),
        payment_id=payment.id,
        invoice_id=payment.invoice_id,
        patient_id=payment.patient_id,
        amount=amount,
        reason=reason,
        status="PROCESSED",
        processed_by=processed_by,
    )

    db.add(refund)

    payment.status = "REFUNDED"

    db.commit()

    if payment.invoice_id:
        invoice = db.get(
            models.Invoice,
            payment.invoice_id,
        )

        if invoice:
            invoice.refunded_amount = (
                d(invoice.refunded_amount)
                + amount
            )

            sync_invoice(
                db,
                invoice,
            )

    db.refresh(refund)

    return refund


def mark_invoice_paid_with_invoice_items(
    db: Session,
    invoice: models.Invoice,
) -> models.Invoice:

    return sync_invoice(
        db,
        invoice,
    )


def payment_confirmation_context(
    payment: models.Payment,
    invoice: models.Invoice | None,
    patient: models.Patient,
) -> dict:

    return {
        "patient_name": patient.full_name,
        "amount": f"{d(payment.amount):.2f}",
        "method": payment.method,
        "code": payment.payment_code,
        "invoice": (
            invoice.invoice_number
            if invoice
            else "-"
        ),
    }