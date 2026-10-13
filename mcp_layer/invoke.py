"""Single entry point for every tool call on the platform.

Both the MCP wire protocol (FastMCP tools) and the REST/chatbot bridge go
through invoke() so RBAC and hallucination control cannot be bypassed:

    principal -> RBAC tier check -> guard.run_guarded(tool_fn) -> envelope

Every call is also written to the audit trail (best effort - a DB outage
cannot be allowed to break the refusal path).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import guard, registry
from .rbac import (
    Principal,
    RbacDenied,
    check as rbac_check,
    reset_current_principal,
    set_current_principal,
)

log = logging.getLogger("vvh.mcp.invoke")


def _audit(principal: Principal, tool_fqn: str, args: dict, result: dict, duration_ms: float) -> None:
    """Best-effort audit row - never raises, never blocks the answer."""
    try:
        if not result.get("grounded"):
            # During outages there is no DB to write to; log locally only.
            log.info("audit(skip-db) %s %s -> %s", principal.subject, tool_fqn, result.get("answer") or result.get("reason"))
            return
        with guard.session_scope() as db:
            from app import models

            row = models.AIToolCall(
                conversation_id=None,
                tool_name=f"mcp:{tool_fqn}",
                arguments=dict(args or {}),
                status="SUCCESS" if result.get("ok") else "ERROR",
                result_summary=str(result.get("note") or result.get("answer") or "ok")[:255],
                duration_ms=int(duration_ms),
            )
            db.add(row)
    except Exception:  # noqa: BLE001 - audit must never break tools
        log.debug("audit write skipped", exc_info=True)


def invoke(
    principal: Principal,
    server_key: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one tool under RBAC + hallucination control.  Always returns an envelope."""
    started = time.time()
    args = dict(arguments or {})
    fqn = f"{server_key}.{tool_name}"

    # ---- 1. registry lookup ------------------------------------------
    spec = registry.SERVERS_BY_KEY.get(server_key)
    if spec is None:
        result = guard.refuse("unknown_server", tool=fqn, detail=f"no MCP server named {server_key!r}")
        return result

    if tool_name not in spec.tools:
        result = guard.refuse("unknown_tool", server=server_key, tool=tool_name,
                              detail=f"{server_key} exposes: {', '.join(spec.tools)}")
        return result

    # ---- 2. RBAC ------------------------------------------------------
    try:
        rbac_check(principal, spec.tier)
    except RbacDenied as exc:
        result = guard.refuse(
            "rbac_denied",
            server=server_key,
            tool=tool_name,
            detail=f"{exc}. Required tier {spec.tier}; caller tier {principal.tier}.",
        )
        result["policy"] = "RBAC: caller tier below server tier -> access denied"
        _audit(principal, fqn, args, result, (time.time() - started) * 1000)
        return result

    # ---- 3. guarded execution ----------------------------------------
    mod = registry.load_server_module(server_key)
    meta = mod.TOOLS[tool_name]
    token = set_current_principal(principal)
    try:
        result = guard.run_guarded(
            meta["fn"],
            server=server_key,
            tool=tool_name,
            needs_db=not meta.get("no_db", False),
            **args,
        )
    finally:
        reset_current_principal(token)

    # ---- 4. audit + tag ----------------------------------------------
    duration_ms = (time.time() - started) * 1000
    result.setdefault("server", server_key)
    result.setdefault("tool", tool_name)
    result["tier"] = spec.tier
    result["caller"] = principal.describe()
    result["duration_ms"] = round(duration_ms, 1)
    _audit(principal, fqn, args, result, duration_ms)
    return result


def list_tools_for(principal: Principal) -> dict[str, Any]:
    """Tools the principal may actually call (RBAC-filtered)."""
    allowed, denied = [], []
    for spec in registry.SERVERS:
        entry = spec.to_dict()
        if principal.can_call(spec.tier):
            allowed.append(entry)
        else:
            denied.append(entry)
    return {
        "caller": principal.describe(),
        "allowed": allowed,
        "denied": denied,
        "total_tools": registry.tools_total(),
        "allowed_tools": sum(len(s["tools"]) for s in allowed),
    }
