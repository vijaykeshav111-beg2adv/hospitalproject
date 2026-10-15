"""Tests for the chatbot fixes (MCP chatbot router + main AI front desk).

These tests do NOT need MySQL. They replace app.database with a small
in-memory SQLite database, so run this file on its own:

    python -m pytest tests/test_chat_fixes.py -q
"""
import os
import sys
import types
import uuid

import pytest

os.environ.setdefault("ALLOW_DB_DOWNTIME", "1")
os.environ["AI_MODE"] = "local"
os.environ["HOSPITAL_OPD_HOURS"] = "Mon-Sat 9am-5pm"   # test value only
os.environ["HOSPITAL_PHONE"] = ""                       # left empty on purpose
os.environ["HOSPITAL_ADDRESS"] = ""

# ---- swap the MySQL database for in-memory SQLite (before any app import) ----
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
fake_database = types.ModuleType("app.database")
fake_database.Base = Base
fake_database.engine = engine
fake_database.ACTIVE_DATABASE_URL = "sqlite://"
fake_database.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
fake_database.get_db = lambda: None
fake_database.REPORTING_VIEWS = ()
fake_database.init_db = lambda: None
sys.modules["app.database"] = fake_database

from app import models, schemas, seed                      # noqa: E402
from app.services import ai_engine                         # noqa: E402
from mcp_layer import chatbot                              # noqa: E402
from mcp_layer.rbac import ANONYMOUS, Principal            # noqa: E402

patient = Principal(subject="p@x.com", role="PATIENT", tier="USER", user_id=5, auth="jwt")
admin = Principal(subject="a@x.com", role="ADMIN", tier="ADMIN", auth="jwt")


def tools_planned(message, who=patient):
    return [f"{c['server']}.{c['tool']}" for c in chatbot._plan(message, who)]


# ==========================================================================
# MCP chatbot router
# ==========================================================================
def test_symptom_goes_to_doctor_search_not_admin_dashboard():
    assert tools_planned("fever") == ["doctors.search_doctors"]
    assert chatbot._plan("fever", patient)[0]["args"]["query"] == "General Medicine"


def test_typo_gestronomy_goes_to_gastroenterology():
    plan = chatbot._plan("gestronomy", patient)
    assert plan[0]["tool"] == "search_doctors"
    assert plan[0]["args"]["query"] == "Gastroenterology"


def test_general_medicine_is_a_department_not_a_tablet_question():
    for text in ("General Medicine doctors", "gernal medicine", "internal medicine doctor"):
        plan = chatbot._plan(text, patient)
        assert plan[0]["tool"] == "search_doctors", text
        assert plan[0]["args"]["query"] == "General Medicine", text


def test_unknown_text_and_bare_number_never_plan_an_admin_tool():
    assert tools_planned("523") == []
    assert tools_planned("some random words here") == []
    assert tools_planned("523", ANONYMOUS) == []


def test_admin_still_gets_dashboard_as_default():
    assert tools_planned("some random words here", admin) == ["analytics.daily_dashboard"]


def test_whole_word_matching_fixes_false_hits():
    assert tools_planned("I live in delhi") == []                 # was a greeting ("hi ")
    assert tools_planned("is anyone available today") == []       # was medical records ("lab")
    assert tools_planned("show my profile") == []                 # was medical records ("file")


def test_normal_requests_still_route_correctly():
    assert tools_planned("my appointments") == ["appointments.list_appointments"]
    assert tools_planned("show today queue") == ["appointments.queue_board"]
    assert tools_planned("prescription 12") == ["prescriptions.get_prescription"]
    assert tools_planned("book appoint ment")[-1] == "appointments.get_available_slots"
    assert tools_planned("hi, show my bill") == ["billing.get_payment_status"]
    assert tools_planned("my lab report") == ["prescriptions.list_medical_records"]


def test_admin_words_still_reach_admin_tools_and_rbac_stays_strict():
    assert tools_planned("revenue report", admin) == ["analytics.daily_dashboard", "analytics.revenue_report"]
    # a patient who explicitly asks for it still goes to the tool, and RBAC will deny it
    assert tools_planned("daily dashboard") == ["analytics.daily_dashboard", "analytics.revenue_report"]


def test_respond_asks_a_question_for_bare_number_and_unknown_text():
    reply = chatbot.respond(patient, "523")
    assert reply["intent"] == "clarify"
    assert "not sure what it is" in reply["reply"]
    assert chatbot.respond(patient, "asdf qwer zxcv")["intent"] == "clarify"


def test_respond_help_and_safety():
    assert chatbot.respond(patient, "hello")["intent"] == "help"
    assert chatbot.respond(patient, "what can you do")["intent"] == "help"
    assert chatbot.respond(patient, "chest pain")["reason"] == "safety_emergency"
    assert chatbot.respond(patient, "medicine for fever")["reason"] == "safety_medical_advice"


def test_do_i_have_an_appointment_is_not_medical_advice():
    reply = chatbot.respond(patient, "do i have an appointment today")
    assert reply.get("reason") != "safety_medical_advice"
    # but a real diagnosis question is still refused
    assert chatbot.respond(patient, "do i have diabetes")["reason"] == "safety_medical_advice"


def test_anonymous_tier_is_none():
    assert ANONYMOUS.tier == "NONE"
    assert not ANONYMOUS.can_call("USER")


