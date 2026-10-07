"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


# ============================== AUTH ==============================
class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=3, max_length=150)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["PATIENT", "DOCTOR", "RECEPTIONIST", "ACCOUNTANT", "ADMIN"] = "PATIENT"
    # patient self-registration profile
    date_of_birth: date | None = None
    gender: str | None = None
    blood_group: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    emergency_contact_relation: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_at: datetime | None = None
    user: "UserOut | None" = None
    permissions: list[str] = []


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# ============================== USERS / RBAC ==============================
class RoleOut(ORMModel):
    id: int
    name: str
    description: str | None = None
    permissions: list["PermissionOut"] = []


class PermissionOut(ORMModel):
    id: int
    code: str
    module: str
    description: str | None = None


class UserOut(ORMModel):
    id: int
    uuid: str
    full_name: str
    email: EmailStr
    phone: str | None = None
    role_name: str | None = None
    is_active: bool
    is_verified: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    phone: str | None = None
    password: str = Field(min_length=8)
    role: Literal["SUPER_ADMIN", "ADMIN", "DOCTOR", "RECEPTIONIST", "ACCOUNTANT", "PATIENT"]


class UserUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    is_verified: bool | None = None
    role: str | None = None


class SessionOut(ORMModel):
    id: int
    session_key: str
    ip_address: str | None = None
    user_agent: str | None = None
    device: str | None = None
    is_revoked: bool
    created_at: datetime
    last_seen_at: datetime | None = None
    expires_at: datetime


class LoginActivityOut(ORMModel):
    id: int
    email_attempted: str
    status: str
    reason: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    attempt_at: datetime


class AuditLogOut(ORMModel):
    id: int
    user_email: str | None = None
    user_role: str | None = None
    action: str
    resource: str
    resource_id: str | None = None
    previous_value: Any = None
    new_value: Any = None
    ip_address: str | None = None
    created_at: datetime


# ============================== PATIENTS ==============================
class PatientBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=150)
    date_of_birth: date | None = None
    age: int | None = None
    gender: str | None = None
    blood_group: str | None = None
    phone: str = Field(min_length=6, max_length=20)
    alternate_phone: str | None = None
    email: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    emergency_contact_relation: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None
    notes: str | None = None


class PatientCreate(PatientBase):
    create_portal_login: bool = False
    portal_password: str | None = None


class PatientUpdate(BaseModel):
    full_name: str | None = None
    date_of_birth: date | None = None
    age: int | None = None
    gender: str | None = None
    blood_group: str | None = None
    phone: str | None = None
    alternate_phone: str | None = None
    email: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    emergency_contact_relation: str | None = None
    allergies: str | None = None
    chronic_conditions: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class PatientOut(ORMModel, PatientBase):
    id: int
    patient_code: str
    user_id: int | None = None
    is_active: bool = True
    created_at: datetime | None = None


class PatientProfileOut(PatientOut):
    upcoming_appointments: list["AppointmentOut"] = []
    previous_appointments: list["AppointmentOut"] = []
    consultations: list["ConsultationOut"] = []
    medical_records: list["MedicalRecordOut"] = []
    prescriptions: list["PrescriptionOut"] = []
    invoices: list["InvoiceOut"] = []
    payments: list["PaymentOut"] = []
    reviews: list["ReviewOut"] = []
    files: list["MedicalFileOut"] = []
    ai_conversations: list["AIConversationOut"] = []
    stats: dict[str, Any] = {}


# ============================== DOCTORS / SPECIALTIES ==============================
class SpecialtyBase(BaseModel):
    code: str
    name: str
    description: str | None = None
    consultation_fee: float = 0
    is_active: bool = True


class SpecialtyCreate(SpecialtyBase):
    concerns: list[str] = []


class SpecialtyOut(ORMModel, SpecialtyBase):
    id: int
    doctor_count: int = 0
    concerns: list[str] = []
    appointment_demand: int = 0


class SpecialtyConcernCreate(BaseModel):
    keyword: str
    weight: int = 1


