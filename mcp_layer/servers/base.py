"""Shared plumbing for the 10 MCP server modules.

Each server module keeps two parallel registries:

    TOOLS   raw tool bodies:  fn(db, **kwargs) -> data | envelope
    (mcp)   FastMCP instance whose tools are thin typed wrappers that
            resolve the caller (JWT / system key / demo) and funnel every
            call through mcp_layer.invoke.invoke()

so the MCP wire protocol and the REST/chatbot bridge share ONE enforcement
path: RBAC tier check + hallucination-control guard + audit.
"""
from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable

from fastmcp import FastMCP

from .. import invoke as _invoke
from ..rbac import ANONYMOUS, Principal, principal_from_headers

log = logging.getLogger("vvh.mcp.servers")

ToolFn = Callable[..., Any]


# --------------------------------------------------------------------------
# Tool registration (raw bodies)
# --------------------------------------------------------------------------
def tool(name: str, description: str, *, write: bool = False, no_db: bool = False):
    """Declare a raw tool body inside a server module.

    Usage inside e.g. mcp_layer/servers/patients.py:

        TOOLS: dict = {}

        @tool("get_patient", "Look up one patient by id.")
        def _get_patient(db, patient_id: int):
            ...

    no_db=True marks meta/status tools (mcp_status, db_health) that must keep
    answering DURING a database outage - they are the diagnostics.
    """

    def decorator(fn: ToolFn) -> ToolFn:
        TOOLS = fn.__globals__.setdefault("TOOLS", {})
        TOOLS[name] = {"fn": fn, "description": description, "write": write, "no_db": no_db}
        return fn

    return decorator


# --------------------------------------------------------------------------
# Caller resolution for the MCP wire
# --------------------------------------------------------------------------
def current_principal() -> Principal:
    """Best-effort principal from the active HTTP request (FastMCP transport)."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        # include_all=True keeps the Authorization / API-key headers that the
        # default filter strips - RBAC depends on them.
        headers = dict(get_http_headers(include_all=True) or {})
    except Exception:  # noqa: BLE001 - stdio / in-process calls have no HTTP
        return ANONYMOUS
    return principal_from_headers(headers)


# --------------------------------------------------------------------------
# FastMCP factory
# --------------------------------------------------------------------------
def attach_tools(mcp: FastMCP, server_key: str, spec, tools: dict[str, dict]) -> None:
    """Register every raw tool body as a typed FastMCP wrapper tool."""

    def _make_wrapper(name: str, meta: dict, fn: ToolFn):
        # NOTE: factory keeps `name` bound per tool (no late-binding closure bug).
        params = [p for p in inspect.signature(fn).parameters.values() if p.name != "db"]
        signature = inspect.Signature(parameters=params)

        def wrapper(**kwargs: Any):
            principal = current_principal()
            return _invoke.invoke(principal, server_key, name, kwargs)

        wrapper.__signature__ = signature  # type: ignore[attr-defined]
        wrapper.__name__ = name
        wrapper.__doc__ = meta.get("description") or (fn.__doc__ or "")
        wrapper.__annotations__ = {
            p.name: (p.annotation if p.annotation is not inspect.Parameter.empty else Any)
            for p in params
        }
        return wrapper

    for tool_name in spec.tools:
        meta = tools[tool_name]
        fn: ToolFn = meta["fn"]
        wrapper = _make_wrapper(tool_name, meta, fn)
        mcp.tool(
            name=tool_name,
            description=meta.get("description") or (fn.__doc__ or ""),
            annotations={"readOnlyHint": not meta.get("write", False)},
        )(wrapper)


def make_server(spec, instructions: str) -> FastMCP:
    """One-liner used by every server module: build + attach + return FastMCP."""
    mcp = FastMCP(
        name=f"{spec.title} [{spec.tier}]",
        instructions=(
            f"{instructions}\n\n"
            f"Access tier: {spec.tier}. Calls are RBAC-checked against the "
            f"caller's JWT/system key, and every answer is database-grounded. "
            f"If the hospital database is unavailable the tools answer "
            f"'no' and generate nothing (hallucination control)."
        ),
    )
    import sys

    caller_module = sys._getframe(1).f_globals
    tools: dict[str, dict] = caller_module.get("TOOLS", {})
    attach_tools(mcp, spec.key, spec, tools)
    return mcp


def run_standalone(spec, mcp: FastMCP) -> None:
    """Serve one server on its own port:  python -m mcp_layer.servers.<key>"""
    from ..config import settings

    port = settings.server_ports[spec.key]
    print(f"[mcp] {spec.key} [{spec.tier}] -> http://{settings.gateway_host}:{port}/mcp")
    mcp.run(transport="http", host=settings.gateway_host, port=port, path="/mcp")