# ==========================================================================
# Doctor search tool (SQLite)
# ==========================================================================
@pytest.fixture(scope="module")
def db():
    Base.metadata.create_all(engine)
    session = fake_database.SessionLocal()
    seed.seed_rbac(session)
    seed.seed_masters(session)
    seed.seed_users_and_doctors(session)
    yield session
    session.close()


def search(db, query):
    from mcp_layer.servers import doctors
    return doctors.TOOLS["search_doctors"]["fn"](db, query=query)


def test_search_doctors_matches_department_name(db):
    rows = search(db, "General Medicine")
    assert isinstance(rows, list) and {r["specialty"] for r in rows} == {"General Medicine"}


def test_search_doctors_fixes_spelling_of_department(db):
    rows = search(db, "orthopedics")          # database spelling is "Orthopaedics"
    assert isinstance(rows, list) and rows[0]["specialty"] == "Orthopaedics"


def test_search_doctors_by_name_and_empty_department(db):
    assert search(db, "Arjun")[0]["full_name"] == "Dr Arjun Mehra"
    # Gastroenterology exists but has no doctor: the answer must be an honest "no"
    result = search(db, "Gastroenterology")
    assert isinstance(result, dict) and result.get("answer") == "no"


# ==========================================================================
# Main AI front desk
# ==========================================================================
@pytest.mark.parametrize("text,intent", [
    ("OPD timing kya hai?", "HOSPITAL_INFO"),
    ("hospital phone number", "HOSPITAL_INFO"),
    ("where is the hospital located", "HOSPITAL_INFO"),
    ("What can you do?", "HELP"),
    ("help", "HELP"),
    ("I want to book appointment", "BOOK_APPOINTMENT"),
    ("hello", "GREETING"),
    ("fever", "PROVIDE_CONCERN"),
])
def test_detect_intent(text, intent):
    assert ai_engine.detect_intent(text, "IDLE", {})[0] == intent


def test_booking_message_that_mentions_hospital_is_still_booking():
    intent = ai_engine.detect_intent("book appointment at the hospital, my phone number is 9876500001", "IDLE", {})[0]
    assert intent == "BOOK_APPOINTMENT"


def chat(db, message):
    payload = schemas.AIChatRequest(message=message, session_id=str(uuid.uuid4()))
    return ai_engine.handle_message(db, payload)


def test_hospital_facts_come_only_from_env(db):
    timing = chat(db, "OPD timing kya hai?")
    assert timing["intent"] == "HOSPITAL_INFO"
    assert "Mon-Sat 9am-5pm" in timing["reply"]

    phone = chat(db, "hospital phone number")
    assert "will not guess" in phone["reply"]          # phone is empty in this test
    assert not any(ch.isdigit() for ch in phone["reply"])   # no invented number


def test_help_reply_and_symptom_flow_still_work(db):
    assert chat(db, "What can you do?")["intent"] == "HELP"
    fever = chat(db, "fever since 3 days")
    assert "General Medicine" in fever["reply"]
    assert chat(db, "chest pain")["reply"].lower().count("108") >= 1


# ==========================================================================
# Full Groq mode: restricted / urgent requests must not reach the model
# ==========================================================================
def test_groq_mode_does_not_call_the_model_for_medical_advice(db, monkeypatch):
    from app.services import groq_engine

    def model_must_not_be_called(*args, **kwargs):
        raise AssertionError("the model was called for a restricted request")

    monkeypatch.setattr(groq_engine, "_groq_request", model_must_not_be_called)

    conv = ai_engine.get_or_create_conversation(db, conversation_id=None, session_id=str(uuid.uuid4()),
                                                channel="WEB", user_id=None)
    for text in ("can you prescribe medicine for fever", "I have severe headache since morning"):
        safety = ai_engine.scan_safety(text)
        if safety["level"] not in {"RESTRICTED", "HIGH"}:
            continue
        result = groq_engine.chat(db, conv, None, text, user_id=None, channel="WEB", safety=safety,
                                  add_message=ai_engine.add_message, escalate=ai_engine.escalate)
        assert result["reply"]


# ==========================================================================
# Reply text: all rows shown, no raw dumps, no extra lookups
# ==========================================================================
def test_outstanding_invoices_does_not_also_look_up_one_invoice():
    assert tools_planned("outstanding invoices") == ["billing.outstanding_invoices"]
    assert tools_planned("invoice 36") == ["billing.get_invoice"]
    plan = chatbot._plan("invoice INV-202610-050400", patient)
    assert [c["tool"] for c in plan] == ["get_invoice"]
    assert plan[0]["args"]["invoice_number"] == "INV-202610-050400"


def test_available_slots_shows_slots_only():
    assert tools_planned("available slots") == ["appointments.get_available_slots"]


def test_render_shows_every_row_and_hides_id_columns():
    rows = [{"id": i, "doctor_id": 1, "doctor": f"Dr {i}", "date": "2026-10-16"} for i in range(8)]
    envelope = {"server": "appointments", "tool": "get_available_slots", "grounded": True, "data": rows}
    text = chatbot._render([envelope], "slots")
    assert text.count("•") == 8 and "more" not in text
    assert "id=" not in text and "Dr 7" in text


def test_render_opens_nested_lists():
    data = {"total": 2, "doctors": [{"id": 1, "full_name": "Dr A"}, {"id": 2, "full_name": "Dr B"}]}
    envelope = {"server": "doctors", "tool": "list_doctors", "grounded": True, "data": data}
    text = chatbot._render([envelope], "doctors")
    assert "full_name=Dr A" in text and "full_name=Dr B" in text and "[{" not in text