class DoctorBase(BaseModel):
    full_name: str
    specialty_id: int
    email: str | None = None
    phone: str | None = None
    qualifications: str | None = None
    experience_years: int = 0
    registration_no: str | None = None
    bio: str | None = None
    languages: str | None = "Hindi, English"
    consultation_fee: float = 0
    follow_up_fee: float = 0
    slot_duration_minutes: int = 30
    daily_capacity: int = 24
    room_number: str | None = None
    is_available_for_ai_booking: bool = True
    is_active: bool = True


class DoctorCreate(DoctorBase):
    create_login: bool = False
    password: str | None = None


class DoctorUpdate(BaseModel):
    full_name: str | None = None
    specialty_id: int | None = None
    email: str | None = None
    phone: str | None = None
    qualifications: str | None = None
    experience_years: int | None = None
    bio: str | None = None
    consultation_fee: float | None = None
    follow_up_fee: float | None = None
    slot_duration_minutes: int | None = None
    daily_capacity: int | None = None
    room_number: str | None = None
    is_available_for_ai_booking: bool | None = None
    is_active: bool | None = None


class DoctorOut(ORMModel, DoctorBase):
    id: int
    doctor_code: str
    user_id: int | None = None
    specialty_name: str | None = None
    rating_avg: float | None = 0
    rating_count: int | None = 0


