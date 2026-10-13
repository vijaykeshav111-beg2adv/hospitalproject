"""MCP Gateway - one FastAPI app exposing all 10 FastMCP servers.

Two deployment modes:

    # standalone:  python -m mcp_layer.gateway        -> serves at /mcp/...
    gateway = create_gateway(prefix="/mcp")

    # mounted inside another app at /mcp (app/main.py, backup bundle):
    app.mount("/mcp", create_gateway(prefix=""))

Surface (paths shown for prefix="/mcp")
-------
    GET  /mcp/                     index page - links to all 10 MCP servers
    *    /mcp/servers/<key>/mcp    Streamable-HTTP MCP endpoint (FastMCP)
    GET  /mcp/api/status           DB guard + RBAC matrix + server list
    GET  /mcp/api/servers          registry (RBAC-filtered per caller)
    GET  /mcp/api/tools            tools the caller may use
    POST /mcp/api/tools/call       generic tool call {server, tool, arguments}
    POST /mcp/api/chat             grounded chatbot {message}

Auth: Authorization: Bearer <hospital JWT>   (any role; tier = f(role))
  or: X-MCP-API-Key: <MCP_SYSTEM_API_KEY>    (SYSTEM tier)
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import chatbot, guard, registry
from .config import settings
from .invoke import invoke, list_tools_for
from .rbac import principal_from_headers


def _index_html(prefix: str) -> str:
    rows = []
    for s in registry.SERVERS:
        tools = ", ".join(f"<code>{t}</code>" for t in s.tools)
        rows.append(f"""
        <tr>
          <td><b>{s.icons} {s.title}</b><br><small>{s.summary}</small></td>
          <td><span class="tier {s.tier.lower()}">{s.tier}</span></td>
          <td>{len(s.tools)}<br><small>{tools}</small></td>
          <td><a href="{prefix}/servers/{s.key}/mcp"><code>{prefix}/servers/{s.key}/mcp</code></a></td>
        </tr>""")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>VVH MCP Gateway</title>
<style>
  body{{font-family:"Plus Jakarta Sans",system-ui,sans-serif;background:#f3f6fd;color:#0f1b3d;margin:0;padding:32px}}
  .wrap{{max-width:1080px;margin:0 auto}}
  h1{{letter-spacing:-.02em}} .grid{{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin:18px 0}}
  .card{{background:#fff;border:1px solid #e5eaf5;border-radius:16px;padding:18px;box-shadow:0 10px 30px rgba(47,91,234,.07)}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:16px;overflow:hidden}}
  td,th{{padding:12px 14px;border-bottom:1px solid #e5eaf5;text-align:left;vertical-align:top;font-size:14px}}
  .tier{{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.6px}}
  .tier.user{{background:#eaf0ff;color:#1f43c4}} .tier.admin{{background:#f0ecff;color:#6d43e8}}
  .tier.system{{background:#e2f8f5;color:#0b8f83}}
  code{{background:#eaf0ff;color:#1f43c4;padding:2px 6px;border-radius:6px;font-size:12px}}
</style></head><body><div class="wrap">
  <h1>Vijay Vargiya Hospitals &mdash; MCP Gateway</h1>
  <p>10 MCP servers &rarr; 5 USER + 3 ADMIN + 2 SYSTEM &rarr; RBAC &nbsp;|&nbsp;
     Hallucination control: <b>db not available &rarr; not generate &rarr; no</b></p>
  <div class="grid">
    <div class="card"><b>Status JSON</b><br><a href="{prefix}/api/status">{prefix}/api/status</a></div>
    <div class="card"><b>Chatbot API</b><br><code>POST {prefix}/api/chat</code></div>
    <div class="card"><b>Standalone ports</b><br>{settings.first_server_port}..{settings.first_server_port + 9}</div>
    <div class="card"><b>Tools</b><br>{registry.tools_total()} across 10 servers</div>
  </div>
  <table>
    <tr><th>Server</th><th>Tier</th><th>Tools</th><th>MCP endpoint</th></tr>
    {''.join(rows)}
  </table>
</div></body></html>"""


