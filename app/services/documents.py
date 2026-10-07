"""PDF documents: prescription and invoice receipt (ReportLab)."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..naming import doctor_label
from ..config import settings
from ..security import utcnow

log = logging.getLogger("vvh.pdf")

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)
    REPORTLAB_OK = True
except Exception:  # noqa: BLE001
    REPORTLAB_OK = False


BRAND = "#0b6e6e" if REPORTLAB_OK else "#000000"


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("VVHTitle", parent=styles["Title"], fontSize=17, textColor=colors.HexColor(BRAND),
                              spaceAfter=2))
    styles.add(ParagraphStyle("VVHSub", parent=styles["Normal"], fontSize=9, textColor=colors.grey, alignment=1))
    styles.add(ParagraphStyle("VVHSection", parent=styles["Heading3"], fontSize=11,
                              textColor=colors.HexColor(BRAND), spaceBefore=8, spaceAfter=4))
    styles.add(ParagraphStyle("VVHSmall", parent=styles["Normal"], fontSize=8.5, textColor=colors.grey))
    return styles


def _header(styles, title: str, subtitle: str) -> list:
    return [
        Paragraph(settings.app_name, styles["VVHTitle"]),
        Paragraph("Multi-speciality clinic &mdash; Jaipur, Rajasthan, India &nbsp;|&nbsp; +91 141 400 0000",
                  styles["VVHSub"]),
        Spacer(1, 6),
        Paragraph(title, styles["VVHSection"]),
        Paragraph(subtitle, styles["VVHSmall"]),
        Spacer(1, 4),
    ]


def prescription_pdf(db: Session, prescription: models.Prescription) -> Path:
    """Render the prescription PDF and store its path on the record."""
    patient = db.get(models.Patient, prescription.patient_id)
    doctor = db.get(models.Doctor, prescription.doctor_id)
    items = db.scalars(select(models.PrescriptionItem).where(
        models.PrescriptionItem.prescription_id == prescription.id)).all()

    out_dir = settings.prescriptions_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prescription.prescription_code}.pdf"

    if not REPORTLAB_OK:
        path.write_text(_plain_prescription(patient, doctor, prescription, items))
        prescription.pdf_path = str(path)
        db.commit()
        return path

    styles = _styles()
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    story = _header(styles, f"Prescription &nbsp;{prescription.prescription_code}",
                    f"Issued: {prescription.issued_at:%d %b %Y %H:%M} &nbsp;|&nbsp; "
                    f"Valid until: {prescription.valid_until or '-'}")

    info = [
        ["Patient", patient.full_name if patient else "-", "Patient ID", patient.patient_code if patient else "-"],
        ["Age / Gender", f"{(patient.age or '-')} / {patient.gender or '-'}" if patient else "-",
         "Phone", patient.phone if patient else "-"],
        ["Doctor", doctor_label(doctor.full_name) if doctor else "-",
         "Specialty", doctor.specialty.name if doctor and doctor.specialty else "-"],
        ["Qualification", (doctor.qualifications if doctor else "-") or "-",
         "Reg. No.", (doctor.registration_no if doctor else "-") or "-"],
    ]
    table = Table(info, colWidths=[26 * mm, 60 * mm, 26 * mm, 52 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor(BRAND)),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor(BRAND)),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [table, Spacer(1, 8)]

    if prescription.diagnosis_summary:
        story += [Paragraph(f"<b>Clinical note:</b> {prescription.diagnosis_summary}", styles["BodyText"])]

    rows = [["#", "Medicine", "Dosage", "Frequency", "Duration", "Instructions"]]
    for idx, item in enumerate(items, start=1):
        rows.append([str(idx), item.medicine_name, item.dosage or "-", item.frequency or "-",
                     item.duration or "-", item.instructions or "-"])
    if len(rows) == 1:
        rows.append(["-", "No medicines prescribed", "-", "-", "-", "-"])
    meds = Table(rows, colWidths=[8 * mm, 46 * mm, 22 * mm, 26 * mm, 24 * mm, 38 * mm])
    meds.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BRAND)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fbfb")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [Paragraph("Rx", styles["VVHSection"]), meds, Spacer(1, 8)]

    if prescription.advice:
        story += [Paragraph("Advice", styles["VVHSection"]),
                  Paragraph(prescription.advice.replace("\n", "<br/>"), styles["BodyText"])]
    if prescription.notes:
        story += [Paragraph("Notes", styles["VVHSection"]),
                  Paragraph(prescription.notes.replace("\n", "<br/>"), styles["BodyText"])]

    story += [
        Spacer(1, 18),
        Paragraph("This prescription is valid only for the patient named above and was generated "
                  "electronically from the hospital management system. Please follow the dosage strictly "
                  "and complete the full course. For any adverse reaction contact the clinic immediately.",
                  styles["VVHSmall"]),
        Spacer(1, 14),
        Paragraph(f"<b>{doctor_label(doctor.full_name) if doctor else ''}</b><br/>"
                  f"{doctor.qualifications if doctor else ''}<br/>Signature", styles["BodyText"]),
    ]
    doc.build(story)
    prescription.pdf_path = str(path)
    db.commit()
    return path


def _plain_prescription(patient, doctor, prescription, items) -> str:
    lines = [
        settings.app_name,
        "=" * 60,
        f"Prescription: {prescription.prescription_code}",
        f"Patient: {patient.full_name if patient else '-'} ({patient.patient_code if patient else '-'})",
        f"Doctor: {doctor_label(doctor.full_name) if doctor else '-'}",
        f"Issued: {prescription.issued_at:%d %b %Y %H:%M}",
        "-" * 60,
        "Rx:",
    ]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.medicine_name} | {item.dosage} | {item.frequency} | {item.duration} | {item.instructions}")
    lines += ["-" * 60, f"Advice: {prescription.advice or '-'}", f"Notes: {prescription.notes or '-'}"]
    return "\n".join(lines)


def invoice_pdf(db: Session, invoice: models.Invoice) -> Path:
    patient = db.get(models.Patient, invoice.patient_id)
    items = db.scalars(select(models.InvoiceItem).where(models.InvoiceItem.invoice_id == invoice.id)).all()
    payments = db.scalars(select(models.Payment).where(models.Payment.invoice_id == invoice.id)).all()
    out_dir = settings.prescriptions_dir / "invoices"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{invoice.invoice_number}.pdf"

    if not REPORTLAB_OK:
        body = [f"{settings.app_name} - Invoice {invoice.invoice_number}",
                f"Patient: {patient.full_name if patient else '-'}",
                f"Total: {invoice.total_amount}  Paid: {invoice.paid_amount}  Balance: {invoice.balance_amount}"]
        body += [f"- {i.description} x{i.quantity} = {i.amount}" for i in items]
        path.write_text("\n".join(body))
        return path

    styles = _styles()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm)
    story = _header(styles, f"Invoice / Receipt &nbsp;{invoice.invoice_number}",
                    f"Issued: {invoice.issued_at:%d %b %Y %H:%M} &nbsp;|&nbsp; "
                    f"Due: {invoice.due_date or '-'} &nbsp;|&nbsp; Status: {invoice.status}")

    info = [
        ["Patient", patient.full_name if patient else "-", "Patient ID", patient.patient_code if patient else "-"],
        ["Phone", patient.phone if patient else "-", "City", (patient.city if patient else "-") or "-"],
    ]
    table = Table(info, colWidths=[24 * mm, 62 * mm, 24 * mm, 54 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor(BRAND)),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor(BRAND)),
    ]))
    story += [table, Spacer(1, 10)]

    rows = [["#", "Description", "Qty", "Rate (Rs)", "Amount (Rs)"]]
    for idx, item in enumerate(items, 1):
        rows.append([str(idx), item.description, str(item.quantity),
                     f"{float(item.unit_price):.2f}", f"{float(item.amount):.2f}"])
    rows += [
        ["", "", "", "Subtotal", f"{float(invoice.subtotal):.2f}"],
        ["", "", "", "Discount", f"-{float(invoice.discount_amount):.2f}"],
        ["", "", "", f"Tax ({float(invoice.tax_percent):.1f}%)", f"{float(invoice.tax_amount):.2f}"],
        ["", "", "", "Total", f"{float(invoice.total_amount):.2f}"],
        ["", "", "", "Paid", f"{float(invoice.paid_amount):.2f}"],
        ["", "", "", "Balance", f"{float(invoice.balance_amount):.2f}"],
    ]
    body = Table(rows, colWidths=[10 * mm, 78 * mm, 14 * mm, 30 * mm, 32 * mm])
    body.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BRAND)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, 5), 0.3, colors.HexColor("#dddddd")),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("BACKGROUND", (3, -3), (-1, -1), colors.HexColor("#eef7f7")),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story += [body, Spacer(1, 10)]

    if payments:
        pay_rows = [["Payment", "Method", "Amount (Rs)", "Status", "Date"]]
        for p in payments:
            pay_rows.append([p.payment_code, p.method, f"{float(p.amount):.2f}", p.status,
                             p.paid_at.strftime("%d %b %Y") if p.paid_at else "-"])
        pay_table = Table(pay_rows, colWidths=[40 * mm, 24 * mm, 28 * mm, 26 * mm, 30 * mm])
        pay_table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b6e6e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
        ]))
        story += [Paragraph("Payments", styles["VVHSection"]), pay_table]

    story += [Spacer(1, 16),
              Paragraph("Thank you for choosing Vijay Vargiya Group of Hospitals. "
                        "This is a computer generated receipt.", styles["VVHSmall"])]
    doc.build(story)
    return path
