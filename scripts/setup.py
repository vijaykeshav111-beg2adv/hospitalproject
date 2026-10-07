#!/usr/bin/env python3
"""
One-command setup + health check for Vijay Vargiya Group of Hospitals.

WHY THIS EXISTS
---------------
sql/01_setup_database.sql creates the application user with the placeholder
password 'CHANGE_ME_APP_PASSWORD'. If your .env uses a different password (very
likely), the app then fails with:

    (1045, "Access denied for user 'vvh_app'@'localhost'")

This script reads your .env and creates the database AND the user with exactly
the credentials in .env - so they can never disagree. It also finishes the job:
tables, reporting views, RBAC and demo data.

USAGE
-----
    python scripts/setup.py --check              # diagnose only, changes nothing
    python scripts/setup.py                      # full setup (asks for root password)
    python scripts/setup.py --root-password X    # non-interactive
    python scripts/setup.py --reset              # DROP the database, then set up fresh

    MYSQL_ROOT_PASSWORD=X python scripts/setup.py     # or via environment
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

MIN_PYTHON = (3, 10)
OK, BAD, WARN = "  [ OK ]", "  [FAIL]", "  [WARN]"


# ---------------------------------------------------------------------------
# small reporting helpers
# ---------------------------------------------------------------------------
def step(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def line(status: str, text: str) -> None:
    print(f"{status} {text}")


def mask(value: str, keep: int = 2) -> str:
    if not value:
        return "(empty)"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def sql_quote(value: str) -> str:
    """Escape a value for use inside a MySQL string literal.

    MySQL treats backslash as an escape character by default, so backslashes are
    doubled BEFORE quotes - otherwise 'a\\'b' would escape the closing quote.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def check_python() -> bool:
    step("1. Python")
    version = sys.version_info
    line(OK if version >= MIN_PYTHON else BAD,
         f"Python {version.major}.{version.minor}.{version.micro} "
         f"(need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)")
    print(f"         interpreter: {sys.executable}")
    if version < MIN_PYTHON:
        line(BAD, "Upgrade Python, then re-run this script.")
        return False
    return True


def check_packages() -> tuple[bool, list[str]]:
    step("2. Required packages")
    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "pymysql": "pymysql",
        "jwt": "PyJWT",
        "jinja2": "jinja2",
        "multipart": "python-multipart",
        "email_validator": "email-validator",
        "reportlab": "reportlab",
        "apscheduler": "APScheduler",
    }
    missing: list[str] = []
    for module, package in required.items():
        try:
            __import__(module)
            line(OK, package)
        except Exception as exc:  # noqa: BLE001
            line(BAD, f"{package}  ->  {exc.__class__.__name__}")
            missing.append(package)

    for module, package in (("groq", "groq"), ("httpx", "httpx")):
        try:
            __import__(module)
            line(OK, f"{package} (optional)")
        except Exception:  # noqa: BLE001
            line(WARN, f"{package} (optional) missing")

    if missing:
        print("\n  Install what is missing:")
        print(f"    pip install {' '.join(missing)}")
        print("    # or simply:  pip install -r requirements.txt")
    return not missing, missing


def check_env_file() -> bool:
    step("3. Environment file (.env)")
    env_path = BASE_DIR / ".env"
    example = BASE_DIR / ".env.example"

    if not env_path.exists():
        line(BAD, f"{env_path} is missing")
        if example.exists():
            print("\n  Create it first:")
            print("    copy .env.example .env          (Windows)")
            print("    cp .env.example .env            (macOS / Linux)")
            print("  Then set DB_USER / DB_PASSWORD / JWT_SECRET inside it.")
        return False

    line(OK, f"{env_path} found")
    return True


def check_settings() -> bool:
    step("4. Configuration")
    try:
        from app.config import settings
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"Could not load app.config: {exc}")
        print("         Is the virtual environment activated?  pip install -r requirements.txt")
        return False

    line(OK, f"database   : {settings.db_host}:{settings.db_port}/{settings.db_name}")
    line(OK, f"db user    : {settings.db_user}")
    line(OK, f"db password: {mask(settings.db_password)}  (from "
              f"{'DB_PASSWORD' if settings._db_password_env else 'DATABASE_URL'})")

    for warning in settings.database_config_warnings():
        line(WARN, warning)

    placeholder = {"", "change-this-to-a-long-random-secret-string-in-production",
                   "change-me", "secret"}
    if settings.jwt_secret in placeholder or len(settings.jwt_secret) < 32:
        line(WARN, "JWT_SECRET is a placeholder or too short - anyone who knows it "
                   "can forge a SUPER_ADMIN login.")
        print("         generate one:  python -c \"import secrets; "
              "print(secrets.token_urlsafe(48))\"")
    else:
        line(OK, "JWT_SECRET looks strong")

    line(OK, f"AI mode    : {settings.ai_mode} (model {settings.groq_model}, "
              f"key {'set' if settings.groq_api_key else 'not set'})")
    if settings.ai_mode == "local":
        print("         AI runs locally, no API key needed. Add GROQ_API_KEY for nicer wording.")
    return True


