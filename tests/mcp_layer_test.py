"""Tests for the MCP layer: registry contract, RBAC (5/3/2), hallucination
control ("db not available -> not generate -> no") and the grounded chatbot.

The hospital app is MySQL-only at runtime; these tests swap in an in-memory
SQLite session so tool SQL logic can be exercised without a server.  Any
tool that intentionally uses MySQL-only SQL is still covered: its failure
must come back as a refusal envelope, never as invented data.

Run:  ALLOW_DB_DOWNTIME=1 python3 -m pytest tests/mcp_layer_test.py -q
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ALLOW = {"ALLOW_DB_DOWNTIME": "1"}


@pytest.fixture(autouse=True)
def _db_env(monkeypatch):
    for k, v in ALLOW.items():
        monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------
# SQLite-backed session stand-in
# --------------------------------------------------------------------------
@pytest.fixture()
def sqlite_db(monkeypatch):
    """Replace guard.session_scope with a SQLite-backed one (models stay real)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    from app import models  # noqa: F401 - register mappers
    from mcp_layer import guard

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    @contextlib.contextmanager
    def fake_session_scope():
        db = TestSession()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(guard, "session_scope", fake_session_scope)
    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": True, "detail": "sqlite test", "checked_at": 0})
    monkeypatch.setattr(guard, "reset_probe_cache", lambda: None)

    db = TestSession()
    yield db
    db.close()


@pytest.fixture()
def seed_hospital(sqlite_db):
    """Minimal hospital dataset used by the tool tests."""
    from datetime import date, time

    from app import models

    db = sqlite_db
    spec = models.Specialty(code="CARD", name="Cardiology", consultation_fee=800)
    db.add(spec)
    db.flush()
    doc = models.Doctor(doctor_code="DOC1", full_name="Dr. Arjun Mehra", specialty_id=spec.id,
                        consultation_fee=800, slot_duration_minutes=30)
    pat = models.Patient(patient_code="PAT001", full_name="Ramesh Yadav", age=45,
                         phone="9876500001", gender="Male", blood_group="B+")
    pat2 = models.Patient(patient_code="PAT002", full_name="Sunita Sharma", age=32,
                          phone="9876500002", gender="Female")
    db.add_all([doc, pat, pat2])
    db.flush()
    slot = models.Slot(doctor_id=doc.id, slot_date=date.today(), start_time=time(10, 0),
                       end_time=time(10, 30), capacity=1, booked_count=0, status="AVAILABLE")
    inv = models.Invoice(invoice_number="INV0001", patient_id=pat.id, subtotal=500,
                         total_amount=500, paid_amount=200, balance_amount=300, status="PARTIAL")
    db.add_all([slot, inv])
    db.commit()
    return {"doctor": doc, "patient": pat, "patient2": pat2, "slot": slot, "invoice": inv}


def superadmin():
    from mcp_layer.rbac import Principal

    return Principal(subject="sa@test", role="SUPER_ADMIN", tier="SYSTEM", user_id=1, auth="jwt")


def patient_user(user_id=77):
    from mcp_layer.rbac import Principal

    return Principal(subject="p@test", role="PATIENT", tier="USER", user_id=user_id, auth="jwt")


# --------------------------------------------------------------------------
# 1. Registry contract: 10 servers -> 5 USER + 3 ADMIN + 2 SYSTEM
# --------------------------------------------------------------------------
def test_registry_contract():
    from mcp_layer import registry

    assert len(registry.SERVERS) == 10
    counts = registry.rbac_matrix()["counts"]
    assert counts == {"USER": 5, "ADMIN": 3, "SYSTEM": 2}
    assert sum(counts.values()) == 10
    assert registry.tools_total() >= 30

    mods = registry.load_all_modules()
    for key, mod in mods.items():
        assert set(mod.TOOLS) == set(registry.SERVERS_BY_KEY[key].tools), key


def test_tier_boundaries():
    from mcp_layer import registry

    user = [s.key for s in registry.SERVERS if s.tier == "USER"]
    admin = [s.key for s in registry.SERVERS if s.tier == "ADMIN"]
    system = [s.key for s in registry.SERVERS if s.tier == "SYSTEM"]
    assert len(user) == 5 and len(admin) == 3 and len(system) == 2