class DoctorScheduleBase(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    break_start: time | None = None
    break_end: time | None = None
    slot_duration_minutes: int = 30
    capacity_per_slot: int = 1
    is_active: bool = True


class DoctorScheduleCreate(DoctorScheduleBase):
    pass


class DoctorScheduleOut(ORMModel, DoctorScheduleBase):
    id: int
    doctor_id: int


class DoctorLeaveCreate(BaseModel):
    leave_type: Literal["FULL_DAY", "PARTIAL", "HOLIDAY", "EMERGENCY"] = "FULL_DAY"
    start_date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = None


class DoctorLeaveOut(ORMModel):
    id: int
    doctor_id: int
    leave_type: str
    start_date: date
    end_date: date
    start_time: time | None = None
    end_time: time | None = None
    reason: str | None = None
    status: str
    slots_blocked: int = 0


class HolidayCreate(BaseModel):
    name: str
    holiday_date: date
    is_full_day: bool = True
    description: str | None = None


class HolidayOut(ORMModel):
    id: int
    name: str
    holiday_date: date
    is_full_day: bool
    description: str | None = None


# ============================== SLOTS / APPOINTMENTS ==============================
class SlotOut(ORMModel):
    id: int
    doctor_id: int
    slot_date: date
    start_time: time
    end_time: time
    capacity: int
    booked_count: int
    status: str
    blocked_reason: str | None = None
    hold_expires_at: datetime | None = None
    label: str | None = None


class AvailableSlotOut(BaseModel):
    slot_id: int
    doctor_id: int
    doctor_name: str
    specialty_name: str | None = None
    label: str
    slot_date: date
    start_time: time
    end_time: time
    fee: float


class SlotGenerateRequest(BaseModel):
    doctor_id: int | None = None
    days_ahead: int = 7
    start_date: date | None = None


class SlotHoldRequest(BaseModel):
    slot_id: int
    patient_id: int | None = None
    conversation_id: int | None = None


class SlotHoldOut(BaseModel):
    slot_id: int
    hold_token: str
    expires_at: datetime
    message: str


class AppointmentCreate(BaseModel):
    patient_id: int
    doctor_id: int | None = None
    specialty_id: int | None = None
    slot_id: int | None = None
    appointment_date: date | None = None
    start_time: time | None = None
    reason: str | None = None
    source: Literal["WEB", "RECEPTION", "AI", "PHONE"] = "RECEPTION"
    hold_token: str | None = None
    follow_up_of: int | None = None
    send_notifications: bool = True


class AppointmentReschedule(BaseModel):
    new_slot_id: int
    reason: str | None = None
    send_notifications: bool = True


class AppointmentCancel(BaseModel):
    reason: str | None = None
    send_notifications: bool = True


class AppointmentStatusUpdate(BaseModel):
    status: Literal["PENDING", "CONFIRMED", "COMPLETED", "CANCELLED", "NO_SHOW", "RESCHEDULED"]


class AppointmentOut(ORMModel):
    id: int
    appointment_code: str
    patient_id: int
    patient_name: str | None = None
    patient_code: str | None = None
    patient_phone: str | None = None
    doctor_id: int
    doctor_name: str | None = None
    specialty_name: str | None = None
    appointment_date: date
    start_time: time
    end_time: time | None = None
    token_number: int | None = None
    status: str
    queue_status: str
    source: str
    reason: str | None = None
    consultation_id: int | None = None
    invoice_id: int | None = None
    checked_in_at: datetime | None = None
    checked_out_at: datetime | None = None
    created_at: datetime | None = None


class QueueAction(BaseModel):
    appointment_id: int
    note: str | None = None


class QueueEntry(BaseModel):
    appointment_id: int
    appointment_code: str
    token_number: int | None = None
    patient_id: int
    patient_name: str
    patient_phone: str | None = None
    doctor_name: str
    appointment_date: date
    start_time: time
    status: str
    queue_status: str
    waiting_minutes: int | None = None


# ============================== CLINICAL ==============================
class ConsultationCreate(BaseModel):
    appointment_id: int | None = None
    patient_id: int
    doctor_id: int | None = None
    chief_complaint: str | None = None
    vitals: dict[str, Any] | None = None


class ConsultationUpdate(BaseModel):
    chief_complaint: str | None = None
    vitals: dict[str, Any] | None = None
    observations: str | None = None
    diagnosis: str | None = None
    clinical_notes: str | None = None
    advice: str | None = None
    follow_up_required: bool | None = None
    follow_up_date: date | None = None
    follow_up_notes: str | None = None
    status: Literal["IN_PROGRESS", "COMPLETED", "CANCELLED"] | None = None


class ConsultationOut(ORMModel):
    id: int
    consultation_code: str
    appointment_id: int | None = None
    patient_id: int
    doctor_id: int
    doctor_name: str | None = None
    patient_name: str | None = None
    status: str
    chief_complaint: str | None = None
    vitals: dict[str, Any] | None = None
    observations: str | None = None
    diagnosis: str | None = None
    clinical_notes: str | None = None
    advice: str | None = None
    follow_up_required: bool = False
    follow_up_date: date | None = None
    follow_up_notes: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class MedicalRecordCreate(BaseModel):
    patient_id: int
    consultation_id: int | None = None
    appointment_id: int | None = None
    doctor_id: int | None = None
    record_type: str = "NOTE"
    title: str
    description: str | None = None
    values_json: dict[str, Any] | None = None
    severity: str = "NORMAL"
    is_visible_to_patient: bool = True


class MedicalRecordOut(ORMModel, MedicalRecordCreate):
    id: int
    recorded_at: datetime
    version: int = 1


class MedicalFileOut(ORMModel):
    id: int
    patient_id: int
    category: str
    title: str
    original_name: str
    mime_type: str | None = None
    size_bytes: int = 0
    access_level: str
    notes: str | None = None
    uploaded_by: int | None = None
    created_at: datetime | None = None


class MedicineBase(BaseModel):
    name: str
    generic_name: str | None = None
    form: str | None = None
    strength: str | None = None
    manufacturer: str | None = None
    default_dosage: str | None = None
    default_frequency: str | None = None
    default_duration: str | None = None
    instructions: str | None = None
    is_active: bool = True


class MedicineOut(ORMModel, MedicineBase):
    id: int


class PrescriptionItemCreate(BaseModel):
    medicine_id: int | None = None
    medicine_name: str
    dosage: str | None = None
    frequency: str | None = None
    duration: str | None = None
    instructions: str | None = None
    quantity: int = 1


class PrescriptionCreate(BaseModel):
    consultation_id: int | None = None
    appointment_id: int | None = None
    patient_id: int
    doctor_id: int | None = None
    diagnosis_summary: str | None = None
    notes: str | None = None
    advice: str | None = None
    valid_days: int = 30
    items: list[PrescriptionItemCreate] = []
    notify_patient: bool = True


class PrescriptionOut(ORMModel):
    id: int
    prescription_code: str
    consultation_id: int | None = None
    patient_id: int
    patient_name: str | None = None
    doctor_id: int
    doctor_name: str | None = None
    diagnosis_summary: str | None = None
    notes: str | None = None
    advice: str | None = None
    status: str
    pdf_url: str | None = None
    issued_at: datetime
    valid_until: date | None = None
    items: list["PrescriptionItemOut"] = []


class PrescriptionItemOut(ORMModel):
    id: int
    medicine_id: int | None = None
    medicine_name: str
    dosage: str | None = None
    frequency: str | None = None
    duration: str | None = None
    instructions: str | None = None
    quantity: int = 1


# ============================== BILLING ==============================
class InvoiceItemCreate(BaseModel):
    item_type: str = "CONSULTATION"
    description: str
    quantity: int = 1
    unit_price: float


class InvoiceCreate(BaseModel):
    patient_id: int
    appointment_id: int | None = None
    consultation_id: int | None = None
    doctor_id: int | None = None
    items: list[InvoiceItemCreate] = []
    discount_amount: float = 0
    discount_reason: str | None = None
    tax_percent: float = 0
    notes: str | None = None
    due_in_days: int = 7


class InvoiceOut(ORMModel):
    id: int
    invoice_number: str
    patient_id: int
    patient_name: str | None = None
    appointment_id: int | None = None
    doctor_id: int | None = None
    subtotal: float
    discount_amount: float
    tax_percent: float
    tax_amount: float
    total_amount: float
    paid_amount: float
    balance_amount: float
    refunded_amount: float
    status: str
    notes: str | None = None
    issued_at: datetime
    due_date: date | None = None
    items: list["InvoiceItemOut"] = []


class InvoiceItemOut(ORMModel):
    id: int
    item_type: str
    description: str
    quantity: int
    unit_price: float
    amount: float


class PaymentCreate(BaseModel):
    invoice_id: int | None = None
    patient_id: int | None = None
    amount: float
    method: Literal["CASH", "UPI", "CARD", "ONLINE"] = "CASH"
    status: Literal["PENDING", "PAID", "FAILED"] = "PAID"
    transaction_reference: str | None = None
    gateway: str | None = None
    notes: str | None = None
    notify_patient: bool = True


class PaymentOut(ORMModel):
    id: int
    payment_code: str
    invoice_id: int | None = None
    invoice_number: str | None = None
    patient_id: int
    patient_name: str | None = None
    amount: float
    method: str
    status: str
    transaction_reference: str | None = None
    paid_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None


class RefundCreate(BaseModel):
    payment_id: int | None = None
    amount: float | None = None
    reason: str | None = None


class RefundOut(ORMModel):
    id: int
    refund_code: str
    payment_id: int
    invoice_id: int | None = None
    amount: float
    reason: str | None = None
    status: str
    created_at: datetime | None = None


# ============================== ENGAGEMENT ==============================
class ReviewCreate(BaseModel):
    doctor_id: int | None = None
    appointment_id: int | None = None
    review_type: Literal["DOCTOR", "CLINIC"] = "DOCTOR"
    rating: int = Field(ge=1, le=5)
    title: str | None = None
    comment: str | None = None


class ReviewModerate(BaseModel):
    status: Literal["APPROVED", "REJECTED", "PENDING"]
    admin_note: str | None = None


class ReviewOut(ORMModel):
    id: int
    patient_id: int
    patient_name: str | None = None
    doctor_id: int | None = None
    doctor_name: str | None = None
    appointment_id: int | None = None
    review_type: str
    rating: int
    title: str | None = None
    comment: str | None = None
    status: str
    admin_note: str | None = None
    created_at: datetime | None = None


class NotificationCreate(BaseModel):
    patient_id: int | None = None
    user_id: int | None = None
    channel: Literal["WHATSAPP", "EMAIL", "IN_APP", "SMS"] = "IN_APP"
    template: str = "CUSTOM"
    subject: str | None = None
    message: str
    payload: dict[str, Any] | None = None
    send_now: bool = True


class NotificationOut(ORMModel):
    id: int
    user_id: int | None = None
    patient_id: int | None = None
    channel: str
    template: str
    subject: str | None = None
    message: str
    status: str
    retry_count: int = 0
    error: str | None = None
    scheduled_at: datetime | None = None
    sent_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime | None = None


class WhatsappOut(ORMModel):
    id: int
    to_number: str
    template: str
    body: str
    provider: str
    status: str
    attempts: int = 0
    error: str | None = None
    provider_message_id: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime | None = None


# ============================== AI ==============================
class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None
    session_id: str | None = None
    channel: Literal["WEB", "WHATSAPP", "KIOSK"] = "WEB"
    phone: str | None = None
    name: str | None = None
    # structured slot selection coming back from the UI
    selected_slot_id: int | None = None
    hold_token: str | None = None


class AIConcernOut(BaseModel):
    concern_text: str
    keywords: list[str] = []
    specialty_id: int | None = None
    specialty_name: str | None = None
    urgency: str = "LOW"
    severity: str = "MODERATE"


class AIChatResponse(BaseModel):
    conversation_id: int
    session_id: str
    reply: str
    intent: str
    confidence: float
    stage: str
    patient: dict[str, Any] | None = None
    concern: AIConcernOut | None = None
    specialty: dict[str, Any] | None = None
    doctors: list[dict[str, Any]] = []
    slots: list[AvailableSlotOut] = []
    appointment: dict[str, Any] | None = None
    prescriptions: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    escalation: dict[str, Any] | None = None
    safety: dict[str, Any] | None = None
    tools_used: list[str] = []
    quick_replies: list[str] = []
    latency_ms: int = 0
    # How the message was understood: which layer read it (local dictionary,
    # Groq semantic reader or embeddings), the language and the sentiment signal.
    # Purely informational - shown in the staff portal and the AI monitor.
    understanding: dict[str, Any] | None = None


class AIConversationOut(ORMModel):
    id: int
    session_id: str
    patient_id: int | None = None
    patient_name: str | None = None
    channel: str
    status: str
    patient_type: str | None = None
    primary_intent: str | None = None
    concern_summary: str | None = None
    summary: str | None = None
    message_count: int = 0
    tool_call_count: int = 0
    booking_attempts: int = 0
    successful_bookings: int = 0
    failed_bookings: int = 0
    escalation_count: int = 0
    error_count: int = 0
    avg_response_ms: int | None = 0
    started_at: datetime
    last_message_at: datetime | None = None


class AIMessageOut(ORMModel):
    id: int
    conversation_id: int
    role: str
    content: str
    intent: str | None = None
    confidence: float | None = None
    tool_calls: Any = None
    structured: Any = None
    latency_ms: int | None = None
    safety_flag: str | None = None
    created_at: datetime


class AIToolOut(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = {}
    category: str = "general"


class AIToolInvoke(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class AIEscalationOut(ORMModel):
    id: int
    conversation_id: int
    patient_id: int | None = None
    level: str
    reason: str
    detail: str | None = None
    status: str
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None


class AISummaryOut(ORMModel):
    id: int
    patient_id: int
    conversation_id: int | None = None
    summary_type: str
    summary: str
    context_json: Any = None
    generated_by: str
    created_at: datetime | None = None


# ============================== ANALYTICS / DASHBOARD ==============================
class DashboardMetrics(BaseModel):
    generated_at: datetime
    cards: dict[str, Any]
    charts: dict[str, Any] = {}


class Paginated(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[Any]