def check_database(expect_schema: bool = True) -> bool:
    step("5. Database connection (as the application user)")
    try:
        from app.config import settings
        import pymysql
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"cannot import driver: {exc}")
        return False

    try:
        conn = pymysql.connect(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, database=settings.db_name,
            charset="utf8mb4", connect_timeout=8,
        )
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else "?"
        line(BAD, f"connection failed ({code}): {exc.args[-1] if exc.args else exc}")

        if code == 1045:
            print("\n  Access denied - the password in .env does not match MySQL.")
            print("  Fix it with (enter your MySQL root password when asked):")
            print(f"    mysql -u root -p -e \"ALTER USER '{settings.db_user}'@'localhost' "
                  f"IDENTIFIED BY '{settings.db_password}'\"")
            print("  ...or just run:  python scripts/setup.py   (it sets this up for you)")
        elif code == 1049:
            print(f"\n  Database '{settings.db_name}' does not exist.")
            print("  Fix it with:  python scripts/setup.py")
        elif code == 2003:
            print(f"\n  Nothing is listening on {settings.db_host}:{settings.db_port}.")
            print("  Start MySQL:")
            print("    Windows : net start MySQL80     (or start it from services.msc)")
            print("    macOS   : brew services start mysql")
            print("    Linux   : sudo systemctl start mysql")
        return False

    line(OK, "connected")

    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        line(OK, f"server version: {cur.fetchone()[0]}")

        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE'", (settings.db_name,))
        tables = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM information_schema.views "
                    "WHERE table_schema=%s", (settings.db_name,))
        views = cur.fetchone()[0]

    if tables == 0:
        line(WARN, "no tables yet - they are created on first start (or by this script)")
    else:
        line(OK, f"{tables} tables")

    if views == 0:
        line(WARN, "reporting views missing - run this script or start the app once")
    else:
        line(OK, f"{views} reporting views")

    if expect_schema and tables:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM roles")
            roles = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users")
            users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM doctors")
            doctors = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM patients")
            patients = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM slots WHERE status='AVAILABLE'")
            slots = cur.fetchone()[0]

        print()
        line(OK if roles == 6 else WARN, f"roles    : {roles} (expect 6)")
        line(OK if users else WARN, f"users    : {users}")
        line(OK if doctors else WARN, f"doctors  : {doctors}")
        line(OK if patients else WARN, f"patients : {patients}")
        line(OK if slots else WARN, f"open slots: {slots}")

        if not users or not doctors or not slots:
            print("\n  Demo data is missing. Fix it with:")
            print("    python -m app.seed")

    conn.close()
    return True


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def connect_root(args) -> "pymysql.connections.Connection":
    import pymysql

    if args.socket:
        return pymysql.connect(unix_socket=args.socket, user=args.root_user,
                               password=args.root_password, charset="utf8mb4",
                               autocommit=True)

    try:
        return pymysql.connect(host=args.root_host, port=args.root_port,
                               user=args.root_user, password=args.root_password,
                               charset="utf8mb4", autocommit=True)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else "?"
        print(f"\n  Could not connect to MySQL as '{args.root_user}' ({code}).")
        if code == 1045:
            print("  Wrong root password. Try again, or pass it directly:")
            print("    python scripts/setup.py --root-password YOUR_ROOT_PASSWORD")
        elif code == 2003:
            print(f"  Nothing is listening on {args.root_host}:{args.root_port}. Start MySQL first.")
            print("    Windows: net start MySQL80   |   macOS: brew services start mysql"
                  "   |   Linux: sudo systemctl start mysql")
        raise SystemExit(2) from exc


