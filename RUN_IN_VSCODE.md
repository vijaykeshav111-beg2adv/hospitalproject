# Run this project in VS Code

Vijay Vargiya Group of Hospitals — FastAPI + MySQL hospital management platform.
This guide takes you from "unzipped folder" to "logged in" in about five minutes.

---

## 0. What you need

| Requirement | Notes |
|---|---|
| **Python 3.10 – 3.13** | `python --version` (Windows: `py -3 --version`) |
| **VS Code** + the **Python extension** | The editor will suggest the rest from `.vscode/extensions.json` |
| **MySQL 8.0+ or MariaDB 10.4+** *(required)* | The application requires MySQL-compatible SQL and fails fast if it cannot connect |
| `mysql` CLI on PATH *(optional)* | Only needed for the SQL setup scripts (MySQL Workbench can run them too) |

---

## 1. Open the folder

1. Unzip `vijay_vargiya_hospital.zip`.
2. VS Code → **File ▸ Open Folder…** → select the `vijay_vargiya_hospital` folder.
3. If VS Code asks to install the recommended extensions → **Install**.

## 2. Create the virtual environment

Open the VS Code terminal (**Ctrl + `**) and run:

```powershell
# Windows (PowerShell)
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Equivalent one-click route: **Terminal ▸ Run Task… ▸ “1. Create virtual environment”** then
**“2. Install dependencies”**.

Then select the interpreter: **Ctrl+Shift+P ▸ Python: Select Interpreter ▸ ./.venv** (the tasks and
launch configs already assume `.venv`, so this makes F5 work straight away).

---

## 3. Choose your database

### Option A — MySQL (recommended, this is what the project is built for)

Run the four scripts in order. Terminal:

```bash
mysql -u root -p < sql/01_setup_database.sql
mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql
mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
```

Windows PowerShell doesn't support `<` redirection — either use the VS Code tasks
(**Terminal ▸ Run Task… ▸ “3a … 3d”**, which wrap the command in `cmd /c`), or:

```powershell
Get-Content sql\01_setup_database.sql | mysql -u root -p
Get-Content sql\02_schema.sql      | mysql -u root -p vijay_vargiya_hospital
Get-Content sql\03_seed.sql        | mysql -u root -p vijay_vargiya_hospital
Get-Content sql\04_verify.sql      | mysql -u root -p vijay_vargiya_hospital
```

`01` creates the database, the dedicated application user `vvh_app`, the reporting/backup/migration
users, grants and runs `FLUSH PRIVILEGES`. `04_verify.sql` prints one row per sanity check — every
row should read **OK**. Details, backups and troubleshooting: **[MYSQL_SETUP.md](MYSQL_SETUP.md)**.

Credentials are already wired into `.env`:

```ini
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=vijay_vargiya_hospital
DB_USER=vvh_app
DB_PASSWORD=change_me
```

If your MySQL password for `vvh_app` differs, edit `.env` (or `.env.example` for a fresh copy).

### Option B — No SQLite fallback

There is no SQLite runtime mode. Configure MySQL in `.env` before starting the application.

## 4. Seed demo data and start the app

```bash
python -m app.seed     # stage aware: adds only what is missing (patients, AI samples, 14 days of slots)
python run.py          # http://127.0.0.1:8000/ui
```

Or press **F5** and pick **“Run hospital app (run.py)”** — a debugger is attached, so you can set
breakpoints in any router or service. **“Run hospital app (reload + debugger)”** gives auto-reload.
Or **Terminal ▸ Run Task… ▸ “5. Run the app”**.

Open <http://127.0.0.1:8000/ui>. API explorer: `/docs`. Health: `/api/v1/admin/health` (it prints
the configured **mysql** backend).

### Demo logins

| Role | Email | Password |
|---|---|---|
| SUPER_ADMIN | `superadmin@vijayvargiiyahospital.in` | `SuperAdmin@123` |
| ADMIN | `admin@vijayvargiiyahospital.in` | `Admin@123` |
| DOCTOR | `dr.arjunmehra@vijayvargiiyahospital.in` | `Doctor@123` |
| RECEPTIONIST | `reception@vijayvargiiyahospital.in` | `Reception@123` |
| ACCOUNTANT | `accounts@vijayvargiiyahospital.in` | `Accounts@123` |
| PATIENT | `ramesh.yadav@example.com` | `Patient@123` |