# --------------------------------------------------------------------------
# 2. RBAC
# --------------------------------------------------------------------------
def test_rbac_matrix_access():
    from mcp_layer.invoke import list_tools_for

    allowed = list_tools_for(patient_user())["allowed"]
    assert [s["key"] for s in allowed] == ["patients", "doctors", "appointments", "prescriptions", "billing"]

    admin_allowed = list_tools_for(superadmin())["allowed"]
    assert len(admin_allowed) == 10


def test_rbac_denied_returns_refusal_not_data():
    from mcp_layer.invoke import invoke

    res = invoke(patient_user(), "security", "list_users", {})
    assert res["answer"] == "no"
    assert res["reason"] == "rbac_denied"
    assert res["generated"] is True  # refusal text is safe to show
    assert res["grounded"] is False  # but it is NOT data


def test_jwt_principal_and_api_key(monkeypatch):
    import jwt as pyjwt

    from mcp_layer.config import settings
    from mcp_layer.rbac import principal_from_api_key, principal_from_headers, principal_from_jwt

    tok = pyjwt.encode({"role": "ADMIN", "email": "a@x.in", "uid": 3}, settings.jwt_secret,
                       algorithm=settings.jwt_algorithm)
    p = principal_from_jwt(tok)
    assert p and p.role == "ADMIN" and p.tier == "ADMIN"

    monkeypatch.setattr(settings, "system_api_key", "topsecret", raising=False)
    p2 = principal_from_api_key("topsecret")
    assert p2 and p2.tier == "SYSTEM"

    p3 = principal_from_headers({"Authorization": f"Bearer {tok}"})
    assert p3.subject == "a@x.in"
    p4 = principal_from_headers({})
    assert p4.tier == "NONE"  # anonymous -> no access


# --------------------------------------------------------------------------
# 3. Hallucination control
# --------------------------------------------------------------------------
def test_db_down_refuses_instead_of_generating(monkeypatch):
    from mcp_layer import guard
    from mcp_layer.invoke import invoke

    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": False, "detail": "boom", "checked_at": 0})
    monkeypatch.setattr(
        guard, "session_scope",
        lambda: (_ for _ in ()).throw(guard.DatabaseUnavailable("down")),
    )
    res = invoke(superadmin(), "patients", "get_patient", {"patient_id": 1})
    assert res["answer"] == "no"
    assert res["generated"] is False
    assert res["grounded"] is False
    assert res["reason"] == "db_unavailable"


def test_tool_exception_is_refusal_not_guess(sqlite_db, monkeypatch):
    from mcp_layer import registry

    mod = registry.load_server_module("patients")
    orig = mod.TOOLS["get_patient"]["fn"]

    def boom(db, **kw):
        raise ValueError("kaboom")

    mod.TOOLS["get_patient"]["fn"] = boom
    try:
        from mcp_layer.invoke import invoke

        res = invoke(superadmin(), "patients", "get_patient", {"patient_id": 1})
    finally:
        mod.TOOLS["get_patient"]["fn"] = orig
    assert res["answer"] == "no" and res["reason"] == "tool_error" and res["generated"] is False


def test_diagnostics_work_during_outage(monkeypatch):
    """ops.mcp_status / ops.db_health are no_db tools: they must keep
    answering WHILE the database is down (they are the outage diagnostics)."""
    from mcp_layer import guard
    from mcp_layer.invoke import invoke

    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": False, "detail": "down", "checked_at": 0})

    res = invoke(superadmin(), "ops", "mcp_status", {})
    assert res["ok"] is True and res["grounded"] is True
    assert res["data"]["tools_total"] >= 30
    assert res["data"]["hallucination_control"]["rule"].startswith("db not available")

    res2 = invoke(superadmin(), "ops", "db_health", {})
    assert res2["data"]["status"] == "down"

    # but a data tool still refuses
    res3 = invoke(superadmin(), "patients", "get_patient", {"patient_id": 1})
    assert res3["answer"] == "no" and res3["generated"] is False


# --------------------------------------------------------------------------
# 4. Tools against real rows (SQLite stand-in)
# --------------------------------------------------------------------------
def test_get_patient_grounded(seed_hospital):
    from mcp_layer.invoke import invoke

    res = invoke(superadmin(), "patients", "get_patient", {"patient_id": seed_hospital["patient"].id})
    assert res["grounded"] is True and res["generated"] is True
    assert res["data"]["full_name"] == "Ramesh Yadav"
    assert res["source"] == "mysql"


def test_get_patient_missing_is_grounded_no(seed_hospital):
    from mcp_layer.invoke import invoke

    res = invoke(superadmin(), "patients", "get_patient", {"patient_id": 99999})
    assert res["grounded"] is True and res["answer"] == "no" and res["data"] is None


