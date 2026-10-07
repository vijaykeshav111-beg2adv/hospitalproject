# MySQL Setup — Vijay Vargiya Group of Hospitals

Everything needed to run this project on **MySQL** as the required database backend:
database creation, a dedicated application user, grants, `FLUSH PRIVILEGES`, the 40-table
schema, seed data, verification, backups and troubleshooting.

> Every command below was executed against a real server while building this project
> (MariaDB 11.8 / MySQL-compatible, InnoDB, `utf8mb4`). The results are recorded in
> [§8 Verification log](#8-verification-log-what-actually-ran) — including the five
> script bugs that were found and fixed in the process.

---

## 1. TL;DR — four commands

```bash
cd vijay_vargiya_hospital

mysql -u root -p < sql/01_setup_database.sql                                    # database + users + grants + FLUSH PRIVILEGES
mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql                     # 40 tables + 4 reporting views (idempotent)
mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql                       # RBAC, specialties, medicines, doctors, holidays
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql                     # sanity check: every row should say OK

python3 -m app.seed                                                             # demo patients, AI samples, 14 days of slots
python3 run.py                                                                  # http://127.0.0.1:8000/ui  on MySQL
```

Requirements: MySQL 8.0+ **or** MariaDB 10.4+, the `mysql` CLI client, and the Python
packages in `requirements.txt` (`PyMySQL` and `cryptography` are already there — they are
what SQLAlchemy uses to talk to MySQL).

---

## 2. Step 1 — database, users, grants (`sql/01_setup_database.sql`)

```bash
mysql -u root -p < sql/01_setup_database.sql
```

What it does:

| # | Action |
|---|---|
| 1 | Drops and creates `vijay_vargiya_hospital` with `CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci` |
| 2 | Creates 5 accounts (table below) |
| 3 | Grants least-privilege access per account |
| 4 | Runs `FLUSH PRIVILEGES` so the grants apply immediately |
| 5 | Prints `SHOW GRANTS` for the app and reporting users as a sanity check |
| 6 | Sets server globals: `time_zone='+05:30'`, `max_connections=200`, `innodb_file_per_table=ON` |

> The reporting **views** used to be created here; they now live at the end of
> `sql/02_schema.sql` because a view cannot be created before its tables exist
> (`ERROR 1146 Table ... doesn't exist` on a fresh install). `01` says so in a comment.

### Accounts it creates

| User | Password | Purpose | Privileges |
|---|---|---|---|
| `vvh_app@localhost` + `vvh_app@%` | `change_me` | The FastAPI application at runtime | `SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW, CREATE TEMPORARY TABLES` on this schema only — **no DDL** |
| `vvh_readonly@localhost` | `change_me_readonly` | Analytics / BI / reporting | `SELECT, SHOW VIEW, EXECUTE` (also on the 4 views) |
| `vvh_backup@localhost` | `change_me_backup` | `mysqldump` backups | `SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER` on the schema + `RELOAD` on `*.*` |
| `vvh_migrator@localhost` | `change_me_migrator` | Schema changes / migrations only | `ALL PRIVILEGES` on this schema |

`RELOAD` is granted separately on `*.*` because MySQL rejects mixing a global privilege
into a database-level `GRANT` (`ERROR 1221 Incorrect usage of DB GRANT and GLOBAL PRIVILEGES`).

**Change all four passwords before going live** — see [§7 Rotating credentials](#7-rotating-credentials).

---

## 3. Step 2 — schema (`sql/02_schema.sql`)

```bash
mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql
```

* **40 tables** — RBAC/sessions/audit/jobs, patients, doctors/schedules/leaves/holidays, slots,
  appointments, consultations, medical records/files, medicines, prescriptions, invoices,
  payments/refunds, reviews, notifications, WhatsApp messages, AI conversations/messages/concerns/
  routing/tool-calls/summaries/escalations.
* **4 reporting views** at the end of the file: `v_appointments_today`, `v_outstanding_invoices`,
  `v_doctor_daily_load`, `v_ai_daily_summary` — granted to `vvh_readonly` and `vvh_app`.
* Every table is `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci` with
  primary keys, unique keys, indexes and foreign keys (196 index entries, 40+ FKs).
* The script is **idempotent**: `CREATE TABLE IF NOT EXISTS` plus indexes declared *inline*
  (mysqldump style). You can re-run it safely; standalone `CREATE INDEX` statements were removed
  because MySQL 8 has no `CREATE INDEX IF NOT EXISTS` (MariaDB-only) and a re-run failed with
  `ERROR 1061 Duplicate key name`.

The schema is **generated from the SQLAlchemy models** so it can never drift from the ORM:

```bash
python3 -m app.tools.export_schema sql/02_schema.sql     # writes the DDL
python3 -m app.tools.export_seed   sql/03_seed.sql       # writes the master-data seed
```

---

## 4. Step 3 — seed data (`sql/03_seed.sql`)

```bash
mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql
```

Creates: **6 roles**, **50 permissions**, **175 role→permission mappings**, **4 staff logins**,
**8 doctor logins + doctor profiles + 48 weekly schedules (6 working days each)**, **12 specialties
with 107 symptom keywords**, **10 medicines**, **5 holidays** — all written with
`INSERT ... ON DUPLICATE KEY UPDATE` / `NOT EXISTS` guards, so the file is re-runnable.

The generator refuses to mislead you:

* a **column-coverage audit** verifies every `NOT NULL` column without a server default is present in
  the generated `INSERT` column list (this is what caught the missing `users.phone_verified`, which
  MySQL strict mode rejected with `ERROR 1364 Field 'phone_verified' doesn't have a default value`);
* a **statement-order audit** flags `INSERT ... SELECT` that reads a table populated *later* in the
  file — the bug that silently created **0 doctors** because the doctor block ran before the
  specialties it selects from.

Both audits print `0 warnings` when clean:

```bash
$ python3 -m app.tools.export_seed sql/03_seed.sql
Wrote sql/03_seed.sql (0 warning(s))
```

---

## 5. Step 4 — verify (`sql/04_verify.sql`)

```bash
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
```

It prints, in order: **expected-data sanity** (one row per check with `OK` / `FAIL` / `INFO`),
an **idempotency probe** (inserts a probe row inside a transaction and rolls it back), the table
inventory with engines/collations, exact row counts, the role→permission matrix, user accounts,
`SHOW GRANTS` for the app user, and upcoming slots per doctor.

Sample of the sanity section:

```
expected                                     result  actual_value
roles seeded (6)                             OK      6
permissions (50)                             OK      50
role_permissions (175)                       OK      175
specialties (12)                             OK      12
specialty_concerns (107)                     OK      107
medicines (10)                               OK      10
doctors (8)                                  OK      8
doctor_schedules (>= 48 = 8 doctors x 6 days) OK     48
every doctor has a schedule                  OK      0
reporting views (4)                          OK      4
all tables InnoDB                            OK      0
demo patients (0 until app.seed runs)        INFO    0
future slots (0 until app.seed runs)         INFO    0
```

`INFO` rows are expected right after the SQL seed: the SQL scripts deliberately stop at master data.
Demo patients and the 14-day slot window come from the Python seeder in the next step.

---

## 6. Step 5 — point the application at MySQL

`.env` in the project root (a ready-to-edit `.env` already ships with these values):

```ini
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=vijay_vargiya_hospital
DB_USER=vvh_app
DB_PASSWORD=change_me

DATABASE_URL=                 # leave blank to build the URL from DB_* above
```

Then:

```bash
python3 -m app.seed      # stage-aware: adds demo patients + AI samples + 14 days of slots
python3 run.py           # startup banner prints: Database : mysql -> vijay_vargiya_hospital
```

`python3 -m app.seed` is **stage aware** — it fills only what is missing:

```
Seed complete: {'skipped': False,
  'existing': {'roles': 6, 'permissions': 50, 'specialties': 12, 'medicines': 10,
               'users': 12, 'doctors': 8, 'patients': 0, 'ai_conversations': 0, 'future_slots': 0},
  'seeded':   {'patients': True, 'ai_samples': True, 'slots_created': 1339}}
```

Run it twice and the second run reports `Nothing to seed - database already complete`. The app also
calls it during startup, so booting the server completes an empty database automatically.

Confirm you are really on MySQL:

```bash
$ curl -s localhost:8000/api/v1/admin/health | python3 -m json.tool
{"service": "ok", "database": {"status": "up", "backend": "mysql", "database": "vijay_vargiya_hospital"}}
```

---

## 7. Backups, restore and credential rotation

### Backup (uses the dedicated `vvh_backup` account — tested)

```bash
mysqldump -u vvh_backup -p \
  --single-transaction --routines --triggers --events \
  vijay_vargiya_hospital > /backup/vvh_$(date +%F_%H%M).sql
```

Nightly cron example (02:30, keep 30 days):

```cron
30 2 * * * mysqldump -u vvh_backup -p --single-transaction --routines --triggers \
  --events vijay_vargiya_hospital | gzip > /backup/vvh_$(date +\%F).sql.gz && \
  find /backup -name 'vvh_*.sql.gz' -mtime +30 -delete
```

### Restore

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS vijay_vargiya_hospital CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
gunzip -c /backup/vvh_2026-10-04.sql.gz | mysql -u root -p vijay_vargiya_hospital
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
```

### Rotating credentials

```sql
-- 1. change the password
ALTER USER 'vvh_app'@'localhost' IDENTIFIED BY 'NewStrongAppPassword!';
ALTER USER 'vvh_app'@'%'         IDENTIFIED BY 'NewStrongAppPassword!';
FLUSH PRIVILEGES;

-- 2. update .env -> DB_PASSWORD=NewStrongAppPassword!
-- 3. restart the app
```

Useful operational queries:

```sql
-- active sessions and connections
SELECT id, user, host, db, command, time FROM information_schema.processlist ORDER BY time DESC;
-- table sizes (MB)
SELECT table_name, ROUND((data_length+index_length)/1048576, 2) AS mb
FROM information_schema.tables WHERE table_schema='vijay_vargiya_hospital' ORDER BY mb DESC;
-- slowest statements (if the slow log is on)
SELECT * FROM mysql.slow_log ORDER BY query_time DESC LIMIT 10;
-- privilege report
SHOW GRANTS FOR 'vvh_app'@'localhost';
-- start over with an empty demo (destructive)
DROP DATABASE vijay_vargiya_hospital;   -- then re-run 01 -> 02 -> 03 -> 04
```

---

## 8. Verification log (what actually ran)

Environment: Debian 13 sandbox, **MariaDB 11.8.6** (MySQL-protocol compatible), `utf8mb4`,
InnoDB, application started with `.env` pointing at `vvh_app@127.0.0.1/vijay_vargiya_hospital`.

| Check | Command | Result |
|---|---|---|
| Bootstrap script | `mysql < sql/01_setup_database.sql` | exit 0 — 5 users, grants, `FLUSH PRIVILEGES`, views moved out |
| Schema | `mysql vijay_vargiya_hospital < sql/02_schema.sql` | exit 0 — 40 tables, 4 views, 196 index entries, re-run also exit 0 (**idempotent**) |
| Seed | `mysql vijay_vargiya_hospital < sql/03_seed.sql` | exit 0 — 6 roles, 50 perms, 175 mappings, 12 users, 8 doctors, 48 schedules |
| Verify | `mysql vijay_vargiya_hospital < sql/04_verify.sql` | every sanity row `OK`, idempotency probe rolled back cleanly |
| Demo data | `python3 -m app.seed` | `slots_created: 1339` + 12 patients + AI samples; second run seeds nothing |
| App health | `GET /api/v1/admin/health` | `"backend": "mysql"`, `"status": "up"` |
| Full test suite | `python3 -m tests.smoke_test` | **89 passed, 0 failed** on MySQL |
| UI + API pages | `python3 -m tests.page_check` | **69 passed, 0 failed** |
| Route sweep | `python3 -m tests.api_sweep` | 450 route/role combinations, **no 5xx, no 422** |
| MySQL behaviours | `python3 -m tests.mysql_checks` | **35 passed, 0 failed** |
| End-to-end flow | live HTTP walkthrough | patient → hold → book (invoice) → queue → consultation → prescription PDF → payment → invoice `PAID` → receipt PDF |
| Backup | `mysqldump -u vvh_backup …` | exit 0, 457 KB dump, 40 `CREATE TABLE` statements |
| Restore | dump → `vvh_restore_test` | exit 0 — 40 tables, 20 patients, 33 appointments, 1,369 slots restored |
| Grant model | as `vvh_readonly` / `vvh_app` / `vvh_migrator` | readonly `UPDATE` denied (`1142`), app `CREATE TABLE` denied (`1142`), migrator DDL allowed |

`tests/mysql_checks.py` covers things that differ from SQLite: strict-mode `NOT NULL` enforcement
(`ERROR 1364`), foreign-key rejection of orphan rows (`ERROR 1452`), transaction rollback on a failed
booking, a utf8mb4 round trip (`सुनीता देवी 🩺` stored and read back intact, 4-byte emoji included),
`DECIMAL` money with no rounding drift, slot-hold exclusivity (second hold on the same slot → `409`
with exactly one `HELD` row), JSON memory round-trip, and the four reporting views.

### Five script bugs found by running this for real (all fixed)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `ERROR 1221 Incorrect usage of DB GRANT and GLOBAL PRIVILEGES` at line 48 of `01` | `RELOAD` (a global privilege) mixed into a database-level `GRANT` for `vvh_backup` | Split into a schema-level grant plus `GRANT RELOAD ON *.*` |
| 2 | `ERROR 1146 Table ... appointments doesn't exist` while creating views in `01` | Views were created before the tables | Views moved to the end of `02_schema.sql`, granted there |
| 3 | `ERROR 1061 Duplicate key name 'ix_holidays_holiday_date'` on the second run of `02` | Standalone `CREATE INDEX` is not idempotent (MySQL 8 has no `IF NOT EXISTS` for indexes) | Indexes are now emitted **inside** `CREATE TABLE` (mysqldump style) |
| 4 | `ERROR 1364 Field 'phone_verified' doesn't have a default value` in `03` | Generated `INSERT` for `users` omitted a `NOT NULL` column | Column added **and** a column-coverage audit added to the generator so it cannot recur |
| 5 | Seed reported success but `doctors`/`doctor_schedules` were **empty** | The doctor block ran *before* the specialties it `INSERT ... SELECT`s from — an empty source table silently inserts 0 rows | Seed blocks reordered (masters first) + statement-order audit added to the generator |

---

## 9. Remote / production deployment

```ini
# /etc/mysql/mysql.conf.d/mysqld.cnf
bind-address = 0.0.0.0            # or the private interface only
max_connections = 200
innodb_buffer_pool_size = 1G      # ~50-60% of RAM on a dedicated DB host
slow_query_log = 1
long_query_time = 1
```

```sql
-- app server on 10.0.0.5 must connect as vvh_app@'10.0.0.%' (created by script 01 via vvh_app@'%')
CREATE USER 'vvh_app'@'10.0.0.5' IDENTIFIED BY 'StrongPassword!';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW, CREATE TEMPORARY TABLES
  ON `vijay_vargiya_hospital`.* TO 'vvh_app'@'10.0.0.5';
FLUSH PRIVILEGES;
```

```bash
# firewall: allow 3306 only from the app server
sudo ufw allow from 10.0.0.5 to any port 3306 proto tcp
```

Then in the app's `.env`: `DB_HOST=10.0.0.10`,
`APP_ENV=production`, a strong `JWT_SECRET`, and TLS-terminated HTTP in front of `run.py`.
For multiple app workers: `python3 run.py --workers 4` (the APScheduler jobs are guarded so only one
worker runs them).

---

## 10. Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `ERROR 1045 Access denied for user 'vvh_app'` | wrong password or missing user/host | Re-run `sql/01_setup_database.sql`, or `ALTER USER 'vvh_app'@'localhost' IDENTIFIED BY '…'`; check `SELECT user,host FROM mysql.user WHERE user LIKE 'vvh%'` |
| `ERROR 1049 Unknown database 'vijay_vargiya_hospital'` | `01` not run (or dropped) | `mysql -u root -p < sql/01_setup_database.sql` |
| `ERROR 2003 Can't connect to MySQL server` | server down / wrong host-port / firewall | `systemctl status mysql` (or `mariadb`), then check `DB_HOST`/`DB_PORT` |
| `ERROR 1142 … command denied to user 'vvh_app'` | app user is DML-only by design | Run DDL with `vvh_migrator` or `root` |
| `ERROR 1146 Table … doesn't exist` | schema not applied, or seed run before schema | Run `02_schema.sql`, then `03_seed.sql` |
| `ERROR 1452 Cannot add or update a child row` | FK violation — referenced row is missing | Create the parent first (this is the protection working) |
| `ERROR 1364 Field 'x' doesn't have a default value` | strict mode + missing `NOT NULL` column | Regenerate `03_seed.sql` (`python3 -m app.tools.export_seed`) — the audit will name the column |
| `ERROR 1061 Duplicate key name` | an old-style standalone `CREATE INDEX` | Regenerate `02_schema.sql` (`python3 -m app.tools.export_schema`) |
| App logs `MySQL connection failed` | credentials/host wrong, or server down | Fix `.env` and start MySQL |
| Slots table empty after setup | `app.seed` not run | `python3 -m app.seed` (generates 14 days from the doctors' schedules) |
| Hindi/emoji text becomes `???` | client not speaking utf8mb4 | Connect with `--default-character-set=utf8mb4`; the schema itself is already utf8mb4 |

---

## 11. Re-generating the SQL from the models

Models are the single source of truth (`app/models.py`), so after changing a model:

```bash
python3 -m app.tools.export_schema sql/02_schema.sql     # DDL incl. inline indexes + views
python3 -m app.tools.export_seed   sql/03_seed.sql       # master data + audits

mysql -u root -p < sql/01_setup_database.sql
mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql
mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql
mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
```

On an existing database `ALTER TABLE` is not generated automatically — for a live system use the
`vvh_migrator` account with an explicit migration (or SQLAlchemy/Alembic) rather than re-creating the
schema, and take a `mysqldump` first.