Every seeded doctor uses `Doctor@123`; every seeded patient uses `Patient@123`.

---

## 5. Verify your install (optional but recommended)

```bash
python -m tests.smoke_test      # 89-check end-to-end suite (auth, slots, queue, billing, AI, jobs…)
python -m tests.page_check      # run while the server is up: 19 pages + 40 APIs + PDF downloads
python -m tests.api_sweep       # every parameterless route × all 6 roles
python -m tests.mysql_checks    # MySQL-only checks: constraints, FKs, utf8mb4, money, views
```

---

## 6. Handy VS Code tasks

**Terminal ▸ Run Task…**

| Task | What it does |
|---|---|
| 1. Create virtual environment | `python -m venv .venv` |
| 2. Install dependencies | `pip install -r requirements.txt` |
| 3a–3d. MySQL: apply 01–04 | Runs each SQL script (Windows-safe wrapper included) |
| 4. Seed demo data | `python -m app.seed` |
| 5. Run the app | Starts the server on port 8000 |
| Run test suite (89 checks) | `python -m tests.smoke_test` |
| Check UI + API of a running server | `python -m tests.page_check` |
| MySQL behaviour checks | `python -m tests.mysql_checks` |
| Regenerate SQL from models | Rewrites `sql/02_schema.sql` + `sql/03_seed.sql` from the SQLAlchemy models |

---

## 7. Everyday commands

```bash
python run.py --reload                 # development, auto-reload
python run.py --port 8010              # different port
python run.py --workers 4              # production-ish (scheduler runs in one worker only)
python run.py --no-scheduler           # disable the 12 background jobs
python scripts/seed.py --force         # rebuild the demo dataset from scratch
python -m app.tools.export_schema sql/02_schema.sql     # regenerate MySQL DDL
python -m app.tools.export_seed   sql/03_seed.sql       # regenerate master-data seed
```

Any ASGI server works too:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: fastapi` | The virtual environment isn't active or selected — rerun step 2, then **Python: Select Interpreter → .venv** |
| `'python' is not recognized` (Windows) | Use `py -3` instead, or install Python with “Add python.exe to PATH” |
| PowerShell blocks `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or activate via `cmd` |
| `MySQL connection failed` | Start MySQL and verify `DB_*` / `DATABASE_URL` in `.env` |
| `ERROR 1045 Access denied for user 'vvh_app'` | Re-run `sql/01_setup_database.sql`, or update `DB_PASSWORD` in `.env` |
| `ERROR 1049 Unknown database` | `sql/01_setup_database.sql` wasn't applied |
| `mysql: command not found` | Add MySQL's `bin` folder to PATH, or run the scripts from MySQL Workbench (open each `sql/*.sql` and execute) |
| Port 8000 already in use | `python run.py --port 8010` |
| Empty slot list / no patients | `python -m app.seed` |
| Hindi text shows as `???` | Connect with `--default-character-set=utf8mb4` (the schema is already utf8mb4) |
| Windows: `FileNotFoundError` on an upload path | The app creates `storage/` automatically; make sure you unzipped into a writable folder (not `Program Files`) |
| PDF downloads 401 | Downloads need the token in the URL (`?token=…`), which the UI adds automatically |

---

## 9. Project layout in one screen

```
app/main.py            FastAPI app factory (lifespan: init_db → seed → scheduler), /api/v1 + /ui
app/models.py          40 SQLAlchemy tables  →  sql/02_schema.sql is generated from this
app/routers/           21 routers (auth, patients, slots, queue, billing, AI, admin…)
app/services/          slot engine, AI engine + 18 tools, billing, PDFs, notifications, jobs
app/templates/         19 Jinja pages (original UI) + shared kit in base.html
sql/                   01 setup · 02 schema (+views) · 03 seed · 04 verify
tests/                 smoke_test, page_check, api_sweep, mysql_checks
run.py                 launcher (host/port/reload/workers/seed)
.env                   active configuration (MySQL creds, JWT, Twilio, AI provider)
MYSQL_SETUP.md         full MySQL guide: users, grants, backup, restore, privileges, errors
README.md              complete feature/architecture reference
```

Everything is Python 3.10+ with FastAPI, SQLAlchemy 2, PyMySQL, PyJWT, Jinja2, APScheduler and
reportlab — see `requirements.txt`. Change the `JWT_SECRET` and every seeded password before using
this anywhere beyond your machine.