def test_patient_scoping_hides_other_patients(seed_hospital, sqlite_db):
    """A PATIENT caller must only ever see their own row."""
    from app import models
    from mcp_layer.invoke import invoke

    # Map the patient principal to PAT001 via user_id
    seed_hospital["patient"].user_id = 77
    sqlite_db.commit()

    res = invoke(patient_user(77), "patients", "get_patient", {"patient_id": seed_hospital["patient2"].id})
    assert res["answer"] == "no", "patient must not read another patient's record"

    res2 = invoke(patient_user(77), "patients", "get_patient", {})
    assert res2["data"]["patient_code"] == "PAT001"


def test_book_appointment_write_path(seed_hospital):
    from mcp_layer.invoke import invoke

    res = invoke(superadmin(), "appointments", "book_appointment",
                 {"patient_id": seed_hospital["patient"].id, "slot_id": seed_hospital["slot"].id,
                  "reason": "chest checkup"})
    assert res["data"]["booked"] is True
    assert res["data"]["appointment"]["status"] == "CONFIRMED"
    assert res["data"]["appointment"]["source"] == "MCP"

    # slot now full
    res2 = invoke(superadmin(), "appointments", "get_available_slots",
                  {"doctor_id": seed_hospital["doctor"].id, "on_date": str(seed_hospital["slot"].slot_date)})
    assert res2["answer"] == "no"


def test_invoice_and_outstanding(seed_hospital):
    from mcp_layer.invoke import invoke

    res = invoke(superadmin(), "billing", "outstanding_invoices", {})
    assert res["data"]["count"] == 1
    assert res["data"]["total_outstanding"] == 300.0

    res2 = invoke(superadmin(), "billing", "get_invoice", {"invoice_number": "INV0001"})
    assert res2["data"]["balance_amount"] == 300.0


# --------------------------------------------------------------------------
# 5. Chatbot
# --------------------------------------------------------------------------
def test_chatbot_refuses_when_db_down(monkeypatch):
    from mcp_layer import chatbot, guard

    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": False, "detail": "down", "checked_at": 0})
    out = chatbot.respond(superadmin(), "show me patient 1")
    assert out["generated"] is False
    assert out["answer"] == "no"
    assert "no" in out["reply"].lower()


def test_chatbot_safety_blocks(monkeypatch):
    from mcp_layer import chatbot, guard

    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": True, "detail": "up", "checked_at": 0})
    out = chatbot.respond(superadmin(), "I have chest pain and sweating")
    assert out["reason"] == "safety_emergency"

    out2 = chatbot.respond(superadmin(), "can you diagnose my rash and prescribe medicine")
    assert out2["reason"] == "safety_medical_advice"


def test_chatbot_greeting_is_static_help_even_if_db_down(monkeypatch):
    """Smalltalk/help is static UI text - it must work without the DB and must
    NOT be a generated 'answer'."""
    from mcp_layer import chatbot, guard

    monkeypatch.setattr(guard, "db_status", lambda force=False: {"ok": False, "detail": "down", "checked_at": 0})
    out = chatbot.respond(superadmin(), "hello")
    assert out["intent"] in ("help", "greeting")
    assert "MCP servers you can access" in out["reply"]


def test_chatbot_grounded_answer_cites_sources(seed_hospital):
    from mcp_layer import chatbot

    out = chatbot.respond(superadmin(), "show outstanding invoices")
    assert out["grounded"] is True and out["generated"] is True
    assert "billing." in " ".join(out["sources"])
    assert "grounded answer" in out["reply"]


def test_chatbot_refuses_rbac_overreach(seed_hospital):
    from mcp_layer import chatbot

    out = chatbot.respond(patient_user(), "list all users and audit log")
    assert out["reason"] == "rbac_denied" or "role_matrix" in out.get("intent", "")


# --------------------------------------------------------------------------
# 6. Wrapper wiring (regression for the closure bug)
# --------------------------------------------------------------------------
def test_each_tool_routes_to_its_own_name(sqlite_db, seed_hospital):
    from mcp_layer.invoke import invoke

    for name in registry_tools("patients"):
        res = invoke(superadmin(), "patients", name, {"patient_id": 1, "query": "ramesh", "limit": 5})
        assert res["tool"] == name, f"{name} routed to {res['tool']}"


def registry_tools(key):
    from mcp_layer import registry

    return registry.SERVERS_BY_KEY[key].tools
