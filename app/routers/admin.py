"""Admin utilities: background jobs, settings, DB health, audit and system info."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..database import db_health, get_db
from ..deps import CurrentUser, audit, require_permission
from ..rbac import PERMISSIONS, ROLE_PERMISSIONS
from ..services import jobs

router = APIRouter(prefix="/admin", tags=["Admin & Jobs"])


@router.get("/health", tags=["System"], summary="Service + database health")
def health():
    return {"service": "ok", "database": db_health()}


@router.get("/system", summary="System snapshot")
def system(db: Session = Depends(get_db), _: CurrentUser = Depends(require_permission("settings:manage"))):
    counts = {}
    for model in (models.User, models.Patient, models.Doctor, models.Specialty, models.Slot,
                  models.Appointment, models.Consultation, models.Prescription, models.Invoice,
                  models.Payment, models.MedicalFile, models.AIConversation, models.AIMessage,
                  models.AIToolCall, models.WhatsappMessage, models.Notification, models.AuditLog):
        counts[model.__tablename__] = db.scalar(select(func.count()).select_from(model)) or 0
    return {
        "database_backend": "mysql",
        "roles": list(ROLE_PERMISSIONS.keys()),
        "permissions": len(PERMISSIONS),
        "tables": counts,
        "scheduler_jobs": list(jobs.JOBS.keys()),
    }


@router.get("/security/summary",
            summary="Security overview for the super-admin dashboard")
def security_summary(db: Session = Depends(get_db),
                     _: CurrentUser = Depends(require_permission("settings:manage"))):
    """Login, lockout, session and audit counters.

    The super-admin dashboard calls this endpoint; it previously did not exist,
    so the "Security overview" card stayed empty.
    """
    from datetime import timedelta

    from ..security import utcnow

    now = utcnow()
    since_24h = now - timedelta(hours=24)

    def count(model, *conditions) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in conditions:
            stmt = stmt.where(condition)
        return db.scalar(stmt) or 0

    return {
        "generated_at": now.isoformat(),
        "users": {
            "total": count(models.User),
            "active": count(models.User, models.User.is_active.is_(True)),
            "locked": count(models.User, models.User.locked_until.is_not(None),
                            models.User.locked_until > now),
            "must_change_password": count(models.User, models.User.must_change_password.is_(True)),
        },
        "logins": {
            "last_24h": count(models.LoginActivity, models.LoginActivity.attempt_at >= since_24h),
            "success": count(models.LoginActivity, models.LoginActivity.status == "SUCCESS"),
            "failed": count(models.LoginActivity, models.LoginActivity.status == "FAILED"),
            "locked": count(models.LoginActivity, models.LoginActivity.status == "LOCKED"),
            "failed_last_24h": count(models.LoginActivity, models.LoginActivity.status == "FAILED",
                                     models.LoginActivity.attempt_at >= since_24h),
        },
        "sessions": {
            "active": count(models.UserSession, models.UserSession.is_revoked.is_(False),
                            models.UserSession.expires_at > now),
            "revoked": count(models.UserSession, models.UserSession.is_revoked.is_(True)),
        },
        "audit": {
            "entries": count(models.AuditLog),
            "last_24h": count(models.AuditLog, models.AuditLog.created_at >= since_24h),
        },
        "recent_failed_logins": [
            {"email": row.email_attempted, "reason": row.reason,
             "ip": row.ip_address,
             "at": row.attempt_at.isoformat() if row.attempt_at else None}
            for row in db.scalars(
                select(models.LoginActivity)
                .where(models.LoginActivity.status.in_(["FAILED", "LOCKED"]))
                .order_by(models.LoginActivity.attempt_at.desc())
                .limit(5)
            ).all()
        ],
    }


@router.get("/jobs", summary="Background job catalogue")
def job_list(_: CurrentUser = Depends(require_permission("settings:manage"))):
    return [{"name": name,
             "schedule": (f"cron {kw}" if kind == "cron" else f"every {kw}"),
             "kind": kind}
            for name, (_, kind, kw) in jobs.JOBS.items()]


@router.post("/jobs/{job_name}/run", summary="Run a background job now")
def run_job(job_name: str, request: Request, db: Session = Depends(get_db),
            user: CurrentUser = Depends(require_permission("settings:manage"))):
    try:
        result = jobs.run_job_by_name(job_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit(db, action="JOB_RUN", resource="job", resource_id=job_name, user=user, request=request,
          new_value=result)
    return result


@router.get("/jobs/runs", summary="Job run history")
def job_runs(db: Session = Depends(get_db), job_name: str | None = None, limit: int = 100,
             _: CurrentUser = Depends(require_permission("settings:manage"))):
    stmt = select(models.JobRun)
    if job_name:
        stmt = stmt.where(models.JobRun.job_name == job_name)
    rows = db.scalars(stmt.order_by(models.JobRun.started_at.desc()).limit(limit)).all()
    return [{
        "id": r.id, "job": r.job_name, "status": r.status,
        "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duration_ms": r.duration_ms, "details": r.details, "error": r.error,
    } for r in rows]


@router.get("/settings", summary="List system settings")
def list_settings(db: Session = Depends(get_db),
                  _: CurrentUser = Depends(require_permission("settings:manage"))):
    rows = db.scalars(select(models.SystemSetting)).all()
    if not rows:
        return _default_settings()
    return [{"key": r.key, "value": r.value, "description": r.description} for r in rows]


@router.put("/settings/{key}", summary="Upsert a system setting")
def set_setting(key: str, value: str, request: Request, description: str | None = None,
                db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("settings:manage"))):
    row = db.scalar(select(models.SystemSetting).where(models.SystemSetting.key == key))
    before = {"value": row.value} if row else None
    if row:
        row.value = value
        if description:
            row.description = description
    else:
        row = models.SystemSetting(key=key, value=value, description=description)
        db.add(row)
    db.commit()
    audit(db, action="SETTING_UPDATE", resource="system_setting", resource_id=key, user=user,
          request=request, previous_value=before, new_value={"value": value})
    return {"key": key, "value": value}


def _default_settings() -> list[dict]:
    from ..config import settings

    return [
        {"key": "app_name", "value": settings.app_name, "description": "Hospital brand shown on documents"},
        {"key": "slot_hold_minutes", "value": str(settings.slot_hold_minutes),
         "description": "How long a slot stays held during booking"},
        {"key": "whatsapp_enabled", "value": str(settings.whatsapp_enabled),
         "description": "Twilio WhatsApp delivery toggle"},
        {"key": "email_enabled", "value": str(settings.email_enabled), "description": "SMTP delivery toggle"},
        {"key": "ai_provider", "value": settings.ai_provider,
         "description": "groq only"},
        {"key": "max_login_attempts", "value": str(settings.max_login_attempts),
         "description": "Lockout threshold"},
        {"key": "enable_scheduler", "value": str(settings.enable_scheduler),
         "description": "Background worker toggle"},
    ]
