"""Hallucination control + database access for every MCP tool.

Policy (enforced here, not in prose)
------------------------------------
* A tool may only speak from rows it actually read out of MySQL.
* If the database is NOT available -> tools do NOT generate anything.  They
  return the grounded refusal envelope with "answer": "no" and
  "generated": false.  The chatbot turns that into an explicit refusal.
* A query that legitimately matches nothing is a grounded negative: it also
  answers "no", but with grounded=true so the client knows the DB confirmed it.
* Any unexpected exception is treated like a DB failure: refuse, never guess.

Every envelope carries:
    ok ......... call completed without infrastructure failure
    grounded ... the payload comes from live DB rows (or a confirmed DB "no")
    generated .. the caller is allowed to verbalise this into user text
    answer ..... "no" must be surfaced literally when nothing may be said
    source ..... which MCP server/tool produced the payload (audit trail)
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

log = logging.getLogger("vvh.mcp.guard")


class DatabaseUnavailable(RuntimeError):
    """Raised when the hospital MySQL database cannot be queried."""


# --------------------------------------------------------------------------
# DB probing
# --------------------------------------------------------------------------
_probe_cache: dict[str, Any] = {"at": 0.0, "ok": False, "detail": "not probed yet"}


def _probe_once() -> dict[str, Any]:
    """One live connectivity probe (SELECT 1)."""
    try:
        from app.database import engine  # heavy import - deferred on purpose

        with engine.connect() as conn:
            from sqlalchemy import text

            conn.execute(text("SELECT 1"))
        return {"ok": True, "detail": "mysql up"}
    except Exception as exc:  # noqa: BLE001 - any failure means "no data source"
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def db_status(force: bool = False) -> dict[str, Any]:
    """Cached DB health used by tools, dashboard and chatbot."""
    now = time.time()
    if force or (now - float(_probe_cache["at"])) > settings.db_probe_ttl_seconds:
        result = _probe_once()
        _probe_cache.update(at=now, ok=result["ok"], detail=result["detail"])
    return {"ok": bool(_probe_cache["ok"]), "detail": str(_probe_cache["detail"]), "checked_at": _probe_cache["at"]}


def reset_probe_cache() -> None:
    """Test hook: forget cached health so the next check is live."""
    _probe_cache.update(at=0.0, ok=False, detail="reset")


@contextmanager
def session_scope() -> Iterator[Any]:
    """Yield a SQLAlchemy session or raise DatabaseUnavailable.

    Any failure - import error, connection error, timeout - collapses into
    DatabaseUnavailable so callers uniformly take the "do not generate" path.
    """
    try:
        from app.database import SessionLocal  # deferred import
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(f"database module unavailable: {exc}") from exc

    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(f"cannot open session: {exc}") from exc

    try:
        # Cheap liveness check: a pooled-but-dead connection must not feed
        # half-answers to the model.
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
        yield db
        db.commit()
    except DatabaseUnavailable:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise DatabaseUnavailable(f"query failed: {exc}") from exc
    finally:
        db.close()


# --------------------------------------------------------------------------
# Response envelopes
# --------------------------------------------------------------------------
def grounded(data: Any, *, server: str, tool: str, note: str = "", meta: dict | None = None) -> dict:
    """A normal, database-backed answer the caller may verbalise."""
    payload = {
        "ok": True,
        "grounded": True,
        "generated": True,
        "source": "mysql",
        "server": server,
        "tool": tool,
        "data": data,
    }
    if note:
        payload["note"] = note
    if meta:
        payload["meta"] = meta
    return payload


def grounded_no(*, server: str, tool: str, what: str = "record", note: str = "") -> dict:
    """The DB was reachable and confirmed there is nothing -> answer 'no'.

    This is still grounded information (a real negative), unlike a refusal.
    """
    return {
        "ok": True,
        "grounded": True,
        "generated": True,
        "answer": "no",
        "found": False,
        "source": "mysql",
        "server": server,
        "tool": tool,
        "data": None,
        "note": note or f"No matching {what} in the database.",
    }


def refuse(reason: str, *, server: str = "", tool: str = "", detail: str = "") -> dict:
    """Hallucination-control refusal: nothing may be generated.

    The literal answer is always "no" - the chatbot mirrors this and adds an
    explanation, but it must NEVER invent data on top of this envelope.

    `generated` is True only for refusals whose *text* is safe and factual to
    show ("access denied", "unknown tool").  DB failures and tool crashes keep
    generated=False - the platform shows the policy block, not a generated
    answer.
    """
    return {
        "ok": False,
        "grounded": False,
        "generated": reason not in {"db_unavailable", "tool_error"},
        "answer": "no",
        "reason": reason,
        "source": None,
        "server": server,
        "tool": tool,
        "data": None,
        "detail": detail,
        "policy": "hallucination-control: db unavailable -> not generate -> no",
    }


def refuse_db_down(*, server: str, tool: str, detail: str = "") -> dict:
    return refuse("db_unavailable", server=server, tool=tool, detail=detail)


def run_guarded(fn, *, server: str, tool: str, needs_db: bool = True, **kwargs) -> dict:
    """Execute fn(db, **kwargs) under the full guard policy.

    * DB down / any exception        -> refuse(...)  (generated=False)
    * fn returns GROUNDING sentinel  -> already an envelope, pass through
    * fn returns None / empty        -> grounded_no(...)
    * fn returns data                -> grounded(...)

    needs_db=False marks diagnostics (mcp_status, db_health) that must keep
    working during an outage - they receive db=None and never open a session.
    """
    if needs_db and settings.refuse_when_db_down and not db_status()["ok"]:
        # Probe first so a dead DB never even reaches the tool body.
        # (The session_scope check below is the second line of defence.)
        status = db_status()
        return refuse_db_down(server=server, tool=tool, detail=str(status.get("detail", "")))

    try:
        if needs_db:
            with session_scope() as db:
                result = fn(db, **kwargs)
        else:
            result = fn(None, **kwargs)
    except DatabaseUnavailable as exc:
        reset_probe_cache()
        return refuse_db_down(server=server, tool=tool, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - unexpected errors must not hallucinate
        log.exception("tool %s.%s crashed - refusing instead of guessing", server, tool)
        return refuse("tool_error", server=server, tool=tool, detail=f"{type(exc).__name__}: {exc}")

    if isinstance(result, dict) and ("grounded" in result or "answer" in result):
        # Tool already produced a policy envelope (e.g. grounded_no).
        result.setdefault("server", server)
        result.setdefault("tool", tool)
        return result

    if result is None or result == [] or result == {}:
        return grounded_no(server=server, tool=tool)

    env = grounded(result, server=server, tool=tool)
    if not needs_db:
        # Diagnostics report process/registry state, not DB rows.
        env["source"] = "meta"
        env["note"] = "diagnostic tool - not database sourced"
    return env
