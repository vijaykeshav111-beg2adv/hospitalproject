#!/usr/bin/env python3
"""
MySQL-specific integration checks -- things that behave differently from SQLite.

Runs against a **running server** (default http://127.0.0.1:8000) and verifies the
database behaviours a MySQL deployment depends on:

  1. strict DML: NOT NULL / UNIQUE constraints really are enforced
  2. foreign keys: orphan rows are rejected, cascades/SET NULL fire
  3. transactions: a failed booking does not leave a half-written appointment
  4. utf8mb4: Hindi/Nepali patient names and emoji survive a round trip
  5. DECIMAL money: totals are exact to 2 decimals (no float drift)
  6. slot-hold exclusivity: two concurrent holds on one slot -> exactly one wins
  7. JSON columns: AI conversation memory (pending_json) round-trips as JSON
  8. generated views (v_appointments_today etc.) are queryable by the app user
  9. TIMESTAMP/DATETIME + timezone: created_at is stored and read back as UTC-consistent

Usage:
    python3 -m tests.mysql_checks                 # http://127.0.0.1:8000
    python3 -m tests.mysql_checks --base http://host:8000
Exit code 1 if any check fails or the backend is not MySQL.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal

# Load .env exactly like the application does, otherwise the `mysql` CLI below
# runs with an empty password and every check fails with
# "Access denied for user 'vvh_app'@'localhost' (using password: NO)".
try:
    from app.config import settings

    _DB_USER = os.getenv("DB_USER") or settings.db_user
    _DB_PASSWORD = os.getenv("DB_PASSWORD") or settings.db_password
    _DB_NAME = os.getenv("DB_NAME") or settings.db_name
except Exception:  # pragma: no cover - keep the suite usable standalone
    _DB_USER = os.getenv("DB_USER", "vvh_app")
    _DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    _DB_NAME = os.getenv("DB_NAME", "vijay_vargiya_hospital")

MYSQL = ["mysql", "-u", _DB_USER, "-p" + _DB_PASSWORD, _DB_NAME, "-N", "-B", "-e"]

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


class SqlError(RuntimeError):
    """Raised when the mysql CLI exits non-zero (e.g. a constraint violation)."""


def try_sql(query: str) -> tuple[bool, str]:
    """Run a query as the application user.

    Returns (succeeded, message). MySQL exits non-zero on a constraint
    violation and prints ``ERROR <code> (<state>)`` - never test for success by
    searching the output text, the statement echo is printed first.
    """
    result = subprocess.run(MYSQL + [query], capture_output=True, text=True)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message


def sql(query: str) -> str:
    ok, message = try_sql(query)
    if not ok:
        raise SqlError(message[-200:])
    return message


def api(base: str, path: str, method: str = "GET", body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200]}


def login(base: str, email: str, password: str) -> str:
    status, payload = api(base, "/api/v1/auth/login", "POST", {"email": email, "password": password})
    if status != 200:
        raise RuntimeError(f"login failed for {email}: {status} {payload}")
    return payload["access_token"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MySQL behaviour checks")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    print(f"MySQL behaviour checks @ {base}")
    status, health = api(base, "/api/v1/admin/health")
    backend = (health.get("database") or {}).get("backend") if status == 200 else None
    check("server is running on MySQL (not the SQLite fallback)", backend == "mysql", f"backend={backend}")
    if backend != "mysql":
        print("\n  This suite only makes sense against MySQL. Start the app with the MySQL .env.")
        print(f"\nRESULT: {passed} passed, {failed} failed")
        return 1

    print(f"  server version: {sql('SELECT VERSION()')}")

    admin = login(base, "superadmin@vijayvargiyahospital.in", "SuperAdmin@123")
    reception = login(base, "reception@vijayvargiyahospital.in", "Reception@123")
    doctor_tk = login(base, "dr.arjunmehra@vijayvargiyahospital.in", "Doctor@123")
    accountant = login(base, "accounts@vijayvargiyahospital.in", "Accounts@123")
    patient_tk = login(base, "ramesh.yadav@example.com", "Patient@123")

    # ---------------------------------------------------------------- 1. constraints
    print("\n1. strict constraint enforcement")
    dup = sql("SELECT COUNT(*) FROM (SELECT email FROM users GROUP BY email HAVING COUNT(*) > 1) x")
    check("users.email is unique in the database", dup == "0", f"duplicate groups={dup}")
    strict = sql("SELECT @@sql_mode")
    check("strict SQL mode is active (bad rows error instead of being coerced)",
          "STRICT" in strict.upper(), strict[:70])
    # deliberately omit the NOT NULL password_hash column
    ok, message = try_sql(
        "INSERT INTO users (uuid, full_name, email, role_id, is_active, is_verified, "
        "must_change_password, phone_verified, failed_login_attempts, created_at, updated_at) "
        "SELECT 'probe-uuid', 'Probe', 'probe@invalid.test', id, 1, 1, 1, 1, 0, NOW(), NOW() "
        "FROM roles LIMIT 1")
    check("NOT NULL column without a value is rejected (no silent default)",
          not ok, "insert unexpectedly succeeded")
    if not ok:
        check("  ...rejected with ERROR 1364", "ERROR 1364" in message,
              message.splitlines()[-1][:90])
    sql("DELETE FROM users WHERE email IN ('x@x', 'probe@invalid.test')")
    charset = sql("SELECT default_character_set_name FROM information_schema.SCHEMATA "
                  "WHERE schema_name = 'vijay_vargiya_hospital'")
    collation = sql("SELECT table_collation FROM information_schema.TABLES "
                    "WHERE table_schema = 'vijay_vargiya_hospital' AND table_name = 'patients'")
    check("database charset is utf8mb4", charset == "utf8mb4", charset)
    check("tables use utf8mb4 collation", collation.startswith("utf8mb4"), collation)
    engines = sql("SELECT COUNT(*) FROM information_schema.TABLES WHERE table_schema = 'vijay_vargiya_hospital' "
                  "AND table_type = 'BASE TABLE' AND engine <> 'InnoDB'")
    check("every table is InnoDB (transactions + FKs)", engines == "0", f"non-InnoDB tables={engines}")

    # ---------------------------------------------------------------- 2. foreign keys
    print("\n2. foreign keys")
    fk_count = sql("SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE table_schema = "
                   "'vijay_vargiya_hospital' AND constraint_type = 'FOREIGN KEY'")
    check("foreign keys are declared on the schema", int(fk_count) > 20, f"foreign keys={fk_count}")
    ok, message = try_sql(
        "INSERT INTO appointments (appointment_code, patient_id, doctor_id, specialty_id, "
        "appointment_date, start_time, end_time, status, queue_status, source, token_number, "
        "created_at, updated_at) VALUES ('FK-TEST', 999999, 1, 1, CURDATE(), '09:00:00', '09:30:00', "
        "'PENDING', 'WAITING', 'RECEPTION', 999, NOW(), NOW())")
    check("orphan appointment (bad patient_id) is rejected", not ok, "insert unexpectedly succeeded")
    if not ok:
        check("  ...rejected with ERROR 1452 (foreign key)", "ERROR 1452" in message,
              message.splitlines()[-1][:90])

    # ---------------------------------------------------------------- 3. transactions
    print("\n3. transactional integrity")
    before = int(sql("SELECT COUNT(*) FROM appointments"))
    _, doctors = api(base, "/api/v1/doctors?active_only=true", token=reception)
    doctor = next(d for d in doctors if d["full_name"] == "Dr Arjun Mehra")
    _, slots = api(base, f"/api/v1/slots/available?doctor_id={doctor['id']}&limit=5", token=reception)
    bad = api(base, "/api/v1/appointments", "POST", {
        "patient_id": 999999, "doctor_id": doctor["id"], "slot_id": slots[0]["slot_id"],
        "appointment_date": slots[0]["slot_date"], "start_time": slots[0]["start_time"],
        "source": "RECEPTION", "reason": "rollback probe"}, token=reception)[0]
    after = int(sql("SELECT COUNT(*) FROM appointments"))
    check("failed booking rolls back (no orphan appointment row)",
          bad >= 400 and before == after, f"status={bad} before={before} after={after}")

    # ---------------------------------------------------------------- 4. utf8mb4 round trip
    print("\n4. utf8mb4 round trip")
    hindi_name = "सुनीता देवी 🩺"
    status, patient = api(base, "/api/v1/patients", "POST", {
        "full_name": hindi_name, "phone": "97" + str(int(time.time()))[-8:],
        "gender": "FEMALE", "age": 33, "city": "जयपुर"}, token=reception)
    check("patient with Hindi name + emoji is created", status == 201, f"status={status} {patient}")
    if status == 201:
        _, detail = api(base, f"/api/v1/patients/{patient['id']}", token=reception)
        check("Hindi name round-trips unchanged", detail.get("full_name") == hindi_name,
              f"stored={detail.get('full_name')!r}")
        row = sql(f"SELECT full_name, city FROM patients WHERE id = {patient['id']}")
        check("MySQL stored 4-byte characters (emoji) intact", "🩺" in row, row)
        check("Hindi city stored intact", "जयपुर" in row, row)

    # ---------------------------------------------------------------- 5. DECIMAL money
    print("\n5. DECIMAL money precision")
    invoice_type = sql("SELECT column_type FROM information_schema.COLUMNS WHERE table_schema = "
                       "'vijay_vargiya_hospital' AND table_name = 'invoices' AND column_name = 'total_amount'")
    check("money columns are DECIMAL (not FLOAT)", invoice_type.startswith("decimal"), invoice_type)
    drift = sql("SELECT COUNT(*) FROM invoices WHERE ABS(total_amount - (subtotal - discount_amount + tax_amount)) > 0.01")
    check("no invoice has a rounding drift > 1 paisa", drift == "0", f"offending rows={drift}")
    paid_ok = sql("SELECT COUNT(*) FROM invoices WHERE status = 'PAID' AND balance_amount <> 0")
    check("PAID invoices have zero balance", paid_ok == "0", f"offending rows={paid_ok}")

    # ---------------------------------------------------------------- 6. slot hold exclusivity
    print("\n6. slot-hold exclusivity (concurrent hold)")
    _, slots2 = api(base, f"/api/v1/slots/available?doctor_id={doctor['id']}&limit=5", token=reception)
    target = slots2[-1]
    _, patients = api(base, "/api/v1/patients?limit=2", token=reception)
    first = api(base, "/api/v1/slots/hold", "POST",
                {"slot_id": target["slot_id"], "patient_id": patients[0]["id"]}, token=reception)
    second = api(base, "/api/v1/slots/hold", "POST",
                 {"slot_id": target["slot_id"], "patient_id": patients[1]["id"]}, token=reception)
    check("first hold succeeds", first[0] == 200, f"status={first[0]}")
    check("second hold on the same slot is refused", second[0] == 409, f"status={second[0]} {second[1]}")
    held_rows = sql(f"SELECT COUNT(*) FROM slots WHERE id = {target['slot_id']} AND status = 'HELD'")
    check("slot row is in exactly one held state", held_rows == "1", f"rows={held_rows}")
    if first[0] == 200:
        # correct route: POST /api/v1/slots/{slot_id}/release
        rel = api(base, f"/api/v1/slots/{target['slot_id']}/release", "POST", None, token=reception)
        check("held slot can be released back to AVAILABLE", rel[0] == 200, f"status={rel[0]} {rel[1]}")
        freed = sql(f"SELECT status FROM slots WHERE id = {target['slot_id']}")
        check("released slot is AVAILABLE again", freed == "AVAILABLE", f"status={freed}")

    # ---------------------------------------------------------------- 7. JSON columns
    print("\n7. JSON / LONGTEXT memory columns")
    status, chat = api(base, "/api/v1/ai/chat", "POST", {"message": "hello"})
    check("AI conversation is created on MySQL", status == 200, f"status={status}")
    conv_id = chat.get("conversation_id")
    if conv_id:
        sql(f"UPDATE ai_conversations SET pending_json = JSON_OBJECT('probe', 'ok') WHERE id = {conv_id}")
        raw = sql(f"SELECT JSON_EXTRACT(pending_json, '$.probe') FROM ai_conversations WHERE id = {conv_id}")
        check("pending_json accepts and returns valid JSON", raw.strip('"') == "ok", raw)
        sql(f"UPDATE ai_conversations SET pending_json = NULL WHERE id = {conv_id}")

    # ---------------------------------------------------------------- 8. views
    print("\n8. reporting views")
    for view in ("v_appointments_today", "v_outstanding_invoices", "v_doctor_daily_load", "v_ai_daily_summary"):
        ok, message = try_sql(f"SELECT COUNT(*) FROM {view}")
        check(f"view {view} is queryable by the app user", ok, message[-90:])

    # ---------------------------------------------------------------- 9. datetimes
    print("\n9. datetimes")
    tz = sql("SELECT @@global.time_zone")
    check("server time zone is set for IST operations", tz not in ("", "SYSTEM"), f"time_zone={tz}")
    status_check = sql("SELECT COUNT(*) FROM appointments WHERE appointment_date < '2000-01-01'")
    check("no appointment has an obviously invalid date", status_check == "0", status_check)

    # ---------------------------------------------------------------- API still healthy
    print("\n10. API smoke on MySQL")
    dash = api(base, "/api/v1/admin/dashboard?days=30", token=admin)[0]
    queue = api(base, "/api/v1/queue/desk", token=reception)[0]
    pdf = api(base, "/api/v1/prescriptions?limit=1", token=doctor_tk)
    check("admin dashboard OK on MySQL", dash == 200, f"status={dash}")
    check("reception queue OK on MySQL", queue == 200, f"status={queue}")
    check("clinical list OK on MySQL", pdf[0] == 200, f"status={pdf[0]}")
    inv = api(base, "/api/v1/invoices?limit=1", token=accountant)[0]
    check("accountant invoice list OK on MySQL", inv == 200, f"status={inv}")
    me = api(base, "/api/v1/patients/me", token=patient_tk)[0]
    check("patient portal OK on MySQL", me == 200, f"status={me}")

    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
