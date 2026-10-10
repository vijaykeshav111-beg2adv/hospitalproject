# Vijay Vargiya Group of Hospitals — Hospital Management Platform

A complete, production-shaped hospital/clinic management system in **Python (FastAPI + SQLAlchemy + MySQL)**
with an original three-portal web UI, an AI front-desk assistant, a slot engine, billing, WhatsApp
notifications and a full audit trail.

```
Web portals  /ui        API  /api/v1        Docs  /docs        DB  MySQL only
```

---

## 1. What is inside

| Area | Delivered |
|---|---|
| **Authentication** | Login, logout, patient self-registration, forgot/reset password, JWT access + refresh tokens (`jti`, rotation, revocation), RBAC with 6 roles and a permission matrix, password hashing (PBKDF2-HMAC-SHA256, 260k iterations), account lockout, session management (list/revoke devices), login activity, audit logs, API security headers/CORS, token in query-string for file downloads |
| **Roles** | `SUPER_ADMIN`, `ADMIN`, `DOCTOR`, `RECEPTIONIST`, `ACCOUNTANT`, `PATIENT` — permission-based guards on every mutating endpoint |
| **Patients** | Registration, demographics, medical history, allergies, chronic conditions, emergency contact, search/suggest, per-patient timeline (visits, prescriptions, files, invoices), patient portal (own data only) |
| **Doctors** | Profiles, qualifications, registration number, languages, consultation fee, weekly schedules with breaks, availability API, leave requests and approval |
| **Specialties** | 12-specialty master with a symptom→specialty keyword map used by the AI router |
| **AI Chat** | Natural-language front desk: greeting → concern extraction → specialty routing → doctor match → live slot search → hold → booking, plus cancel, reschedule, prescription/report/bill lookup, human escalation. Works anonymously, via WhatsApp channel or inside the logged-in portal |
| **AI Memory** | Conversation, session, per-message intent/confidence/safety flags, structured concern memory, patient context summary used for personalised replies |
| **AI Tools (18)** | `get_patient_profile`, `get_patient_summary`, `get_patient_concerns`, `get_patient_appointments`, `get_patient_prescriptions`, `get_patient_files`, `search_specialties`, `search_doctors`, `get_doctor_availability`, `get_available_slots`, `hold_slot`, `release_slot`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `get_invoice`, `get_payment_status`, `send_notification` — every call is logged in `ai_tool_calls` with arguments, status and duration |
| **AI Safety** | No diagnosis, no medical advice, no prescriptions (refused with a friendly redirect), emergency phrase detection → 108 guidance + doctor/reception escalation + WhatsApp alert, urgent-symptom flagging, full safety audit |
| **Appointments** | Booking with token numbers, confirm/cancel/reschedule/complete/no-show, status history, invoice link, consultation link, reminders |
| **Slot engine** | Slot generation from doctor schedules, break-aware, leave/holiday aware, hold with expiry (`SLOT_HOLD_MINUTES`), release, booking, next-day generation job |
| **Reception** | Live queue board with `WAITING / CHECKED_IN / IN_CONSULTATION / COMPLETED / NO_SHOW`, check-in, start consultation, check-out, no-show, payment collection at the desk |
| **Clinical** | Consultations (vitals, examination, diagnosis summary, advice, follow-up date), medical records, medical file uploads (MIME-whitelisted, SHA-256 named), prescriptions with medicine lines + **PDF** |
| **Billing** | Invoices (auto-created on booking), line items, discounts, taxes, **PDF invoices**, payment reminders |
| **Payments** | Cash / UPI / Card / Online, statuses `PENDING / PAID / FAILED / REFUNDED`, online payment initiation + gateway callback, refunds, daily collection report |
| **Notifications** | Notification service (WhatsApp / Email / In-App / SMS-ready) with templates, delivery status, retry jobs; Twilio WhatsApp provider with a dry-run console fallback |
| **Reviews** | Post-visit review capture, ratings 1–5, moderation (approve/reject/flag), aggregates on doctor profiles |
| **Dashboards** | Admin (KPIs + charts), Doctor console (today's list, follow-ups, leaves), Patient portal, Reception desk — plus analytics endpoints (revenue, appointments, doctors, patients, AI) |
| **Background jobs** | 12 APScheduler jobs (see §8) with run history in `job_runs` and manual trigger from the System page |
| **Ops** | Health/system endpoints, DB health, settings store, audit log, AI monitor (routing accuracy, failures, escalations), SQL setup/verify scripts |

---

## 2. Quick start (demo, zero setup)

```bash
cd vijay_vargiya_hospital
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                 # optional: tweak port / secrets
python3 scripts/seed.py --force      # create schema + demo data (12 specialties, 8 doctors, 12 patients, 14 days of slots)

python3 run.py                       # http://127.0.0.1:8000/ui
```

MySQL is mandatory. If the MySQL server is unreachable the app fails fast with a connection error.
(`vvh_demo.db`) so the whole product stays explorable. Nothing else changes — the same code path,
models and queries run against MySQL.

### Demo logins

| Role | Email | Password |
|---|---|---|
| SUPER_ADMIN | `superadmin@vijayvargiyahospital.in` | `SuperAdmin@123` |
| ADMIN | `admin@vijayvargiyahospital.in` | `Admin@123` |
| DOCTOR | `dr.arjunmehra@vijayvargiyahospital.in` | `Doctor@123` |
| RECEPTIONIST | `reception@vijayvargiyahospital.in` | `Reception@123` |
| ACCOUNTANT | `accounts@vijayvargiyahospital.in` | `Accounts@123` |
| PATIENT | `ramesh.yadav@example.com` | `Patient@123` |

Every seeded doctor uses `Doctor@123`; every seeded patient uses `Patient@123`.

### Portals

| Page | URL | Who |
|---|---|---|
| Public site + AI chat widget | `/ui` | everyone |
| Login / register / forgot / reset | `/ui/login`, `/ui/register`, `/ui/forgot-password`, `/ui/reset-password` | everyone |
| Admin dashboard | `/ui/admin` | SUPER_ADMIN, ADMIN |
| Doctor console | `/ui/doctor` | DOCTOR |
| Patient portal | `/ui/patient` | PATIENT |
| Reception desk | `/ui/reception` | RECEPTIONIST (+ ADMIN) |
| Booking, patients, appointments | `/ui/book`, `/ui/patients`, `/ui/appointments` | staff |
| Clinical workspace | `/ui/clinical` | DOCTOR, ADMIN |
| Billing & payments | `/ui/billing` | ACCOUNTANT, RECEPTIONIST, ADMIN |
| AI assistant / AI monitor | `/ui/ai-chat`, `/ui/ai-monitor` | everyone / ADMIN |
| Security & audit, System & jobs | `/ui/security`, `/ui/system` | ADMIN, SUPER_ADMIN |

---

## 3. MySQL setup (the real deployment)

> Full step-by-step guide, verification results, backup/restore and troubleshooting:
> **[MYSQL_SETUP.md](MYSQL_SETUP.md)**. Short version below.

Everything the brief asked for — database creation, a dedicated application user, grants and
`FLUSH PRIVILEGES` — lives in `sql/01_setup_database.sql`. Run the scripts in order:

```bash
# 1. database, users, grants, views  (creates the schema + vvh_app / vvh_readonly / vvh_backup / vvh_migrator)
mysql -u root -p < sql/01_setup_database.sql

# 2. tables (40 tables, InnoDB, utf8mb4, indexes + foreign keys)
mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql

# 3. master + demo data (roles, permissions, specialties, medicines, doctors, patients)
mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql

# 4. verification: sanity checks, row counts, grants, reporting views, idempotency probe
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql

# 5. finish the demo data (stage aware - adds only what is missing) and start
python3 -m app.seed
python3 run.py
```

All four scripts were executed against a real MySQL-protocol server (MariaDB 11.8) with the
application then running on it: **smoke test 89/89, page check 69/69, route sweep 450 combinations
with no 5xx/422, and 35/35 `tests/mysql_checks.py`** (strict mode, foreign keys, transaction
rollback, utf8mb4 round trip, DECIMAL money, slot-hold exclusivity, JSON memory, reporting views).
See the verification log in [MYSQL_SETUP.md](MYSQL_SETUP.md#8-verification-log-what-actually-ran).

Then point the app at it (`.env`):

```ini
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=vijay_vargiya_hospital
DB_USER=vvh_app
DB_PASSWORD=change_me
```

and start normally: `python3 run.py`.

**Users created by script 01**

| User | Password | Grants |
|---|---|---|
| `vvh_app@localhost`, `vvh_app@%` | `change_me` | SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW, CREATE TEMPORARY TABLES on `vijay_vargiya_hospital.*` |
| `vvh_readonly@%` | `change_me_readonly` | SELECT, SHOW VIEW (reporting/BI) |
| `vvh_backup@localhost` | `change_me_backup` | SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER, RELOAD (backups) |
| `vvh_migrator@localhost` | `change_me_migrator` | ALL PRIVILEGES (schema changes only) |

Verified privilege behaviour: `vvh_readonly` is refused `UPDATE` (`ERROR 1142`), `vvh_app` is refused
`CREATE TABLE` (least privilege — DDL belongs to `vvh_migrator`), and `vvh_backup` can produce a
457 KB dump that restores into a scratch database with matching row counts.

Server globals applied: `time_zone = '+05:30'`, `max_connections = 200`,
`character_set_server = utf8mb4`. All passwords are change-first deployment secrets.

`sql/02_schema.sql` and `sql/03_seed.sql` are **generated from the SQLAlchemy models** so the DDL can
never drift from the ORM:

```bash
python3 -m app.tools.export_schema sql/02_schema.sql
python3 -m app.tools.export_seed   sql/03_seed.sql
```

---

## 4. Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `APP_NAME` | Vijay Vargiya Group of Hospitals | Displayed across portals, PDFs, WhatsApp |
| `APP_ENV` | development | `development` / `production` |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Bind address |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | localhost/3306/vijay_vargiya_hospital/vvh_app | MySQL connection |
| `DATABASE_URL` | *(blank)* | Full SQLAlchemy URL; overrides the DB_* pair |
| `JWT_SECRET`, `JWT_ALGORITHM` | dev secret, HS256 | **Set a long random secret in production** |
| `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS` | 60, 14 | Token lifetimes |
| `PASSWORD_HASH_ITERATIONS` | 260000 | PBKDF2 rounds |
| `MAX_LOGIN_ATTEMPTS`, `LOCKOUT_MINUTES` | 5, 15 | Brute-force protection |
| `STORAGE_DIR`, `MAX_UPLOAD_MB` | `./storage`, 25 | Medical file storage |
| `WHATSAPP_ENABLED`, `TWILIO_*` | false | Twilio WhatsApp; when false messages are queued and logged |
| `EMAIL_ENABLED`, `SMTP_*` | false | SMTP notifications |
| | `GROQ_API_KEY`, `GROQ_MODEL` | blank / `llama-3.3-70b-versatile` | Groq API credentials/model |
| `GROQ_BASE_URL`, `GROQ_TIMEOUT_SECONDS`, `GROQ_MAX_TOKENS` | Groq OpenAI-compatible endpoint / 45 / 700 | Groq connection/runtime settings |
| `SLOT_HOLD_MINUTES` | 10 | Slot hold expiry |
| `ENABLE_SCHEDULER` | true | Background jobs |

---

## 5. Architecture

```
app/
├── main.py             FastAPI factory: lifespan (init_db → seed → scheduler), CORS, /api/v1 mount, /ui, /static
├── config.py           Settings (env driven, .env loader)
├── database.py         MySQL engine + session, init_db(), db_health()
├── models.py           40 SQLAlchemy tables (RBAC, patients, clinical, billing, AI, ops)
├── schemas.py          Pydantic v2 request/response models for every module
├── security.py         PBKDF2 hashing, JWT issue/verify, opaque tokens, utcnow()
├── rbac.py             Roles, permissions, role→permission map, matrix helpers
├── deps.py             CurrentUser, guards (role/permission/patient-scope), audit(), pagination
├── naming.py           "Dr" honorific helpers shared by API, PDFs and notifications
├── routers/            21 routers — auth, users, patients, doctors, specialties, schedules, leaves,
│                       appointments, queue, consultations, medical_records, medical_files, medicines,
│                       prescriptions, invoices, payments, reviews, notifications, ai, analytics, admin
├── services/
│   ├── slots.py        Slot generation / availability / hold / release / book / expiry
│   ├── ai_engine.py    Safety scan → intent → concern → routing → state machine → reply
│   ├── ai_tools.py     18 registered tools + ToolContext + call logging
│   ├── patients.py     Patient creation/lookup helpers
│   ├── billing.py      Invoice numbering, totals, payment application
│   ├── documents.py    Prescription & invoice PDFs (reportlab)
│   ├── notify.py       Notification + WhatsApp service, templates, retries
│   ├── analytics.py    Aggregations for dashboards and reports
│   └── jobs.py         12 APScheduler jobs with run history
├── templates/          19 Jinja pages + shared UI kit (base.html)
├── static/             favicon + logo
├── seed.py             Idempotent demo seeder
├── web.py              /ui page routes
└── tools/              export_schema.py, export_seed.py (MySQL DDL/dump generators)
sql/                    01 setup · 02 schema (+views) · 03 seed · 04 verify
scripts/seed.py         CLI seeder wrapper
tests/smoke_test.py     End-to-end API test suite
tests/api_sweep.py      Live sweep of every GET route across all six roles
run.py                  Launcher (host/port/reload/workers/seed)
```

**Request flow (example: AI booking)**
`POST /api/v1/ai/chat` → `ai_engine.handle_message` → safety scan → intent classification →
`extract_concern` → `route_to_specialty` (symptom map) → `search_doctors` →
`get_available_slots` → `hold_slot` → user confirms → `book_appointment` → invoice created →
notification queued → tool calls, routing log and AI summary persisted.

---

## 6. API surface (`/api/v1`)

171 documented endpoints (`/docs` for the interactive UI). Highlights:

```
POST   /auth/login|register|logout|refresh|forgot-password|reset-password|change-password
GET    /auth/me|sessions|login-activity|audit-logs|audit-logs/summary        DELETE /auth/sessions/{id}
GET    /users|/users/{id}   PATCH /users/{id}   POST /users/{id}/unlock     GET /permissions/matrix
GET    /patients   POST /patients   GET /patients/{id}|{id}/summary|{id}/history|me
GET    /patients/search/suggest?q=
GET    /doctors   GET /doctors/{id}|{id}/availability|me/profile            POST /doctors
GET    /specialties   POST /specialties
GET    /schedules   POST /schedules   GET /schedules/working-days?doctor_id=
POST   /leaves   GET /leaves   POST /leaves/{id}/approve|reject
GET    /slots   GET /slots/available   POST /slots/hold   POST /slots/release
GET    /appointments|/today        POST /appointments
POST   /appointments/{id}/confirm|cancel|reschedule|complete|no-show|status
GET    /queue/desk   POST /queue/check-in|start-consultation|check-out|no-show
POST   /consultations   GET /consultations|follow-ups/due   PATCH /consultations/{id}   POST /{id}/complete
GET    /medical-records|/medical-files   POST /medical-files (multipart)   GET /medical-files/{id}/download
GET    /medicines   POST /medicines
POST   /prescriptions   GET /prescriptions   GET /prescriptions/{id}/pdf
GET    /invoices|/invoices/{id}   POST /invoices   GET /invoices/{id}/receipt (alias /pdf)
POST   /invoices/{id}/remind|discount|tax   POST /invoices/{id}/cancel
GET    /payments|/payments/reports/collection|/payments/refunds/all
POST   /payments   POST /payments/online/initiate   POST /payments/{id}/refund
POST   /payments/{id}/gateway-callback?status_value=PAID
GET    /reviews   POST /reviews   POST /reviews/{id}/approve|reject|flag
GET    /notifications   POST /notifications   GET /notifications/whatsapp|templates
POST   /ai/chat   GET /ai/monitor|monitor/routing|monitor/failures|tools|tool-calls|escalations|summaries
GET    /ai/conversations|{id}|{id}/messages   POST /ai/conversations/{id}/escalate|close
GET    /analytics/overview|revenue|appointments|doctors|patients|ai
GET    /admin/health|system|settings|jobs|jobs/runs   POST /admin/jobs/{name}/run
GET    /admin/dashboard|dashboard/doctor|dashboard/patient
```

Auth header: `Authorization: Bearer <access_token>` (also accepts `X-Access-Token`, and `?token=` for
PDF/file downloads so links work in the browser).

---

## 7. AI assistant

* **Intents:** greeting, thanks, concern, name, phone, slot/doctor selection, affirm/deny, book,
  cancel, reschedule, appointment status, prescription lookup, report lookup, payment/invoice status,
  notification request, review, human escalation, emergency.
* **Conversation memory:** every message stores intent + confidence + safety flag; the conversation
  keeps a stage machine (`IDLE → AWAIT_CONCERN → AWAIT_IDENTITY_NAME → AWAIT_IDENTITY_PHONE →
  AWAIT_DOCTOR_CHOICE → AWAIT_SLOT_CHOICE → AWAIT_CONFIRMATION → POST_BOOK`), a structured concern and a
  patient context summary. Names and concerns are disambiguated, so a symptom sentence is never stored
  as a patient name — the concern is remembered and asked for the name again.
* **Identity:** existing patient by phone (last 10 digits) or linked user account → "welcome back";
  otherwise a patient record is created on the fly and the code is echoed back.
* **Safety:** diagnosis/medicine/prescription requests are refused with an alternative
  ("I can find the right specialist, book a slot, pull up reports"); emergency phrases
  (chest pain + breathlessness, unconsciousness, heavy bleeding, stroke signs, poisoning, pregnancy
  bleeding, high fever with rash/stiff neck) trigger an immediate 108 recommendation, a doctor +
  reception escalation and a WhatsApp `EMERGENCY_ALERT`; after the safety reply the assistant offers to
  keep a priority slot ready and resumes the booking flow.
* **Observability:** AI monitor endpoints expose routing distribution, emergency/urgent counts,
  failures, tool usage and escalation queue with resolve actions.
* **Groq AI:** `AI_PROVIDER=groq` handles natural-language conversation and tool selection; hospital decisions and database operations remain in the backend tools
  in the deterministic rule engine, so safety behaviour never depends on a model.

Try it from the public page (`/ui` → chat widget), the AI assistant page (`/ui/ai-chat`) or the API:

```bash
curl -s localhost:8000/api/v1/ai/chat -H 'Content-Type: application/json' \
  -d '{"message":"fever since 3 days"}' | python3 -m json.tool
```

---

## 8. Background jobs

| Job | Schedule | Purpose |
|---|---|---|
| `generate_tomorrow_slots` | 23:30 daily | Roll the slot window forward one day |
| `expire_old_slots` | every 5 min | Release/expire held slots and mark past slots |
| `appointment_reminders` | 18:00 daily | WhatsApp reminder for tomorrow's appointments |
| `follow_up_reminders` | 09:00 daily | Follow-ups due today |
| `payment_reminders` | 11:00 daily | Pending invoice reminders |
| `doctor_daily_digest` | 20:00 daily | Tomorrow's list to each doctor |
| `review_requests` | 10:30 daily | Ask completed visits for a rating |
| `cleanup_temp_data` | 02:00 daily | Temp files, stale holds |
| `close_stale_sessions` | every 30 min | Expire idle sessions |
| `noshow_sweep` | 01:00 daily | Mark un-attended appointments `NO_SHOW` |
| `whatsapp_retry` | every 10 min | Retry failed WhatsApp sends |
| `notification_retry` | every 15 min | Retry failed notifications |

Runs are persisted in `job_runs`; trigger manually from `/ui/system` or
`POST /api/v1/admin/jobs/{name}/run`.

---

## 9. Testing & verification

```bash
python3 -m tests.smoke_test           # 89-check end-to-end suite on a live TestClient
python3 -m tests.page_check           # running server: all 19 pages + 40 read APIs + PDF downloads
python3 -m tests.api_sweep            # sweep every parameterless GET route with all 6 roles
python3 -m tests.api_sweep --all      # include write routes
python3 -m tests.mysql_checks         # MySQL-only checks (run against the MySQL-backed app)
python3 -m app.tools.export_schema sql/02_schema.sql
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
```

The smoke test covers: authentication and password flows, RBAC and lockout, slot hold/expiry/booking,
queue transitions, consultations, prescription + invoice PDFs, payments and refunds, reviews,
WhatsApp queueing, the full AI conversation (greeting → concern → identity → doctor → slot → booking),
AI safety refusals, emergency escalation, the 14 Groq tools, AI monitoring, dashboards, analytics, audit and
sessions, 9 background jobs and logout.

Latest runs: smoke test **89/89**, page + API check **69/69**, API sweep **450 route/role combinations with no 5xx and no 422** (the 4xx responses are intentional RBAC denials).

---

## 10. Security notes

* PBKDF2-HMAC-SHA256 (260k iterations, per-user salt) — never plain text, constant-time compare.
* JWT HS256 with `jti`; refresh rotation and server-side session revocation; idle-session closure job.
* Login attempt throttling and temporary lockout (`MAX_LOGIN_ATTEMPTS`, `LOCKOUT_MINUTES`).
* Permission checks on every write endpoint, patient-scope enforcement (`ensure_patient_scope`) so a
  patient token can only ever read its own records.
* Every security-relevant action is written to `audit_logs` (actor, role, IP, user agent, before/after).
* Uploads: MIME whitelist, size limit, content stored as `sha256[:12]_originalname` under the patient code.
* The AI never diagnoses, prescribes or gives medical advice; emergency text triggers escalation.

**Before going live:** set `JWT_SECRET`, change every password from `sql/01_setup_database.sql`,
`APP_ENV=production`, enable TLS behind a reverse proxy, and schedule
`mysqldump` with the `vvh_backup` account.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `MySQL connection failed` | Start MySQL/MariaDB, create the database with `sql/01_setup_database.sql`, and check `DB_*` or `DATABASE_URL` in `.env`. |
| `Access denied for user 'vvh_app'` | Re-run script 01 (grants) or `ALTER USER 'vvh_app'@'localhost' IDENTIFIED BY '...'`. |
| Port already in use | `python3 run.py --port 8010` |
| Tables missing | `python3 -m app.seed` (or `init_db()` runs automatically at startup). On MySQL run `sql/02_schema.sql` first. |
| `ERROR 1221` / `ERROR 1146` / `ERROR 1061` / `ERROR 1364` while running the SQL scripts | fixed in this version — re-pull `sql/01`–`04` (see MYSQL_SETUP.md §8) |
| No slots to book | `python3 -m app.seed` regenerates 14 days; check doctor schedules/leaves for that weekday |
| WhatsApp not delivering | Set `WHATSAPP_ENABLED=true` + Twilio credentials; otherwise messages stay queued (visible in `/ui/system`) |
| Reset password link not visible | In development the API returns `reset_token` in the response and the UI shows it directly (no SMTP needed). |

## AI provider: Groq

This version uses the Groq API as the only LLM provider. The model receives only the 14 hospital tools listed below. It never receives direct database access.

1. `get_patient_profile()`
2. `get_patient_appointments()`
3. `search_specialties()`
4. `search_doctors()`
5. `get_doctor_availability()`
6. `get_available_slots()`
7. `hold_slot()`
8. `release_slot()`
9. `book_appointment()`
10. `cancel_appointment()`
11. `reschedule_appointment()`
12. `get_invoice()`
13. `get_payment_status()`
14. `send_notification()`

Set `GROQ_API_KEY` in your local `.env`. Do not commit `.env` or API keys to source control.

## Database: MySQL only

SQLite fallback has been removed. The application requires MySQL and will fail fast if it cannot connect. Configure `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, or provide a MySQL `DATABASE_URL`.
