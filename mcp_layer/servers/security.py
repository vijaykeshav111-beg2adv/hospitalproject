"""MCP server 09 - Security & RBAC Server  [SYSTEM tier].

Users, role/permission matrix, live sessions, login activity and the audit
trail.  Only SYSTEM-tier principals (SUPER_ADMIN JWT or the MCP system API
key) may reach this server.
"""
from __future__ import annotations

from sqlalchemy import select

from app import models

from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["security"]
TOOLS: dict = {}


def _user_brief(u: models.User) -> dict:
    return {
        "id": u.id,
        "uuid": u.uuid,
        "full_name": u.full_name,
        "email": u.email,
        "role": u.role_name,
        "is_active": bool(u.is_active),
        "is_verified": bool(u.is_verified),
        "is_locked": bool(u.is_locked),
        "last_login_at": str(u.last_login_at) if u.last_login_at else None,
    }


@tool("list_users", "List platform users (filter by role).")
def _list_users(db, role: str = "", limit: int = 25):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.User)
    if (role or "").strip():
        stmt = stmt.join(models.Role).where(models.Role.name == role.strip().upper())
    rows = db.scalars(stmt.order_by(models.User.id).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_users", what="user")
    return [_user_brief(u) for u in rows]


@tool("get_user", "Look up one user by id or email.")
def _get_user(db, user_id: int | None = None, email: str = ""):
    stmt = select(models.User)
    if user_id:
        stmt = stmt.where(models.User.id == user_id)
    elif email.strip():
        stmt = stmt.where(models.User.email == email.strip().lower())
    else:
        return grounded_no(server=SPEC.key, tool="get_user", what="user",
                           note="Provide user_id or email.")
    u = db.scalar(stmt)
    if u is None:
        return grounded_no(server=SPEC.key, tool="get_user", what="user")
    return _user_brief(u)


@tool("role_matrix", "RBAC matrix: every hospital role with its permission codes, plus the MCP 5/3/2 tier split.")
def _role_matrix(db):
    from app import rbac as app_rbac

    catalogue = {
        role_name: sorted(app_rbac.permissions_for_role(role_name))
        for role_name in app_rbac.ROLES
    }
    db_roles = [r.name for r in db.scalars(select(models.Role).order_by(models.Role.name)).all()]
    return {
        "catalogue": catalogue,
        "roles_in_db": db_roles,
        "mcp_tiers": {
            "USER": ["patients", "doctors", "appointments", "prescriptions", "billing"],
            "ADMIN": ["notifications", "reviews", "analytics"],
            "SYSTEM": ["security", "ops"],
        },
    }


@tool("list_sessions", "Live (non-revoked) user sessions.")
def _list_sessions(db, limit: int = 25):
    limit = max(1, min(int(limit), 100))
    rows = db.scalars(
        select(models.UserSession).where(models.UserSession.is_revoked.is_(False))
        .order_by(models.UserSession.last_seen_at.desc()).limit(limit)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_sessions", what="session")
    return [{"id": s.id, "user_id": s.user_id, "device": s.device,
             "ip_address": s.ip_address, "is_revoked": bool(s.is_revoked),
             "last_seen_at": str(s.last_seen_at), "expires_at": str(s.expires_at)} for s in rows]


@tool("audit_log", "Recent audit-log entries (who changed what).")
def _audit_log(db, action: str = "", limit: int = 25):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.AuditLog)
    if (action or "").strip():
        stmt = stmt.where(models.AuditLog.action.ilike(f"%{action.strip()}%"))
    rows = db.scalars(stmt.order_by(models.AuditLog.id.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="audit_log", what="audit entry")
    return [{"id": r.id, "user_email": r.user_email, "user_role": r.user_role,
             "action": r.action, "resource": r.resource, "resource_id": r.resource_id,
             "ip_address": r.ip_address, "created_at": str(r.created_at)} for r in rows]


@tool("login_activity", "Recent login attempts (success / failed / locked).")
def _login_activity(db, status: str = "", limit: int = 25):
    limit = max(1, min(int(limit), 100))
    stmt = select(models.LoginActivity)
    if (status or "").strip():
        stmt = stmt.where(models.LoginActivity.status == status.strip().upper())
    rows = db.scalars(stmt.order_by(models.LoginActivity.id.desc()).limit(limit)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="login_activity", what="login attempt")
    return [{"id": r.id, "email": r.email_attempted, "status": r.status,
             "reason": r.reason, "ip_address": r.ip_address,
             "attempt_at": str(r.attempt_at)} for r in rows]


mcp = make_server(
    SPEC,
    "Security & RBAC Server (SYSTEM tier). Exposes identity and audit data "
    "to SUPER_ADMIN / service callers only; every call is itself audited.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
