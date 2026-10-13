"""MCP server 05 - Billing & Payment Server  [USER tier].

Invoices, invoice lines, payment status, refunds and the grounded
outstanding-balance list.  Patients see only their own bills.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app import models

from ..guard import grounded_no
from ..rbac import current_actor
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["billing"]
TOOLS: dict = {}


def _visible_patient_id(db, requested: int | None) -> int | None:
    """PATIENT callers may only read their own rows."""
    actor = current_actor()
    if actor.role == "PATIENT":
        row = db.scalar(select(models.Patient).where(models.Patient.user_id == actor.user_id))
        own = row.id if row else -1
        if requested is None:
            return own
        return requested if requested == own else -1
    return requested


def _invoice_brief(i: models.Invoice) -> dict:
    return {
        "id": i.id,
        "invoice_number": i.invoice_number,
        "patient": i.patient.full_name if i.patient else None,
        "patient_id": i.patient_id,
        "total_amount": float(i.total_amount or 0),
        "paid_amount": float(i.paid_amount or 0),
        "balance_amount": float(i.balance_amount or 0),
        "status": i.status,
        "issued_at": str(i.issued_at),
        "due_date": str(i.due_date) if i.due_date else None,
    }


@tool("get_invoice", "Look up one invoice (with line items) by id or invoice number.")
def _get_invoice(db, invoice_id: int | None = None, invoice_number: str = ""):
    stmt = select(models.Invoice)
    if invoice_id:
        stmt = stmt.where(models.Invoice.id == invoice_id)
    elif invoice_number.strip():
        stmt = stmt.where(models.Invoice.invoice_number == invoice_number.strip().upper())
    else:
        return grounded_no(server=SPEC.key, tool="get_invoice", what="invoice",
                           note="Provide invoice_id or invoice_number.")
    inv = db.scalar(stmt)
    if inv is None:
        return grounded_no(server=SPEC.key, tool="get_invoice", what="invoice")
    pid = _visible_patient_id(db, inv.patient_id)
    if current_actor().role == "PATIENT" and inv.patient_id != pid:
        return grounded_no(server=SPEC.key, tool="get_invoice", what="invoice")
    items = db.scalars(select(models.InvoiceItem).where(models.InvoiceItem.invoice_id == inv.id)).all()
    brief = _invoice_brief(inv)
    brief["items"] = [{"type": it.item_type, "description": it.description,
                       "quantity": it.quantity, "unit_price": float(it.unit_price or 0),
                       "amount": float(it.amount or 0)} for it in items]
    return brief


@tool("list_invoices", "List invoices for a patient (or all, for staff).")
def _list_invoices(db, patient_id: int | None = None, status: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    pid = _visible_patient_id(db, patient_id)
    stmt = select(models.Invoice)
    if pid and pid > 0:
        stmt = stmt.where(models.Invoice.patient_id == pid)
    elif current_actor().role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="list_invoices", what="invoice")
    if (status or "").strip():
        stmt = stmt.where(models.Invoice.status == status.strip().upper())
    rows = db.scalars(stmt.order_by(models.Invoice.issued_at.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_invoices", what="invoice")
    return [_invoice_brief(i) for i in rows]


@tool("outstanding_invoices", "Unpaid / partially paid invoices with balances.")
def _outstanding_invoices(db, patient_id: int | None = None, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    pid = _visible_patient_id(db, patient_id)
    stmt = select(models.Invoice).where(
        models.Invoice.balance_amount > 0,
        models.Invoice.status.in_(("UNPAID", "PARTIAL")),
    )
    if pid and pid > 0:
        stmt = stmt.where(models.Invoice.patient_id == pid)
    elif current_actor().role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="outstanding_invoices", what="invoice")
    rows = db.scalars(stmt.order_by(models.Invoice.due_date).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="outstanding_invoices", what="outstanding invoice")
    total = sum(float(i.balance_amount or 0) for i in rows)
    return {"count": len(rows), "total_outstanding": total,
            "invoices": [_invoice_brief(i) for i in rows]}


@tool("get_payment_status", "Payment status summary for a patient or an invoice.")
def _get_payment_status(db, patient_id: int | None = None, invoice_id: int | None = None):
    pid = _visible_patient_id(db, patient_id)
    stmt = select(models.Payment)
    if invoice_id:
        stmt = stmt.where(models.Payment.invoice_id == invoice_id)
    elif pid and pid > 0:
        stmt = stmt.where(models.Payment.patient_id == pid)
    elif current_actor().role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="get_payment_status", what="payment")
    rows = db.scalars(stmt.order_by(models.Payment.id.desc()).limit(20)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="get_payment_status", what="payment")
    return [{
        "id": p.id, "payment_code": p.payment_code, "invoice_id": p.invoice_id,
        "amount": float(p.amount or 0), "method": p.method, "status": p.status,
        "paid_at": str(p.paid_at) if p.paid_at else None,
    } for p in rows]


@tool("list_payments", "List payments (staff-wide or filtered by patient).")
def _list_payments(db, patient_id: int | None = None, method: str = "", limit: int = 20):
    limit = max(1, min(int(limit), 100))
    pid = _visible_patient_id(db, patient_id)
    stmt = select(models.Payment)
    if pid and pid > 0:
        stmt = stmt.where(models.Payment.patient_id == pid)
    elif current_actor().role == "PATIENT":
        return grounded_no(server=SPEC.key, tool="list_payments", what="payment")
    if (method or "").strip():
        stmt = stmt.where(models.Payment.method == method.strip().upper())
    rows = db.scalars(stmt.order_by(models.Payment.id.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_payments", what="payment")
    total = sum(float(p.amount or 0) for p in rows if p.status == "PAID")
    return {"payments": [{"id": p.id, "payment_code": p.payment_code, "amount": float(p.amount or 0),
                          "method": p.method, "status": p.status} for p in rows],
            "paid_total": total}


mcp = make_server(
    SPEC,
    "Billing & Payment Server. All money figures come straight from MySQL "
    "DECIMAL columns - rounded only for display.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