def run_setup(args) -> int:
    from app.config import settings

    step("Creating database, user and grants")
    conn = connect_root(args)
    print(f"  [ OK ] connected to MySQL as '{args.root_user}'")

    db_name = settings.db_name if not args.db_name else args.db_name
    db_user = settings.db_user
    db_password = settings.db_password

    if not db_password:
        line(BAD, "DB_PASSWORD (and DATABASE_URL) are empty in .env.")
        print("         set a password in .env - creating a passwordless DB user is unsafe.")
        conn.close()
        return 1

    with conn.cursor() as cur:
        if args.reset:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            print(f"  [ OK ] dropped database `{db_name}` (--reset)")

        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        print(f"  [ OK ] database `{db_name}` ready (utf8mb4)")

        # The password below comes straight from .env, so the app and MySQL can
        # never disagree. CREATE ... IF NOT EXISTS can't change an existing
        # password, hence the ALTER in both cases.
        for host in ("localhost", "%"):
            cur.execute(f"CREATE USER IF NOT EXISTS '{db_user}'@'{host}' "
                        f"IDENTIFIED BY '{sql_quote(db_password)}'")
            cur.execute(f"ALTER USER '{db_user}'@'{host}' "
                        f"IDENTIFIED BY '{sql_quote(db_password)}'")
            cur.execute(f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'{host}'")
        cur.execute("FLUSH PRIVILEGES")
        print(f"  [ OK ] user '{db_user}'@'localhost' and @'%' set to the .env password")
        print(f"  [ OK ] ALL PRIVILEGES granted on `{db_name}`.*")

    conn.close()

    step("Creating tables and reporting views")
    try:
        from app.database import init_db, db_health
        init_db()
        print(f"  [ OK ] schema ready: {db_health()}")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"init_db failed: {exc}")
        return 1

    step("Seeding roles, staff, doctors, patients and slots")
    try:
        from app.seed import seed_all
        summary = seed_all()
        seeded = summary.get("seeded", {})
        print(f"  [ OK ] {', '.join(k for k, v in seeded.items() if v) or 'already seeded'}")
        if summary.get("existing"):
            print(f"         existing rows: {summary['existing']}")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"seeding failed: {exc}")
        return 1

    return 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up or diagnose the hospital platform database.",
        epilog="Run without arguments for a full setup, or --check to only diagnose.",
    )
    parser.add_argument("--check", action="store_true",
                        help="diagnose only, make no changes")
    parser.add_argument("--reset", action="store_true",
                        help="DROP the database first (deletes all data)")
    parser.add_argument("--root-user", default=os.getenv("MYSQL_ROOT_USER", "root"))
    parser.add_argument("--root-password", default=os.getenv("MYSQL_ROOT_PASSWORD", ""))
    parser.add_argument("--root-host", default=os.getenv("MYSQL_ROOT_HOST", "127.0.0.1"))
    parser.add_argument("--root-port", type=int, default=int(os.getenv("MYSQL_ROOT_PORT", "3306")))
    parser.add_argument("--socket", default=os.getenv("MYSQL_SOCKET", ""),
                        help="connect through a unix socket instead of TCP (Linux/macOS)")
    parser.add_argument("--db-name", default="", help="override the database name")
    args = parser.parse_args(argv)

    print("=" * 74)
    print("  VIJAY VARGIYA GROUP OF HOSPITALS - setup / health check")
    print("=" * 74)

    if args.check:
        ok = check_python()
        ok = check_packages()[0] and ok
        ok = check_env_file() and ok
        if ok:
            ok = check_settings() and ok
            ok = check_database() and ok

        print("\n" + "=" * 74)
        if ok:
            print("  Everything looks good.")
            print("  Start the app:   python run.py")
            print("  Then open:       http://127.0.0.1:8000/ui")
        else:
            print("  Problems found above. Fix them, then run:  python scripts/setup.py")
        print("=" * 74)
        return 0 if ok else 1

    # full setup
    if not check_python():
        return 1
    packages_ok, _ = check_packages()
    if not packages_ok:
        print("\n  Install the missing packages first:")
        print("    pip install -r requirements.txt")
        return 1
    if not check_env_file():
        return 1

    # Ask for the root password if one is needed and none was supplied.
    if not args.root_password and not args.socket:
        try:
            entered = getpass.getpass(
                f"\nMySQL '{args.root_user}' password (Enter if there is none): ")
            args.root_password = entered
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 130

    if run_setup(args) != 0:
        return 1

    print("\n" + "=" * 74)
    print("  SETUP COMPLETE")
    print("=" * 74)
    print("  1. Start the app:      python run.py")
    print("  2. Open the portals:   http://127.0.0.1:8000/ui")
    print("  3. API explorer:       http://127.0.0.1:8000/docs")
    print()
    print("  Demo logins")
    print("    SUPER_ADMIN   superadmin@vijayvargiiyahospital.in  / SuperAdmin@123")
    print("    ADMIN         admin@vijayvargiiyahospital.in       / Admin@123")
    print("    DOCTOR        dr.arjunmehra@vijayvargiiyahospital.in / Doctor@123")
    print("    RECEPTIONIST  reception@vijayvargiiyahospital.in   / Reception@123")
    print("    ACCOUNTANT    accounts@vijayvargiiyahospital.in    / Accounts@123")
    print("    PATIENT       ramesh.yadav@example.com             / Patient@123")
    print()
    print("  Verify the install (server must be running):")
    print("    python -m tests.smoke_test     # 89 end-to-end checks")
    print("    python -m tests.page_check     # every page x every role")
    print("    python -m tests.mysql_checks   # MySQL constraints, views, money")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
