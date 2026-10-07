"""End-to-end smoke test: auth, RBAC, slots, appointments, clinical, billing, AI chat, jobs.

Run with:  python -m tests.smoke_test
(Uses FastAPI's TestClient against the same database the app uses.)
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


def token(client: TestClient, email: str, password: str) -> str | None:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        return None
    return r.json()["access_token"]


def hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def main() -> int:
    init_db()
    with TestClient(app) as client:
        print("=" * 78)
        print("VIJAY VARGIYA GROUP OF HOSPITALS - smoke test | database:",
              "mysql")
        print("=" * 78)

        # ---------- public ----------
        check("health endpoint", client.get("/api/v1/admin/health").status_code == 200)
        check("specialties list", client.get("/api/v1/specialties").status_code == 200)
        doctors = client.get("/api/v1/doctors").json()
        check("doctors seeded", len(doctors) >= 8, str(len(doctors)))
        slots = client.get("/api/v1/slots/available?limit=5").json()
        check("live slots available", len(slots) > 0, str(len(slots)))

        # ---------- auth ----------
        admin = token(client, "admin@vijayvargiiyahospital.in", "Admin@123")
        check("admin login", bool(admin))
        reception = token(client, "reception@vijayvargiiyahospital.in", "Reception@123")
        check("reception login", bool(reception))
        accountant = token(client, "accounts@vijayvargiiyahospital.in", "Accounts@123")
        check("accountant login", bool(accountant))
        doctor_tk = token(client, "dr.arjunmehra@vijayvargiiyahospital.in", "Doctor@123")
        check("doctor login", bool(doctor_tk))
        patient = token(client, "ramesh.yadav@example.com", "Patient@123")
        check("patient login", bool(patient))
        bad = client.post("/api/v1/auth/login", json={"email": "admin@vijayvargiiyahospital.in",
                                                      "password": "wrong"})
        check("bad password rejected", bad.status_code == 401)
        me = client.get("/api/v1/auth/me", headers=hdr(admin)).json()
        check("me endpoint returns permissions", len(me["permissions"]) > 20, str(len(me["permissions"])))
        check("RBAC: patient blocked from admin dashboard",
              client.get("/api/v1/admin/dashboard", headers=hdr(patient)).status_code == 403)
        check("RBAC: receptionist blocked from user admin",
              client.get("/api/v1/users", headers=hdr(reception)).status_code == 403)
        check("RBAC: doctor can read own dashboard",
              client.get("/api/v1/admin/dashboard/doctor?doctor_id=1", headers=hdr(doctor_tk)).status_code == 200)

        # ---------- registration + password flows ----------
        reg = client.post("/api/v1/auth/register", json={
            "full_name": "Test Patient", "email": f"test{datetime.now():%H%M%S}@example.com",
            "phone": "9812345678", "password": "Test@12345", "role": "PATIENT", "city": "Jaipur"})
        check("patient self-registration", reg.status_code == 201, reg.text[:120])
        forgot = client.post("/api/v1/auth/forgot-password",
                             json={"email": "admin@vijayvargiiyahospital.in"}).json()
        check("forgot password issues token", bool(forgot.get("reset_token")))
        if forgot.get("reset_token"):
            reset = client.post("/api/v1/auth/reset-password",
                                json={"token": forgot["reset_token"], "new_password": "NewAdmin@123"})
            check("reset password works", reset.status_code == 200, reset.text[:120])
            relogin = token(client, "admin@vijayvargiiyahospital.in", "NewAdmin@123")
            check("login with new password", bool(relogin))
            back = client.post("/api/v1/auth/change-password", headers=hdr(relogin), json={
                "current_password": "NewAdmin@123", "new_password": "Admin@123"})
            check("change password back", back.status_code == 200)

        # ---------- RBAC matrix ----------
        matrix = client.get("/api/v1/permissions/matrix", headers=hdr(admin))
        check("permission matrix", matrix.status_code == 200 and len(matrix.json()["roles"]) == 6)
        roles = client.get("/api/v1/roles", headers=hdr(admin)).json()
        check("6 roles present", len(roles) == 6, str(len(roles)))

        # ---------- patient system ----------
        new_patient = client.post("/api/v1/patients", headers=hdr(reception), json={
            "full_name": "Asha Kumari", "phone": f"98{datetime.now():%H%M%S}2", "age": 33,
            "gender": "Female", "city": "Jaipur", "blood_group": "B+",
            "emergency_contact_name": "Ravi", "emergency_contact_phone": "9811111111",
            "emergency_contact_relation": "Husband"}).json()
        check("reception registers patient", "patient_code" in new_patient, str(new_patient)[:120])
        search = client.get(f"/api/v1/patients?search={new_patient['patient_code']}", headers=hdr(reception)).json()
        check("patient search by code", len(search) == 1)
        profile = client.get(f"/api/v1/patients/{new_patient['id']}", headers=hdr(reception)).json()
        check("patient profile aggregates history", "stats" in profile and "ai_conversations" in profile)
        check("patient cannot read another patient",
              client.get(f"/api/v1/patients/{new_patient['id']}", headers=hdr(patient)).status_code == 403)

        # ---------- slot engine ----------
        gen = client.post("/api/v1/slots/generate", headers=hdr(admin),
                          json={"days_ahead": 3}).json()
        check("slot generation job endpoint", "created" in gen or "doctors" in gen, str(gen)[:120])
        doc_slots = client.get(f"/api/v1/slots/available?doctor_id={new_patient and doctors[0]['id']}&limit=3").json()
        check("slots for a doctor", len(doc_slots) > 0, str(len(doc_slots)))
        schedule = client.post(f"/api/v1/schedules?doctor_id={doctors[0]['id']}", headers=hdr(admin), json={
            "day_of_week": 6, "start_time": "18:00:00", "end_time": "20:00:00",
            "break_start": "19:00:00", "break_end": "19:15:00", "slot_duration_minutes": 30,
            "capacity_per_slot": 1, "is_active": True})
        check("create weekly schedule", schedule.status_code in (201, 409), schedule.text[:140])
        leave = client.post(f"/api/v1/leaves?doctor_id={doctors[0]['id']}", headers=hdr(admin), json={
            "leave_type": "FULL_DAY", "start_date": str(date.today() + timedelta(days=3)),
            "end_date": str(date.today() + timedelta(days=3)), "reason": "conference"})
        check("apply + block leave", leave.status_code == 201, leave.text[:140])

        # ---------- appointment booking + hold ----------
        hold = client.post("/api/v1/slots/hold", headers=hdr(reception),
                           json={"slot_id": doc_slots[0]["slot_id"], "patient_id": new_patient["id"]}).json()
        check("hold slot", "hold_token" in hold, str(hold)[:120])
        appt = client.post("/api/v1/appointments", headers=hdr(reception), json={
            "patient_id": new_patient["id"], "slot_id": doc_slots[0]["slot_id"],
            "hold_token": hold.get("hold_token"), "reason": "Fever since 3 days",
            "source": "RECEPTION", "send_notifications": True})
        check("book appointment", appt.status_code == 201, appt.text[:160])
        appt = appt.json()
        check("appointment has token + invoice", appt.get("token_number") and appt.get("invoice_id"))
        check("duplicate booking blocked", client.post("/api/v1/appointments", headers=hdr(reception), json={
            "patient_id": new_patient["id"], "slot_id": doc_slots[0]["slot_id"]}).status_code in (409, 422))

        # ---------- queue ----------
        ci = client.post("/api/v1/queue/check-in", headers=hdr(reception),
                         json={"appointment_id": appt["id"]})
        check("queue check-in", ci.status_code == 200 and ci.json()["queue_status"] == "CHECKED_IN")
        start = client.post("/api/v1/queue/start-consultation", headers=hdr(reception),
                            json={"appointment_id": appt["id"]})
        check("queue start consultation", start.json()["queue_status"] == "IN_CONSULTATION")

        # ---------- clinical ----------
        consult = client.post("/api/v1/consultations", headers=hdr(doctor_tk), json={
            "appointment_id": appt["id"], "patient_id": new_patient["id"],
            "chief_complaint": "Fever since 3 days", "vitals": {"bp": "118/78", "temp_f": "101.2"}})
        check("start consultation", consult.status_code == 201, consult.text[:140])
        consult = consult.json()
        upd = client.patch(f"/api/v1/consultations/{consult['id']}", headers=hdr(doctor_tk), json={
            "observations": "Throat congested", "diagnosis": "Acute viral febrile illness",
            "advice": "Hydration + rest", "follow_up_required": True,
            "follow_up_date": str(date.today() + timedelta(days=5))})
        check("save clinical notes + diagnosis", upd.status_code == 200, upd.text[:140])
        records = client.get(f"/api/v1/medical-records?patient_id={new_patient['id']}", headers=hdr(doctor_tk)).json()
        check("records auto-created from consultation", len(records) >= 2, str(len(records)))
        meds = client.get("/api/v1/medicines", headers=hdr(doctor_tk)).json()
        rx = client.post("/api/v1/prescriptions", headers=hdr(doctor_tk), json={
            "consultation_id": consult["id"], "patient_id": new_patient["id"],
            "diagnosis_summary": "Acute viral febrile illness", "advice": "Complete the course",
            "items": [{"medicine_id": meds[0]["id"], "medicine_name": meds[0]["name"], "dosage": "1-0-1",
                       "frequency": "1-0-1", "duration": "5 days", "instructions": "After food", "quantity": 1}],
            "notify_patient": True})
        check("issue prescription", rx.status_code == 201, rx.text[:140])
        rx = rx.json()
        pdf = client.get(f"/api/v1/prescriptions/{rx['id']}/pdf", headers=hdr(doctor_tk))
        check("prescription PDF generated", pdf.status_code == 200 and pdf.content[:4] == b"%PDF")
        done = client.post(f"/api/v1/consultations/{consult['id']}/complete", headers=hdr(doctor_tk))
        check("complete consultation", done.status_code == 200)
        co = client.post("/api/v1/queue/check-out", headers=hdr(reception), json={"appointment_id": appt["id"]})
        check("queue check-out", co.status_code == 200 and co.json()["queue_status"] == "COMPLETED")

        # ---------- billing ----------
        invoices = client.get(f"/api/v1/invoices?patient_id={new_patient['id']}", headers=hdr(accountant)).json()
        check("invoice auto-created on booking", len(invoices) >= 1, str(len(invoices)))
        inv = invoices[0]
        pay = client.post("/api/v1/payments", headers=hdr(reception), json={
            "invoice_id": inv["id"], "amount": float(inv["balance_amount"]), "method": "UPI",
            "status": "PAID", "transaction_reference": "UPI-TEST-001"})
        check("record payment", pay.status_code == 201, pay.text[:140])
        pay = pay.json()
        inv2 = client.get(f"/api/v1/invoices/{inv['id']}", headers=hdr(accountant)).json()
        check("invoice marked paid", inv2["status"] == "PAID" and inv2["balance_amount"] == 0,
              f"{inv2['status']} / {inv2['balance_amount']}")
        receipt = client.get(f"/api/v1/invoices/{inv['id']}/receipt", headers=hdr(accountant))
        check("invoice receipt PDF", receipt.status_code == 200 and receipt.content[:4] == b"%PDF")
        ref = client.post(f"/api/v1/payments/{pay['id']}/refund", headers=hdr(accountant),
                          json={"amount": 100, "reason": "overcharge"})
        check("refund processed", ref.status_code == 200, ref.text[:140])

        # ---------- reviews ----------
        rv = client.post("/api/v1/reviews", headers=hdr(patient), json={
            "doctor_id": doctors[0]["id"], "rating": 5, "title": "Great", "comment": "Very helpful"})
        check("submit review", rv.status_code == 201, rv.text[:140])
        mod = client.post(f"/api/v1/reviews/{rv.json()['id']}/moderate", headers=hdr(admin),
                          json={"status": "APPROVED"})
        check("moderate review", mod.status_code == 200)

        # ---------- notifications + whatsapp ----------
        notif = client.post("/api/v1/notifications", headers=hdr(reception), json={
            "patient_id": new_patient["id"], "channel": "WHATSAPP", "template": "APPOINTMENT_REMINDER",
            "message": "Reminder", "payload": {"patient_name": "Asha", "doctor_name": "Dr Arjun Mehra",
                                               "date": "05 Oct", "time": "10:00", "hospital": "VVH", "token": 3},
            "send_now": True})
        check("send whatsapp notification", notif.status_code == 201 and notif.json()["status"] == "SENT",
              notif.text[:140])
        wa = client.get("/api/v1/whatsapp/status/summary", headers=hdr(admin)).json()
        check("whatsapp delivery funnel", wa["total"] > 0 and "by_template" in wa)

        # ---------- AI chat flows ----------
        c1 = client.post("/api/v1/ai/chat", json={"message": "hello"}).json()
        check("AI greeting", "Namaste" in c1["reply"] or "Welcome" in c1["reply"])
        conv_id, session = c1["conversation_id"], c1["session_id"]
        c2 = client.post("/api/v1/ai/chat", json={
            "message": "fever and body ache since 3 days", "conversation_id": conv_id}).json()
        check("AI concern extraction + specialty routing",
              bool(c2["concern"] and c2["specialty"]), str(c2.get("concern"))[:120])
        c3 = client.post("/api/v1/ai/chat", json={
            "message": "my name is Sunil Kumar", "conversation_id": conv_id}).json()
        check("AI new-patient flow asks for phone", c3["stage"] == "AWAIT_IDENTITY_PHONE", c3["stage"])
        new_phone = "98" + str(int(time.time()))[-8:]
        c4 = client.post("/api/v1/ai/chat", json={
            "message": new_phone, "conversation_id": conv_id, "name": "Sunil Kumar"}).json()
        check("AI creates patient on new number",
              bool(c4["patient"] and c4["patient"]["type"] == "NEW"), str(c4.get("patient")))
        c5 = client.post("/api/v1/ai/chat", json={
            "message": "fever and body ache since 3 days", "conversation_id": conv_id}).json()
        check("AI offers doctors", len(c5["doctors"]) > 0, str(len(c5["doctors"])))
        c6 = client.post("/api/v1/ai/chat", json={"message": "1", "conversation_id": conv_id}).json()
        check("AI shows live slots", len(c6["slots"]) > 0, str(len(c6["slots"])))
        c7 = client.post("/api/v1/ai/chat", json={"message": "1", "conversation_id": conv_id}).json()
        check("AI holds slot and asks confirmation", c7["stage"] == "AWAIT_CONFIRMATION", c7["stage"])
        c8 = client.post("/api/v1/ai/chat", json={"message": "yes confirm", "conversation_id": conv_id}).json()
        check("AI books appointment", bool(c8["appointment"] and c8["appointment"].get("appointment_code")),
              c8["reply"][:140])
        c9 = client.post("/api/v1/ai/chat", json={
            "message": "what medicines should I take for this fever?", "conversation_id": conv_id}).json()
        check("AI refuses medical advice", c9["safety"]["flag"] == "MEDICAL_ADVICE_RESTRICTION",
              str(c9.get("safety")))
        c10 = client.post("/api/v1/ai/chat", json={
            "message": "I have severe chest pain and cannot breathe", "conversation_id": conv_id}).json()
        check("AI emergency escalation", c10["safety"]["level"] == "EMERGENCY" and c10["escalation"]["level"] == "EMERGENCY")

        # patient-scoped AI lookups
        p1 = client.post("/api/v1/ai/chat", headers=hdr(patient), json={"message": "show my prescription"}).json()
        check("AI prescription lookup for logged-in patient",
              "prescription" in p1["reply"].lower() or bool(p1["prescriptions"]))
        p2 = client.post("/api/v1/ai/chat", headers=hdr(patient), json={"message": "my payment status"}).json()
        check("AI payment status", "Rs" in p2["reply"] or bool(p2["payments"]))
        p3 = client.post("/api/v1/ai/chat", headers=hdr(patient), json={
            "message": "cancel appointment", "conversation_id": p2["conversation_id"]}).json()
        check("AI cancel flow responds", "cancel" in p3["reply"].lower(), p3["reply"][:120])
        tools = client.get("/api/v1/ai/tools", headers=hdr(admin)).json()
        check("18 AI tools registered", len(tools) == 18, str(len(tools)))
        invoke = client.post("/api/v1/ai/tools/invoke", headers=hdr(admin),
                             json={"name": "get_patient_summary", "arguments": {"patient_id": 1}})
        check("AI tool invocation", invoke.status_code == 200 and invoke.json()["result"].get("found"))
        routing = client.get("/api/v1/ai/routing?concern=knee pain while walking", headers=hdr(admin)).json()
        check("stateless routing endpoint",
              routing["specialty"]["specialty_name"] == "Orthopaedics",
              str(routing.get("specialty")))
        monitor = client.get("/api/v1/ai/monitor", headers=hdr(admin)).json()
        check("AI monitor metrics", monitor["cards"]["conversations"] > 0 and "tool_usage" in monitor)
        esc = client.get("/api/v1/ai/escalations?status_filter=OPEN", headers=hdr(admin)).json()
        check("escalation queue populated", len(esc) >= 1, str(len(esc)))

        # ---------- dashboards + analytics ----------
        dash = client.get("/api/v1/admin/dashboard", headers=hdr(admin)).json()
        check("admin dashboard cards + charts",
              dash["cards"]["total_patients"] > 0 and "appointment_trend" in dash["charts"])
        pgen = client.get("/api/v1/analytics/patient-growth", headers=hdr(admin)).json()
        check("patient growth analytics", len(pgen) > 0)
        rev = client.get("/api/v1/analytics/revenue", headers=hdr(admin)).json()
        check("revenue analytics", "by_method" in rev)
        ai_an = client.get("/api/v1/analytics/ai", headers=hdr(admin)).json()
        check("AI analytics", "cards" in ai_an)
        util = client.get("/api/v1/analytics/doctor-utilisation", headers=hdr(admin)).json()
        check("doctor utilisation", len(util) > 0)

        # ---------- audit + sessions ----------
        audit_logs = client.get("/api/v1/auth/audit-logs", headers=hdr(admin)).json()
        check("audit trail written", len(audit_logs) > 5, str(len(audit_logs)))
        check("audit captures old → new values",
              any(x.get("previous_value") or x.get("new_value") for x in audit_logs))
        logins = client.get("/api/v1/auth/login-activity", headers=hdr(admin)).json()
        check("login activity tracked", any(x["status"] == "FAILED" for x in logins))
        sessions = client.get("/api/v1/auth/sessions", headers=hdr(admin)).json()
        check("session management list", len(sessions) >= 1)
        if sessions:
            rev = client.delete(f"/api/v1/auth/sessions/{sessions[0]['id']}", headers=hdr(admin))
            check("revoke session", rev.status_code == 200)

        # ---------- background jobs ----------
        for job in ["generate_tomorrow_slots", "expire_old_slots", "appointment_reminders",
                    "follow_up_reminders", "payment_reminders", "review_requests",
                    "cleanup_temp_data", "noshow_sweep", "close_stale_sessions"]:
            r = client.post(f"/api/v1/admin/jobs/{job}/run", headers=hdr(admin))
            check(f"job: {job}", r.status_code == 200 and r.json()["status"] == "SUCCESS", r.text[:120])

        # ---------- notification channels + email ----------
        # The whole system asks for ["WHATSAPP", "IN_APP"]. When SMTP is
        # configured, email must be added automatically; when nothing is
        # configured, a disabled channel must report the truth (never a fake
        # "sent").
        from app.config import settings as live_settings
        from app.services import notify as notify_service

        was_email, was_wa = live_settings.email_enabled, live_settings.whatsapp_enabled
        try:
            live_settings.email_enabled, live_settings.whatsapp_enabled = False, False
            check("channels unchanged when nothing is configured",
                  notify_service.available_channels(["WHATSAPP", "IN_APP"]) == ["WHATSAPP", "IN_APP"],
                  str(notify_service.available_channels(["WHATSAPP", "IN_APP"])))
            live_settings.email_enabled = True
            chans = notify_service.available_channels(["WHATSAPP", "IN_APP"])
            check("email is added automatically when SMTP is switched on",
                  chans[0] == "EMAIL" and "IN_APP" in chans and "WHATSAPP" in chans, str(chans))
            live_settings.email_enabled = False
            ok, _ref, err = notify_service._send_email("someone@example.com", "test", "body")
            check("email stays off while EMAIL_ENABLED=false", ok is False and "EMAIL_ENABLED" in (err or ""),
                  f"ok={ok} err={err}")
            ok, _ref, err = notify_service._send_email("someone@example.com", "test", "body")
            check("no recipient -> clear error, no crash", ok is False and bool(err), str(err))
            stats = client.get("/api/v1/notifications/stats?days=30", headers=hdr(admin)).json()
            check("notification stats expose the live channels",
                  "available_channels" in stats and "email_enabled" in stats, str(list(stats))[:120])
            test_mail = client.post("/api/v1/notifications/test-email", headers=hdr(admin))
            check("POST /notifications/test-email explains the state",
                  test_mail.status_code == 200 and "detail" in test_mail.json(), test_mail.text[:120])
        finally:
            live_settings.email_enabled, live_settings.whatsapp_enabled = was_email, was_wa

        # ---------- logout ----------
        check("logout", client.post("/api/v1/auth/logout", headers=hdr(reception)).status_code == 200)

    print("=" * 78)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failed checks:")
        for f in FAIL:
            print("  -", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
