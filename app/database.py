"""MySQL-only database engine and session management."""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger("vvh.database")


class Base(DeclarativeBase):
    pass


def _build_engine():
    """Create the mandatory MySQL engine. SQLite is deliberately unsupported."""
    if not settings.primary_url.startswith("mysql"):
        raise RuntimeError("MySQL is required. Set DATABASE_URL to a mysql+pymysql URL.")
    try:
        eng = create_engine(settings.primary_url, echo=False, future=True,
                            pool_pre_ping=True, pool_recycle=1800)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("Connected to MySQL database '%s' at %s:%s",
                 settings.db_name, settings.db_host, settings.db_port)
        return eng, settings.primary_url
    except Exception as exc:
        raise RuntimeError(
            f"MySQL connection failed for {settings.db_host}:{settings.db_port}/{settings.db_name}: {exc}"
        ) from exc


engine, ACTIVE_DATABASE_URL = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Reporting views used by analytics / BI / the reception TV display. They live in
# app/tools/export_schema.py (and sql/02_schema.sql), but init_db() must create
# them too: `python run.py` alone used to leave them missing, so every reporting
# query failed with "Table 'v_...' doesn't exist" (MySQL error 1146).
REPORTING_VIEWS: tuple[str, ...] = (
    """
    CREATE OR REPLACE VIEW v_appointments_today AS
    SELECT a.id, a.appointment_code, p.patient_code, p.full_name AS patient_name, p.phone,
           d.full_name AS doctor_name, s.name AS specialty, a.appointment_date, a.start_time,
           a.token_number, a.status, a.queue_status, a.source
    FROM appointments a
    JOIN patients p  ON p.id = a.patient_id
    LEFT JOIN doctors d ON d.id = a.doctor_id
    LEFT JOIN specialties s ON s.id = a.specialty_id
    WHERE a.appointment_date = CURDATE()
    """,
    """
    CREATE OR REPLACE VIEW v_outstanding_invoices AS
    SELECT i.id, i.invoice_number, p.patient_code, p.full_name AS patient_name, p.phone,
           i.total_amount, i.paid_amount, i.balance_amount, i.status, i.issued_at, i.due_date
    FROM invoices i JOIN patients p ON p.id = i.patient_id
    WHERE i.balance_amount > 0 AND i.status IN ('UNPAID','PARTIAL')
    """,
    """
    CREATE OR REPLACE VIEW v_doctor_daily_load AS
    SELECT d.id AS doctor_id, d.doctor_code, d.full_name AS doctor_name, s.name AS specialty,
           a.appointment_date,
           COUNT(*) AS appointments,
           SUM(a.status = 'COMPLETED') AS completed,
           SUM(a.status = 'CANCELLED') AS cancelled,
           SUM(a.queue_status = 'NO_SHOW') AS no_shows,
           SUM(CASE WHEN a.status IN ('PENDING','CONFIRMED') THEN 1 ELSE 0 END) AS upcoming
    FROM appointments a
    JOIN doctors d ON d.id = a.doctor_id
    LEFT JOIN specialties s ON s.id = d.specialty_id
    GROUP BY d.id, d.doctor_code, d.full_name, s.name, a.appointment_date
    """,
    """
    CREATE OR REPLACE VIEW v_ai_daily_summary AS
    SELECT DATE(c.started_at) AS day, c.status, COUNT(*) AS conversations,
           SUM(c.escalation_count) AS escalations,
           ROUND(AVG(c.message_count), 1) AS avg_messages,
           SUM(c.successful_bookings) AS bookings,
           ROUND(AVG(c.avg_response_ms), 0) AS avg_response_ms
    FROM ai_conversations c
    GROUP BY DATE(c.started_at), c.status
    """,
)


def init_db(drop: bool = False) -> None:
    from . import models  # noqa: F401  (register mappers)

    if drop:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Views are created after the tables they read. Failures are logged, never
    # fatal: a restricted DB user may not hold CREATE VIEW, and the app itself
    # does not depend on them.
    for view_sql in REPORTING_VIEWS:
        try:
            with engine.begin() as conn:
                conn.execute(text(view_sql))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not create reporting view: %s", exc)


def db_health() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "status": "up",
            "backend": "mysql",
            "database": settings.db_name,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "error": str(exc)}
