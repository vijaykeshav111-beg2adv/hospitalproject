"""Role + permission catalogue.

This module is the single source of truth for RBAC. The SQL seed file
(sql/03_seed.sql) is generated from exactly these structures, so the database
and the running application always agree.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
ROLES: dict[str, str] = {
    "SUPER_ADMIN": "Platform owner - full control including roles, security and audit",
    "ADMIN": "Hospital administrator - operations, staff, billing, moderation",
    "DOCTOR": "Consulting doctor - clinical records, prescriptions, schedule",
    "RECEPTIONIST": "Front desk - registration, booking, queue, collection",
    "ACCOUNTANT": "Finance - invoices, payments, refunds, revenue analytics",
    "PATIENT": "Patient portal - own appointments, records, files, bills, AI chat",
}

# --------------------------------------------------------------------------
# Permissions  ->  module:action
# --------------------------------------------------------------------------
PERMISSIONS: dict[str, tuple[str, str]] = {
    # security / administration
    "users:read": ("security", "View users"),
    "users:write": ("security", "Create / update / disable users"),
    "roles:manage": ("security", "Manage roles and permission matrix"),
    "session:manage": ("security", "View and revoke user sessions"),
    "audit:read": ("security", "Read audit logs and login activity"),
    "settings:manage": ("security", "Manage system settings"),
    # patients
    "patients:read": ("patients", "View patient profiles and history"),
    "patients:write": ("patients", "Register / update patients"),
    "patients:delete": ("patients", "Archive patients"),
    # doctors & masters
    "doctors:read": ("doctors", "View doctors"),
    "doctors:write": ("doctors", "Create / update doctors"),
    "specialties:read": ("doctors", "View specialties"),
    "specialties:write": ("doctors", "Manage specialties and concern mapping"),
    "schedules:read": ("doctors", "View schedules, leaves, holidays"),
    "schedules:write": ("doctors", "Manage schedules, leaves, holidays"),
    # slots
    "slots:read": ("slots", "View slots and availability"),
    "slots:manage": ("slots", "Generate / hold / release slots"),
    # appointments & queue
    "appointments:read": ("appointments", "View appointments"),
    "appointments:write": ("appointments", "Book / reschedule / confirm appointments"),
    "appointments:cancel": ("appointments", "Cancel appointments"),
    "queue:manage": ("appointments", "Check-in, check-out and queue control"),
    # clinical
    "consultations:read": ("clinical", "View consultations"),
    "consultations:write": ("clinical", "Run consultations and clinical notes"),
    "records:read": ("clinical", "View medical records"),
    "records:write": ("clinical", "Write medical records"),
    "files:read": ("clinical", "View / download medical files"),
    "files:write": ("clinical", "Upload medical files"),
    "medicines:read": ("clinical", "View medicine master"),
    "medicines:write": ("clinical", "Manage medicine master"),
    "prescriptions:read": ("clinical", "View prescriptions"),
    "prescriptions:write": ("clinical", "Issue prescriptions"),
    # billing
    "invoices:read": ("billing", "View invoices"),
    "invoices:write": ("billing", "Create / update invoices"),
    "payments:read": ("billing", "View payments"),
    "payments:write": ("billing", "Collect payments"),
    "refunds:write": ("billing", "Process refunds"),
    # engagement
    "reviews:read": ("engagement", "View reviews"),
    "reviews:write": ("engagement", "Submit reviews"),
    "reviews:moderate": ("engagement", "Moderate reviews"),
    "notifications:read": ("engagement", "View notifications"),
    "notifications:write": ("engagement", "Send / retry notifications"),
    "whatsapp:send": ("engagement", "Send WhatsApp messages"),
    # AI
    "ai:chat": ("ai", "Use the AI front-desk assistant"),
    "ai:admin": ("ai", "AI monitoring, safety and conversation admin"),
    "ai:tools": ("ai", "Introspect and invoke AI tools"),
    # dashboards / analytics
    "analytics:read": ("analytics", "View analytics reports"),
    "dashboard:admin": ("analytics", "Access admin dashboard"),
    "dashboard:doctor": ("analytics", "Access doctor dashboard"),
    "dashboard:reception": ("analytics", "Access reception desk"),
    "dashboard:patient": ("analytics", "Access patient portal"),
}

ALL_PERMISSIONS = list(PERMISSIONS.keys())

# --------------------------------------------------------------------------
# Role -> permission matrix  ("*" = every permission, including future ones)
# --------------------------------------------------------------------------
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "SUPER_ADMIN": ["*"],
    "ADMIN": [
        "users:read", "users:write", "session:manage", "audit:read", "settings:manage",
        "patients:read", "patients:write", "patients:delete",
        "doctors:read", "doctors:write", "specialties:read", "specialties:write",
        "schedules:read", "schedules:write",
        "slots:read", "slots:manage",
        "appointments:read", "appointments:write", "appointments:cancel", "queue:manage",
        "consultations:read", "records:read", "records:write", "files:read", "files:write",
        "medicines:read", "medicines:write", "prescriptions:read",
        "invoices:read", "invoices:write", "payments:read", "payments:write", "refunds:write",
        "reviews:read", "reviews:moderate", "notifications:read", "notifications:write",
        "whatsapp:send", "ai:chat", "ai:admin", "ai:tools",
        "analytics:read", "dashboard:admin", "dashboard:reception", "dashboard:doctor",
    ],
    "DOCTOR": [
        "patients:read", "doctors:read", "specialties:read", "schedules:read",
        "slots:read", "appointments:read", "appointments:write",
        "consultations:read", "consultations:write", "records:read", "records:write",
        "files:read", "files:write", "medicines:read",
        "prescriptions:read", "prescriptions:write",
        "invoices:read", "reviews:read", "notifications:read",
        "ai:chat", "analytics:read", "dashboard:doctor",
    ],
    "RECEPTIONIST": [
        "patients:read", "patients:write", "doctors:read", "specialties:read",
        "schedules:read", "slots:read", "slots:manage",
        "appointments:read", "appointments:write", "appointments:cancel", "queue:manage",
        "records:read", "files:read", "files:write", "prescriptions:read", "medicines:read",
        "invoices:read", "invoices:write", "payments:read", "payments:write",
        "reviews:read", "notifications:read", "notifications:write", "whatsapp:send",
        "ai:chat", "dashboard:reception",
    ],
    "ACCOUNTANT": [
        "patients:read", "doctors:read", "specialties:read",
        "appointments:read", "invoices:read", "invoices:write",
        "payments:read", "payments:write", "refunds:write",
        "reviews:read", "notifications:read", "analytics:read", "dashboard:admin",
    ],
    "PATIENT": [
        "dashboard:patient", "patients:read", "doctors:read", "specialties:read",
        "schedules:read", "slots:read",
        "appointments:read", "appointments:write", "appointments:cancel",
        "consultations:read", "records:read", "files:read", "prescriptions:read",
        "invoices:read", "payments:read", "reviews:read", "reviews:write",
        "notifications:read", "ai:chat",
    ],
}


def permissions_for_role(role: str) -> list[str]:
    granted = ROLE_PERMISSIONS.get(role, [])
    if "*" in granted:
        return list(ALL_PERMISSIONS)
    return granted
