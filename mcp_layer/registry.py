"""Registry of the 10 MCP servers and their tools.

The split is fixed by the platform contract:

    5 servers  ->  USER    tier   (patients, doctors, appointments,
                                   prescriptions, billing)
    3 servers  ->  ADMIN   tier   (notifications, reviews, analytics)
    2 servers  ->  SYSTEM  tier   (security, ops)
                                        total 10

Each server lives in mcp_layer/servers/<key>.py and exposes:
    SPEC   -> ServerSpec (this file's dataclass)
    TOOLS  -> {tool_name: {"fn": callable, "description": str, "write": bool}}
    mcp    -> FastMCP instance (tools already registered)

Standalone ports are assigned in registry order (gateway_port+1 ... +10).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerSpec:
    key: str
    title: str
    tier: str                       # USER | ADMIN | SYSTEM
    summary: str
    module: str = ""                # filled by _specs()
    tools: tuple[str, ...] = ()
    write_tools: tuple[str, ...] = ()
    icons: str = "🩺"

    @property
    def server_id(self) -> str:
        return f"{self.tier[:1]}-{self.key}".upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "id": self.server_id,
            "title": self.title,
            "tier": self.tier,
            "summary": self.summary,
            "icons": self.icons,
            "tool_count": len(self.tools),
            "tools": list(self.tools),
            "write_tools": list(self.write_tools),
        }


# --------------------------------------------------------------------------
# The ten servers.  Order defines standalone ports (8101 .. 8110).
# --------------------------------------------------------------------------
SERVERS: tuple[ServerSpec, ...] = (
    # ---------------- USER tier (5) -----------------------------------
    ServerSpec(
        key="patients",
        title="Patient Server",
        tier="USER",
        icons="🧑‍🤝‍🧑",
        summary="Registration, demographics, history and per-patient timeline. "
                "Patients see only their own rows; staff see all.",
        tools=("get_patient", "search_patients", "patient_timeline", "patient_summary", "list_patients"),
    ),
    ServerSpec(
        key="doctors",
        title="Doctor & Specialty Server",
        tier="USER",
        icons="🩺",
        summary="Doctor profiles, specialty master, schedules, leaves and "
                "availability windows.",
        tools=("get_doctor", "search_doctors", "list_specialties", "doctor_availability",
               "doctor_schedule", "list_doctors"),
    ),
    ServerSpec(
        key="appointments",
        title="Appointment & Slot Server",
        tier="USER",
        icons="📅",
        summary="Live slot search, hold/release, booking, cancel, reschedule "
                "and the reception queue.",
        tools=("get_available_slots", "get_appointment", "list_appointments", "book_appointment",
               "cancel_appointment", "reschedule_appointment", "queue_board"),
        write_tools=("book_appointment", "cancel_appointment", "reschedule_appointment"),
    ),
    ServerSpec(
        key="prescriptions",
        title="Prescription & Records Server",
        tier="USER",
        icons="💊",
        summary="Prescriptions with medicine lines, medicine master, "
                "consultations and medical records.",
        tools=("get_prescription", "list_prescriptions", "list_medicines", "get_consultation",
               "list_medical_records"),
    ),
    ServerSpec(
        key="billing",
        title="Billing & Payment Server",
        tier="USER",
        icons="🧾",
        summary="Invoices, line items, payment status, refunds and daily "
                "collection figures.",
        tools=("get_invoice", "list_invoices", "get_payment_status", "list_payments",
               "outstanding_invoices"),
    ),
    # ---------------- ADMIN tier (3) ----------------------------------
    ServerSpec(
        key="notifications",
        title="Notification Server",
        tier="ADMIN",
        icons="🔔",
        summary="WhatsApp / Email / In-App notifications, delivery status and "
                "retries.",
        tools=("list_notifications", "get_notification", "send_notification", "notification_stats"),
        write_tools=("send_notification",),
    ),
    ServerSpec(
        key="reviews",
        title="Review Server",
        tier="ADMIN",
        icons="⭐",
        summary="Post-visit reviews, ratings and moderation queue.",
        tools=("list_reviews", "get_review", "moderate_review", "doctor_rating_summary"),
        write_tools=("moderate_review",),
    ),
    ServerSpec(
        key="analytics",
        title="Analytics & Reports Server",
        tier="ADMIN",
        icons="📊",
        summary="Revenue, appointment, doctor-load, patient-growth and AI "
                "monitoring reports (reporting views).",
        tools=("revenue_report", "appointments_report", "doctor_load", "patient_growth",
               "ai_monitor_summary", "daily_dashboard"),
    ),
    # ---------------- SYSTEM tier (2) --------------------------------
    ServerSpec(
        key="security",
        title="Security & RBAC Server",
        tier="SYSTEM",
        icons="🔐",
        summary="Users, roles, permission matrix, sessions, login activity and "
                "the audit trail.  Super Admin / service accounts only.",
        tools=("list_users", "get_user", "role_matrix", "list_sessions", "audit_log",
               "login_activity"),
    ),
    ServerSpec(
        key="ops",
        title="System & Ops Server",
        tier="SYSTEM",
        icons="🖥️",
        summary="Health probes, DB status, background job runs, system "
                "settings and the MCP layer's own status.",
        tools=("system_health", "db_health", "job_runs", "list_settings", "mcp_status"),
    ),
)

SERVERS_BY_KEY: dict[str, ServerSpec] = {s.key: s for s in SERVERS}

_TIER_COUNTS = {"USER": 0, "ADMIN": 0, "SYSTEM": 0}
for _s in SERVERS:
    _TIER_COUNTS[_s.tier] += 1


def tools_total() -> int:
    return sum(len(s.tools) for s in SERVERS)


def module_for(key: str) -> str:
    return f"{__package__}.servers.{key}"


def load_server_module(key: str):
    """Import mcp_layer.servers.<key> on demand (keeps startup light)."""
    spec = SERVERS_BY_KEY[key]
    return importlib.import_module(module_for(spec.key))


def load_all_modules() -> dict[str, Any]:
    return {key: load_server_module(key) for key in SERVERS_BY_KEY}


def tools_index() -> dict[str, dict]:
    """name -> {server, tier, description, write} for every tool on the platform."""
    index: dict[str, dict] = {}
    for key in SERVERS_BY_KEY:
        mod = load_server_module(key)
        for name, meta in mod.TOOLS.items():
            index[f"{key}.{name}"] = {
                "server": key,
                "tier": SERVERS_BY_KEY[key].tier,
                "tool": name,
                "description": meta.get("description", ""),
                "write": bool(meta.get("write", False)),
            }
    return index


def rbac_matrix() -> dict:
    """The 5 / 3 / 2 matrix plus which hospital roles may reach each tier."""
    from .rbac import TIER_AUDIENCE

    return {
        "counts": dict(_TIER_COUNTS),
        "contract": "10 MCP servers -> 5 USER + 3 ADMIN + 2 SYSTEM -> RBAC",
        "tiers": {
            tier: {
                "servers": [s.key for s in SERVERS if s.tier == tier],
                "hospital_roles": list(TIER_AUDIENCE[tier]),
                "level": {"USER": 10, "ADMIN": 20, "SYSTEM": 30}[tier],
            }
            for tier in ("USER", "ADMIN", "SYSTEM")
        },
    }
