"""MCP server 10 - System & Ops Server  [SYSTEM tier].

Health probes, DB status, background job history, system settings and the
MCP layer's own status (this is where the hallucination-control policy is
reported live).
"""
from __future__ import annotations

from sqlalchemy import select

from app import models

from .. import guard, registry
from ..config import settings as mcp_settings
from ..guard import grounded_no
from ..registry import SERVERS_BY_KEY
from .base import make_server, run_standalone, tool

SPEC = SERVERS_BY_KEY["ops"]
TOOLS: dict = {}


@tool("system_health", "Application health snapshot (process, env, storage).", no_db=True)
def _system_health(db):
    import sys
    from pathlib import Path

    from app.config import settings

    storage = Path(settings.storage_dir)
    return {
        "app_name": settings.app_name,
        "env": settings.app_env,
        "python": sys.version.split()[0],
        "storage_dir": str(storage),
        "storage_exists": storage.exists(),
        "scheduler_enabled": bool(getattr(settings, "enable_scheduler", False)),
    }


@tool("db_health", "Live database probe - the same check the hallucination guard uses.", no_db=True)
def _db_health(db):
    status = guard.db_status(force=True)
    return {
        "status": "up" if status["ok"] else "down",
        "detail": status["detail"],
        "policy": "db unavailable -> tools return 'no' and the chatbot does not generate",
        "probe_ttl_seconds": mcp_settings.db_probe_ttl_seconds,
    }


@tool("job_runs", "Recent background job runs (job name, status, duration).")
def _job_runs(db, limit: int = 20):
    limit = max(1, min(int(limit), 100))
    rows = db.scalars(
        select(models.JobRun).order_by(models.JobRun.id.desc()).limit(limit)
    ).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="job_runs", what="job run")
    return [{"id": r.id, "job_name": r.job_name, "status": r.status,
             "started_at": str(r.started_at), "duration_ms": r.duration_ms,
             "error": r.error} for r in rows]


@tool("list_settings", "System settings store (key / value / description).")
def _list_settings(db, key: str = ""):
    stmt = select(models.SystemSetting)
    if (key or "").strip():
        stmt = stmt.where(models.SystemSetting.key.ilike(f"%{key.strip()}%"))
    rows = db.scalars(stmt.order_by(models.SystemSetting.key)).all()
    if not rows:
        return grounded_no(server=SPEC.key, tool="list_settings", what="setting")
    return [{"key": s.key, "value": s.value, "description": s.description} for s in rows]


@tool("mcp_status", "MCP layer status: the 10 servers, tiers, tools and DB guard state.", no_db=True)
def _mcp_status(db):
    health = guard.db_status()
    return {
        "servers": [s.to_dict() for s in registry.SERVERS],
        "rbac": registry.rbac_matrix(),
        "tools_total": registry.tools_total(),
        "db": health,
        "hallucination_control": {
            "refuse_when_db_down": mcp_settings.refuse_when_db_down,
            "rule": "db not available -> not generate -> no",
        },
        "standalone_ports": mcp_settings.server_ports,
        "gateway_port": mcp_settings.gateway_port,
    }


mcp = make_server(
    SPEC,
    "System & Ops Server (SYSTEM tier). Operational truth about the "
    "platform and the MCP layer itself.",
)

if __name__ == "__main__":
    run_standalone(SPEC, mcp)
