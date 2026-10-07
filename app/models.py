"""SQLAlchemy models for the Vijay Vargiya Group of Hospitals platform.

Every table is MySQL friendly (utf8mb4, InnoDB, JSON columns, DECIMAL money).
`python -m app.tools.export_schema` compiles this metadata into sql/02_schema.sql.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, Time, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base
from .security import utcnow


class TS:
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# ==========================================================================
# SECURITY  /  IDENTITY
# ==========================================================================
class Role(Base, TS):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True)
    name = Column(String(40), unique=True, nullable=False)
    description = Column(String(255))
    is_system = Column(Boolean, default=True, nullable=False)

    permissions = relationship("Permission", secondary="role_permissions", lazy="selectin")


class Permission(Base, TS):
    __tablename__ = "permissions"
    id = Column(Integer, primary_key=True)
    code = Column(String(80), unique=True, nullable=False)
    module = Column(String(40), nullable=False)
    description = Column(String(255))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    id = Column(Integer, primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission_id = Column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),)


class User(Base, TS):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    uuid = Column(String(36), unique=True, nullable=False)
    full_name = Column(String(150), nullable=False)
    email = Column(String(150), unique=True, nullable=False)
    phone = Column(String(20), index=True)
    password_hash = Column(String(255), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    phone_verified = Column(Boolean, default=False, nullable=False)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime)
    last_login_at = Column(DateTime)
    last_login_ip = Column(String(64))
    password_changed_at = Column(DateTime, default=utcnow)

    role = relationship("Role", lazy="joined")

    @property
    def role_name(self) -> str:
        return self.role.name if self.role else "PATIENT"

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > utcnow())


class UserSession(Base, TS):
    """Refresh-token backed session (Session Management)."""
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True)
    session_key = Column(String(64), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash = Column(String(128), nullable=False)
    access_jti = Column(String(64))
    ip_address = Column(String(64))
    user_agent = Column(String(255))
    device = Column(String(120))
    is_revoked = Column(Boolean, default=False, nullable=False)
    revoked_at = Column(DateTime)
    revoked_reason = Column(String(120))
    expires_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, default=utcnow)


class LoginActivity(Base, TS):
    __tablename__ = "login_activity"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    email_attempted = Column(String(150), nullable=False, index=True)
    status = Column(String(20), nullable=False)          # SUCCESS | FAILED | LOCKED | LOGOUT
    reason = Column(String(200))
    ip_address = Column(String(64))
    user_agent = Column(String(255))
    attempt_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False)
    channel = Column(String(20), default="EMAIL")
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime)
    requested_ip = Column(String(64))
    created_at = Column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    user_email = Column(String(150))
    user_role = Column(String(40))
    action = Column(String(80), nullable=False, index=True)
    resource = Column(String(80), nullable=False, index=True)
    resource_id = Column(String(60))
    previous_value = Column(JSON)
    new_value = Column(JSON)
    ip_address = Column(String(64))
    user_agent = Column(String(255))
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class SystemSetting(Base, TS):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    value = Column(String(500))
    description = Column(String(255))


# ==========================================================================
# PATIENTS
# ==========================================================================
class Patient(Base, TS):
    __tablename__ = "patients"
    id = Column(Integer, primary_key=True)
    patient_code = Column(String(20), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    full_name = Column(String(150), nullable=False, index=True)
    date_of_birth = Column(Date)
    age = Column(Integer)
    gender = Column(String(20))
    blood_group = Column(String(8))
    phone = Column(String(20), nullable=False, index=True)
    alternate_phone = Column(String(20))
    email = Column(String(150), index=True)
    address_line = Column(String(255))
    city = Column(String(80))
    state = Column(String(80))
    pincode = Column(String(12))
    emergency_contact_name = Column(String(150))
    emergency_contact_phone = Column(String(20))
    emergency_contact_relation = Column(String(60))
    allergies = Column(Text)
    chronic_conditions = Column(Text)
    notes = Column(Text)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


# ==========================================================================
# DOCTORS / SPECIALTIES / SCHEDULE
# ==========================================================================
class Specialty(Base, TS):
    __tablename__ = "specialties"
    id = Column(Integer, primary_key=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(Text)
    consultation_fee = Column(Numeric(10, 2), default=0)
    is_active = Column(Boolean, default=True, nullable=False)


class SpecialtyConcern(Base, TS):
    """Symptom / concern keyword mapping used by AI specialty routing."""
    __tablename__ = "specialty_concerns"
    id = Column(Integer, primary_key=True)
    specialty_id = Column(Integer, ForeignKey("specialties.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword = Column(String(80), nullable=False, index=True)
    weight = Column(Integer, default=1, nullable=False)
    __table_args__ = (UniqueConstraint("specialty_id", "keyword", name="uq_specialty_keyword"),)


class Doctor(Base, TS):
    __tablename__ = "doctors"
    id = Column(Integer, primary_key=True)
    doctor_code = Column(String(20), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    specialty_id = Column(Integer, ForeignKey("specialties.id"), nullable=False, index=True)
    full_name = Column(String(150), nullable=False, index=True)
    email = Column(String(150))
    phone = Column(String(20))
    qualifications = Column(String(255))
    experience_years = Column(Integer, default=0)
    registration_no = Column(String(60))
    bio = Column(Text)
    languages = Column(String(150), default="Hindi, English")
    consultation_fee = Column(Numeric(10, 2), default=0, nullable=False)
    follow_up_fee = Column(Numeric(10, 2), default=0)
    slot_duration_minutes = Column(Integer, default=30, nullable=False)
    daily_capacity = Column(Integer, default=24, nullable=False)
    room_number = Column(String(20))
    is_available_for_ai_booking = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    rating_avg = Column(Numeric(3, 2), default=0)
    rating_count = Column(Integer, default=0)

    specialty = relationship("Specialty", lazy="joined")


class DoctorSchedule(Base, TS):
    """Recurring weekly working hours + breaks (0 = Monday)."""
    __tablename__ = "doctor_schedules"
    id = Column(Integer, primary_key=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True)
    day_of_week = Column(Integer, nullable=False)          # 0..6
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    break_start = Column(Time)
    break_end = Column(Time)
    slot_duration_minutes = Column(Integer, default=30, nullable=False)
    capacity_per_slot = Column(Integer, default=1, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    __table_args__ = (
        UniqueConstraint("doctor_id", "day_of_week", "start_time", name="uq_doctor_day_start"),
        Index("ix_schedule_doctor_day", "doctor_id", "day_of_week"),
    )


class DoctorLeave(Base, TS):
    __tablename__ = "doctor_leaves"
    id = Column(Integer, primary_key=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True)
    leave_type = Column(String(20), default="FULL_DAY", nullable=False)  # FULL_DAY|PARTIAL|HOLIDAY|EMERGENCY
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False)
    start_time = Column(Time)     # only for PARTIAL
    end_time = Column(Time)
    reason = Column(String(255))
    status = Column(String(20), default="APPROVED", nullable=False)      # PENDING|APPROVED|REJECTED
    approved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    slots_blocked = Column(Integer, default=0)


class Holiday(Base, TS):
    __tablename__ = "holidays"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    holiday_date = Column(Date, nullable=False, unique=True, index=True)
    is_full_day = Column(Boolean, default=True, nullable=False)
    description = Column(String(255))


class Slot(Base, TS):
    """A bookable unit produced by the slot engine."""
    __tablename__ = "slots"
    id = Column(Integer, primary_key=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_date = Column(Date, nullable=False, index=True)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    capacity = Column(Integer, default=1, nullable=False)
    booked_count = Column(Integer, default=0, nullable=False)
    status = Column(String(20), default="AVAILABLE", nullable=False)  # AVAILABLE|HELD|BOOKED|BLOCKED|EXPIRED
    hold_token = Column(String(64), index=True)
    hold_patient_id = Column(Integer, ForeignKey("patients.id", ondelete="SET NULL"))
    hold_expires_at = Column(DateTime)
    blocked_reason = Column(String(120))
    generated_by = Column(String(20), default="SCHEDULER")
    __table_args__ = (
        UniqueConstraint("doctor_id", "slot_date", "start_time", name="uq_slot_doctor_date_start"),
        Index("ix_slot_lookup", "doctor_id", "slot_date", "status"),
    )


# ==========================================================================
# APPOINTMENTS / QUEUE / CLINICAL
# ==========================================================================
class Appointment(Base, TS):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    appointment_code = Column(String(24), unique=True, nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    specialty_id = Column(Integer, ForeignKey("specialties.id"))
    slot_id = Column(Integer, ForeignKey("slots.id", ondelete="SET NULL"))
    appointment_date = Column(Date, nullable=False, index=True)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time)
    token_number = Column(Integer)
    status = Column(String(20), default="PENDING", nullable=False, index=True)
    # PENDING | CONFIRMED | COMPLETED | CANCELLED | NO_SHOW | RESCHEDULED
    queue_status = Column(String(20), default="WAITING", nullable=False, index=True)
    # WAITING | CHECKED_IN | IN_CONSULTATION | COMPLETED | NO_SHOW
    source = Column(String(20), default="RECEPTION", nullable=False)  # WEB|RECEPTION|AI|PHONE
    reason = Column(Text)
    ai_concern_summary = Column(Text)
    booked_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    cancellation_reason = Column(String(255))
    rescheduled_from_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    checked_in_at = Column(DateTime)
    checked_out_at = Column(DateTime)
    reminder_sent_at = Column(DateTime)
    follow_up_of = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    notes = Column(Text)

    patient = relationship("Patient", lazy="joined")
    doctor = relationship("Doctor", lazy="joined")
    specialty = relationship("Specialty", lazy="joined")


class Consultation(Base, TS):
    __tablename__ = "consultations"
    id = Column(Integer, primary_key=True)
    consultation_code = Column(String(24), unique=True, nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    status = Column(String(20), default="IN_PROGRESS", nullable=False)  # IN_PROGRESS|COMPLETED|CANCELLED
    chief_complaint = Column(Text)
    vitals = Column(JSON)
    observations = Column(Text)
    diagnosis = Column(Text)
    clinical_notes = Column(Text)
    advice = Column(Text)
    follow_up_required = Column(Boolean, default=False, nullable=False)
    follow_up_date = Column(Date, index=True)
    follow_up_notes = Column(String(255))
    follow_up_appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    started_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime)


class MedicalRecord(Base, TS):
    """Longitudinal patient record (previous records + history)."""
    __tablename__ = "medical_records"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    consultation_id = Column(Integer, ForeignKey("consultations.id", ondelete="SET NULL"), index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"))
    record_type = Column(String(30), nullable=False)  # CONSULTATION|DIAGNOSIS|OBSERVATION|FOLLOW_UP|VITALS|ALLERGY|NOTE
    title = Column(String(180), nullable=False)
    description = Column(Text)
    values_json = Column(JSON)
    severity = Column(String(20), default="NORMAL")
    is_visible_to_patient = Column(Boolean, default=True, nullable=False)
    entered_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    recorded_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    version = Column(Integer, default=1, nullable=False)
    supersedes_id = Column(Integer, ForeignKey("medical_records.id", ondelete="SET NULL"))


class MedicalFile(Base, TS):
    __tablename__ = "medical_files"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    consultation_id = Column(Integer, ForeignKey("consultations.id", ondelete="SET NULL"))
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    category = Column(String(30), default="OTHER", nullable=False)
    # LAB_REPORT|PRESCRIPTION|SCAN|IMAGE|PDF|DISCHARGE|DOCTOR_DOC|OTHER
    title = Column(String(180), nullable=False)
    original_name = Column(String(255), nullable=False)
    stored_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    mime_type = Column(String(120))
    size_bytes = Column(Integer, default=0)
    checksum = Column(String(64))
    access_level = Column(String(24), default="PATIENT_VISIBLE", nullable=False)
    # PATIENT_VISIBLE | DOCTOR_ONLY | STAFF_ONLY
    notes = Column(String(255))
    is_archived = Column(Boolean, default=False, nullable=False)


class Medicine(Base, TS):
    __tablename__ = "medicines"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, index=True)
    generic_name = Column(String(150))
    form = Column(String(40))         # TABLET | SYRUP | INJECTION ...
    strength = Column(String(60))
    manufacturer = Column(String(120))
    default_dosage = Column(String(60))
    default_frequency = Column(String(60))
    default_duration = Column(String(60))
    instructions = Column(String(255))
    is_active = Column(Boolean, default=True, nullable=False)


class Prescription(Base, TS):
    __tablename__ = "prescriptions"
    id = Column(Integer, primary_key=True)
    prescription_code = Column(String(24), unique=True, nullable=False, index=True)
    consultation_id = Column(Integer, ForeignKey("consultations.id", ondelete="SET NULL"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    diagnosis_summary = Column(Text)
    notes = Column(Text)
    advice = Column(Text)
    status = Column(String(20), default="ISSUED", nullable=False)  # ISSUED|DISPENSED|CANCELLED
    pdf_path = Column(String(500))
    issued_at = Column(DateTime, default=utcnow, nullable=False)
    valid_until = Column(Date)


class PrescriptionItem(Base, TS):
    __tablename__ = "prescription_items"
    id = Column(Integer, primary_key=True)
    prescription_id = Column(Integer, ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id = Column(Integer, ForeignKey("medicines.id", ondelete="SET NULL"))
    medicine_name = Column(String(150), nullable=False)
    dosage = Column(String(60))
    frequency = Column(String(60))
    duration = Column(String(60))
    instructions = Column(String(255))
    quantity = Column(Integer, default=1)


# ==========================================================================
# BILLING / PAYMENTS
# ==========================================================================
class Invoice(Base, TS):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    invoice_number = Column(String(24), unique=True, nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    consultation_id = Column(Integer, ForeignKey("consultations.id", ondelete="SET NULL"))
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"))
    subtotal = Column(Numeric(10, 2), default=0, nullable=False)
    discount_amount = Column(Numeric(10, 2), default=0, nullable=False)
    discount_reason = Column(String(150))
    tax_percent = Column(Numeric(5, 2), default=0, nullable=False)
    tax_amount = Column(Numeric(10, 2), default=0, nullable=False)
    total_amount = Column(Numeric(10, 2), default=0, nullable=False)
    paid_amount = Column(Numeric(10, 2), default=0, nullable=False)
    balance_amount = Column(Numeric(10, 2), default=0, nullable=False)
    refunded_amount = Column(Numeric(10, 2), default=0, nullable=False)
    status = Column(String(20), default="UNPAID", nullable=False, index=True)  # UNPAID|PARTIAL|PAID|REFUNDED|CANCELLED
    notes = Column(Text)
    issued_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    issued_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    due_date = Column(Date)

    patient = relationship("Patient", lazy="joined")
    items = relationship("InvoiceItem", cascade="all, delete-orphan", lazy="selectin")


class InvoiceItem(Base, TS):
    __tablename__ = "invoice_items"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    item_type = Column(String(30), default="CONSULTATION", nullable=False)
    description = Column(String(200), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    unit_price = Column(Numeric(10, 2), default=0, nullable=False)
    amount = Column(Numeric(10, 2), default=0, nullable=False)


class Payment(Base, TS):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    payment_code = Column(String(24), unique=True, nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    method = Column(String(20), default="CASH", nullable=False)   # CASH|UPI|CARD|ONLINE
    status = Column(String(20), default="PENDING", nullable=False, index=True)  # PENDING|PAID|FAILED|REFUNDED
    gateway = Column(String(40))
    gateway_reference = Column(String(120))
    transaction_reference = Column(String(120), index=True)
    collected_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    paid_at = Column(DateTime)
    failure_reason = Column(String(255))
    notes = Column(String(255))

    invoice = relationship("Invoice", lazy="joined")


class Refund(Base, TS):
    __tablename__ = "refunds"
    id = Column(Integer, primary_key=True)
    refund_code = Column(String(24), unique=True, nullable=False)
    payment_id = Column(Integer, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="SET NULL"))
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    reason = Column(String(255))
    status = Column(String(20), default="PROCESSED", nullable=False)  # REQUESTED|PROCESSED|REJECTED
    processed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


# ==========================================================================
# ENGAGEMENT
# ==========================================================================
class Review(Base, TS):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"), index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    review_type = Column(String(20), default="DOCTOR", nullable=False)  # DOCTOR|CLINIC
    rating = Column(Integer, nullable=False)
    title = Column(String(150))
    comment = Column(Text)
    status = Column(String(20), default="PENDING", nullable=False, index=True)  # PENDING|APPROVED|REJECTED
    requested_at = Column(DateTime)
    moderated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    moderated_at = Column(DateTime)
    admin_note = Column(String(255))

    patient = relationship("Patient", lazy="joined")
    doctor = relationship("Doctor", lazy="joined")


class Notification(Base, TS):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    channel = Column(String(20), default="IN_APP", nullable=False)  # WHATSAPP|EMAIL|IN_APP|SMS
    template = Column(String(60), nullable=False)
    subject = Column(String(180))
    message = Column(Text, nullable=False)
    payload = Column(JSON)
    status = Column(String(20), default="PENDING", nullable=False, index=True)  # PENDING|SENT|FAILED|RETRYING
    retry_count = Column(Integer, default=0, nullable=False)
    error = Column(String(255))
    provider_reference = Column(String(120))
    scheduled_at = Column(DateTime, index=True)
    sent_at = Column(DateTime)
    read_at = Column(DateTime)

    patient = relationship("Patient", lazy="joined")


class WhatsappMessage(Base, TS):
    __tablename__ = "whatsapp_messages"
    id = Column(Integer, primary_key=True)
    notification_id = Column(Integer, ForeignKey("notifications.id", ondelete="SET NULL"))
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="SET NULL"))
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    to_number = Column(String(24), nullable=False)
    template = Column(String(60), nullable=False)
    body = Column(Text, nullable=False)
    provider = Column(String(30), default="console", nullable=False)
    provider_message_id = Column(String(80))
    status = Column(String(20), default="QUEUED", nullable=False, index=True)  # QUEUED|SENT|DELIVERED|READ|FAILED
    error = Column(String(255))
    attempts = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime)
    sent_at = Column(DateTime)
    delivered_at = Column(DateTime)
    read_at = Column(DateTime)
    raw_response = Column(JSON)


# ==========================================================================
# AI  ASSISTANT
# ==========================================================================
class AIConversation(Base, TS):
    __tablename__ = "ai_conversations"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), unique=True, nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="SET NULL"), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    channel = Column(String(20), default="WEB", nullable=False)  # WEB|WHATSAPP|KIOSK
    status = Column(String(20), default="ACTIVE", nullable=False, index=True)  # ACTIVE|CLOSED|ESCALATED
    patient_type = Column(String(20), default="UNKNOWN")  # NEW|EXISTING|UNKNOWN
    stage = Column(String(30), default="IDLE", nullable=False)
    pending_json = Column(JSON)          # mid-flow selections (doctors, slots, hold token)
    primary_intent = Column(String(40))
    intent_history = Column(JSON, default=list)
    concern_summary = Column(Text)
    summary = Column(Text)
    message_count = Column(Integer, default=0, nullable=False)
    tool_call_count = Column(Integer, default=0, nullable=False)
    booking_attempts = Column(Integer, default=0, nullable=False)
    successful_bookings = Column(Integer, default=0, nullable=False)
    failed_bookings = Column(Integer, default=0, nullable=False)
    escalation_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    avg_response_ms = Column(Integer, default=0)
    started_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    last_message_at = Column(DateTime)
    ended_at = Column(DateTime)

    patient = relationship("Patient", lazy="joined")


class AIMessage(Base, TS):
    __tablename__ = "ai_messages"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(12), nullable=False)  # user|assistant|system|tool
    content = Column(Text, nullable=False)
    intent = Column(String(40))
    confidence = Column(Numeric(4, 2))
    tool_calls = Column(JSON)
    structured = Column(JSON)
    latency_ms = Column(Integer)
    safety_flag = Column(String(30))
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class AIConcern(Base, TS):
    __tablename__ = "ai_concerns"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), index=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="SET NULL"), index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"))
    specialty_id = Column(Integer, ForeignKey("specialties.id", ondelete="SET NULL"))
    concern_text = Column(Text, nullable=False)
    keywords = Column(JSON)
    duration_text = Column(String(80))
    severity = Column(String(20), default="MODERATE")
    urgency = Column(String(20), default="LOW", nullable=False, index=True)  # LOW|MEDIUM|HIGH|EMERGENCY
    status = Column(String(20), default="OPEN", nullable=False)  # OPEN|ADDRESSED|CLOSED
    history_json = Column(JSON)   # concern history trail


class AIToolCall(Base):
    __tablename__ = "ai_tool_calls"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True)
    message_id = Column(Integer, ForeignKey("ai_messages.id", ondelete="SET NULL"))
    tool_name = Column(String(60), nullable=False, index=True)
    arguments = Column(JSON)
    result_summary = Column(String(500))
    status = Column(String(20), default="SUCCESS", nullable=False, index=True)  # SUCCESS|FAILED
    error = Column(String(255))
    duration_ms = Column(Integer)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AIEscalation(Base, TS):
    __tablename__ = "ai_escalations"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="SET NULL"))
    level = Column(String(20), nullable=False)  # RECEPTION|DOCTOR|EMERGENCY
    reason = Column(String(255), nullable=False)
    detail = Column(Text)
    status = Column(String(20), default="OPEN", nullable=False, index=True)  # OPEN|ACKNOWLEDGED|RESOLVED
    assigned_to = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    resolution_notes = Column(Text)
    resolved_at = Column(DateTime)


class AISummary(Base, TS):
    __tablename__ = "ai_summaries"
    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="SET NULL"))
    summary_type = Column(String(30), default="CONTEXT", nullable=False)  # CONTEXT|VISIT|HANDOVER
    summary = Column(Text, nullable=False)
    context_json = Column(JSON)
    generated_by = Column(String(20), default="AI")
    token_estimate = Column(Integer)


class AIRoutingLog(Base):
    """Specialty / doctor routing decision trail (AI routing analytics)."""
    __tablename__ = "ai_routing_log"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True)
    concern_id = Column(Integer, ForeignKey("ai_concerns.id", ondelete="SET NULL"))
    candidate_specialties = Column(JSON)
    selected_specialty_id = Column(Integer, ForeignKey("specialties.id", ondelete="SET NULL"))
    candidate_doctors = Column(JSON)
    selected_doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"))
    match_score = Column(Numeric(4, 2))
    decision = Column(String(200))
    created_at = Column(DateTime, default=utcnow, nullable=False)


# ==========================================================================
# OPERATIONS
# ==========================================================================
class JobRun(Base):
    __tablename__ = "job_runs"
    id = Column(Integer, primary_key=True)
    job_name = Column(String(60), nullable=False, index=True)
    status = Column(String(20), default="RUNNING", nullable=False)
    started_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    finished_at = Column(DateTime)
    duration_ms = Column(Integer)
    details = Column(JSON)
    error = Column(String(500))


class SlotHoldHistory(Base):
    """Audit trail for the slot engine (hold / release / expiry events)."""
    __tablename__ = "slot_hold_history"
    id = Column(Integer, primary_key=True)
    slot_id = Column(Integer, ForeignKey("slots.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String(20), nullable=False)  # HOLD|RELEASE|BOOK|EXPIRE|BLOCK
    actor = Column(String(60))
    conversation_id = Column(Integer, ForeignKey("ai_conversations.id", ondelete="SET NULL"))
    hold_token = Column(String(64))
    created_at = Column(DateTime, default=utcnow, nullable=False)