def create_gateway(prefix: str = "/mcp") -> FastAPI:
    """Build the gateway FastAPI app.

    prefix=""  -> routes live at /api/..., /servers/...  (mount this app at /mcp)
    prefix="/mcp" -> routes live at /mcp/api/...         (run standalone)
    """
    prefix = prefix.rstrip("/")

    # Each FastMCP http_app() owns a Streamable-HTTP session manager that must
    # be started by the PARENT app's lifespan (Starlette does not run mounted
    # apps' lifespans automatically).  We chain all 10 here.
    from contextlib import AsyncExitStack, asynccontextmanager

    sub_apps: dict[str, object] = {}
    for spec in registry.SERVERS:
        mod = registry.load_server_module(spec.key)
        sub_apps[spec.key] = mod.mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def combined_lifespan(parent):
        async with AsyncExitStack() as stack:
            for sub in sub_apps.values():
                sub_lifespan = getattr(sub, "lifespan", None)
                if sub_lifespan is not None:
                    await stack.enter_async_context(sub_lifespan(sub))
            yield

    app = FastAPI(
        title="VVH MCP Gateway",
        description="10 hospital MCP servers with RBAC + hallucination control",
        version="1.0.0",
        docs_url=f"{prefix}/docs" if prefix else "/docs",
        openapi_url=f"{prefix}/openapi.json" if prefix else "/openapi.json",
        lifespan=combined_lifespan,
    )

    # ---- 10 FastMCP servers ------------------------------------------
    for spec in registry.SERVERS:
        app.mount(f"{prefix}/servers/{spec.key}", sub_apps[spec.key])

    # ---- index --------------------------------------------------------
    @app.get(f"{prefix}", response_class=HTMLResponse, include_in_schema=False)
    @app.get(f"{prefix}/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return HTMLResponse(_index_html(prefix))

    # ---- API ----------------------------------------------------------
    @app.get(f"{prefix}/api/status")
    def api_status(request: Request):
        p = principal_from_headers(dict(request.headers))
        return {
            "gateway": "vvh-mcp",
            "time": time.time(),
            "db": guard.db_status(),
            "hallucination_control": {
                "rule": "db not available -> not generate -> no",
                "refuse_when_db_down": settings.refuse_when_db_down,
            },
            "rbac": registry.rbac_matrix(),
            "caller": p.describe(),
            "tools_total": registry.tools_total(),
            "endpoints": {s.key: f"{prefix}/servers/{s.key}/mcp" for s in registry.SERVERS},
            "standalone_ports": settings.server_ports,
        }

    @app.get(f"{prefix}/api/servers")
    def api_servers(request: Request):
        return list_tools_for(principal_from_headers(dict(request.headers)))

    @app.get(f"{prefix}/api/tools")
    def api_tools():
        return registry.tools_index()

    @app.post(f"{prefix}/api/tools/call")
    async def api_tool_call(request: Request):
        p = principal_from_headers(dict(request.headers))
        body = await request.json()
        server = str(body.get("server") or "")
        tool = str(body.get("tool") or "")
        args = body.get("arguments") or {}
        if not isinstance(args, dict):
            return JSONResponse({"ok": False, "detail": "arguments must be an object"}, status_code=400)
        return invoke(p, server, tool, args)

    @app.post(f"{prefix}/api/chat")
    async def api_chat(request: Request):
        p = principal_from_headers(dict(request.headers))
        body = await request.json()
        message = str(body.get("message") or "")
        started = time.time()
        result = chatbot.respond(p, message)
        result["latency_ms"] = round((time.time() - started) * 1000, 1)
        result["model"] = settings.chatbot_model
        result["caller"] = p.describe()
        return result

    return app


# Standalone module-level app (python -m mcp_layer.gateway).
gateway = create_gateway(prefix="/mcp")


def main() -> None:
    import uvicorn

    print(f"[mcp-gateway] http://{settings.gateway_host}:{settings.gateway_port}/mcp")
    print("[mcp-gateway] 10 servers mounted under /mcp/servers/<key>/mcp")
    for key, port in settings.server_ports.items():
        print(f"               {key:14s} standalone -> :{port}/mcp")
    uvicorn.run(gateway, host=settings.gateway_host, port=settings.gateway_port, log_level="info")


if __name__ == "__main__":
    main()
