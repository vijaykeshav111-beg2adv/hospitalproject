"""Billing: invoices, invoice items, discount, tax, totals, receipts."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, patient_for_user, require_permission
from ..services import billing, documents, notify

router = APIRouter(prefix="/invoices", tags=["Billing"])


def _out(db: Session, inv: models.Invoice) -> dict:
    patient = db.get(models.Patient, inv.patient_id)
    return {
        "id": inv.id, "invoice_number": inv.invoice_number, "patient_id": inv.patient_id,
        "patient_name": patient.full_name if patient else None, "appointment_id": inv.appointment_id,
        "consultation_id": inv.consultation_id, "doctor_id": inv.doctor_id,
        "subtotal": float(inv.subtotal), "discount_amount": float(inv.discount_amount),
        "discount_reason": inv.discount_reason, "tax_percent": float(inv.tax_percent),
        "tax_amount": float(inv.tax_amount), "total_amount": float(inv.total_amount),
        "paid_amount": float(inv.paid_amount), "balance_amount": float(inv.balance_amount),
        "refunded_amount": float(inv.refunded_amount), "status": inv.status, "notes": inv.notes,
        "issued_at": inv.issued_at, "due_date": inv.due_date,
        "items": list(db.scalars(select(models.InvoiceItem).where(models.InvoiceItem.invoice_id == inv.id))),
    }


@router.get("", response_model=list[schemas.InvoiceOut], summary="List invoices")
def list_invoices(db: Session = Depends(get_db), patient_id: int | None = None,
                  status_filter: str | None = None, from_date: date | None = None,
                  to_date: date | None = None, pending_only: bool = False, limit: int = Query(100, le=500),
                  user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.Invoice)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.Invoice.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("invoices:read"):
        raise HTTPException(status_code=403, detail="Missing permission: invoices:read")
    if patient_id:
        stmt = stmt.where(models.Invoice.patient_id == patient_id)
    if status_filter:
        stmt = stmt.where(models.Invoice.status == status_filter.upper())
    if pending_only:
        stmt = stmt.where(models.Invoice.balance_amount > 0)
    if from_date:
        stmt = stmt.where(func.date(models.Invoice.issued_at) >= from_date)
    if to_date:
        stmt = stmt.where(func.date(models.Invoice.issued_at) <= to_date)
    rows = db.scalars(stmt.order_by(models.Invoice.issued_at.desc()).limit(limit)).all()
    return [_out(db, i) for i in rows]


@router.post("", response_model=schemas.InvoiceOut, status_code=201, summary="Create an invoice")
def create_invoice(payload: schemas.InvoiceCreate, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("invoices:write"))):
    patient = db.get(models.Patient, payload.patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    if not payload.items:
        raise HTTPException(status_code=422, detail="At least one invoice item is required")
    invoice = billing.create_invoice(
        db, patient=patient,
        items=[i.model_dump() for i in payload.items],
        appointment_id=payload.appointment_id, consultation_id=payload.consultation_id,
        doctor_id=payload.doctor_id, discount_amount=payload.discount_amount,
        discount_reason=payload.discount_reason, tax_percent=payload.tax_percent,
        notes=payload.notes, due_in_days=payload.due_in_days, issued_by=user.id,
    )
    audit(db, action="INVOICE_CREATE", resource="invoice", resource_id=invoice.id, user=user,
          request=request, new_value={"number": invoice.invoice_number,
                                      "total": float(invoice.total_amount),
                                      "patient_id": patient.id})
    return _out(db, invoice)


@router.post("/from-appointment/{appointment_id}", response_model=schemas.InvoiceOut,
             summary="Ensure the consultation invoice exists for an appointment")
def from_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_permission("invoices:write"))):
    appointment = db.get(models.Appointment, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    doctor = db.get(models.Doctor, appointment.doctor_id)
    invoice = billing.ensure_consultation_invoice(db, appointment, doctor)
    audit(db, action="INVOICE_FROM_APPOINTMENT", resource="invoice", resource_id=invoice.id, user=user,
          request=request, new_value={"appointment_id": appointment_id})
    return _out(db, invoice)


@router.get("/{invoice_id}", response_model=schemas.InvoiceOut, summary="Invoice detail")
def invoice_detail(invoice_id: int, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or invoice.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return _out(db, invoice)


@router.post("/{invoice_id}/discount", response_model=schemas.InvoiceOut, summary="Apply a discount")
def apply_discount(invoice_id: int, request: Request, discount_amount: float, reason: str | None = None,
                   db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("invoices:write"))):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if discount_amount < 0:
        raise HTTPException(status_code=422, detail="Discount cannot be negative")
    before = {"discount": float(invoice.discount_amount), "total": float(invoice.total_amount)}
    invoice.discount_amount = billing.d(discount_amount)
    invoice.discount_reason = reason
    billing.sync_invoice(db, invoice)
    audit(db, action="INVOICE_DISCOUNT", resource="invoice", resource_id=invoice_id, user=user,
          request=request, previous_value=before,
          new_value={"discount": float(invoice.discount_amount), "total": float(invoice.total_amount),
                     "reason": reason})
    return _out(db, invoice)


@router.post("/{invoice_id}/tax", response_model=schemas.InvoiceOut, summary="Set tax percent")
def set_tax(invoice_id: int, request: Request, tax_percent: float, db: Session = Depends(get_db),
            user: CurrentUser = Depends(require_permission("invoices:write"))):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice.tax_percent = billing.d(tax_percent)
    billing.sync_invoice(db, invoice)
    audit(db, action="INVOICE_TAX", resource="invoice", resource_id=invoice_id, user=user, request=request,
          new_value={"tax_percent": tax_percent})
    return _out(db, invoice)


@router.post("/{invoice_id}/cancel", response_model=schemas.InvoiceOut, summary="Cancel an invoice")
def cancel_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_permission("invoices:write"))):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice.status = "CANCELLED"
    db.commit()
    audit(db, action="INVOICE_CANCEL", resource="invoice", resource_id=invoice_id, user=user, request=request)
    return _out(db, invoice)


@router.get("/{invoice_id}/receipt", summary="Download invoice / receipt PDF")
@router.get("/{invoice_id}/pdf", include_in_schema=False)   # friendly alias
def receipt(invoice_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or invoice.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    path = documents.invoice_pdf(db, invoice)
    audit(db, action="INVOICE_RECEIPT", resource="invoice", resource_id=invoice_id, user=user)
    return FileResponse(path, media_type="application/pdf", filename=f"{invoice.invoice_number}.pdf")


@router.post("/{invoice_id}/remind", summary="Send a payment reminder")
def send_reminder(invoice_id: int, request: Request, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(require_permission("notifications:write", "invoices:write",
                                                                 any_of=True))):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    patient = db.get(models.Patient, invoice.patient_id)
    if invoice.balance_amount <= 0:
        return {"success": False, "message": "Invoice is already settled"}
    notify.notify_patient(db, patient, "PAYMENT_REMINDER", {
        "patient_name": patient.full_name, "amount": f"{float(invoice.balance_amount):.2f}",
        "invoice": invoice.invoice_number,
    }, channels=["WHATSAPP", "EMAIL", "IN_APP"])
    audit(db, action="INVOICE_REMIND", resource="invoice", resource_id=invoice_id, user=user, request=request)
    return {"success": True, "message": "Reminder sent", "balance": float(invoice.balance_amount)}
