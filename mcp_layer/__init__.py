"""MCP layer for the Vijay Vargiya Group of Hospitals platform.

Ten FastMCP servers (5 USER + 3 ADMIN + 2 SYSTEM) expose the hospital MySQL
domain as Model Context Protocol tools, with two cross-cutting guarantees:

1. RBAC ............ every server declares an access level (USER / ADMIN /
                     SYSTEM). JWT principals from the hospital app (or the
                     system API key) may only call servers at or below their
                     level.  See mcp_layer.rbac.

2. Hallucination
   control ......... every tool answers ONLY from live database rows.  If the
                     database is not available the tool returns the grounded
                     refusal envelope {"answer": "no", "generated": false}
                     and the chatbot refuses to generate any text.  Nothing is
                     ever invented.  See mcp_layer.guard.

The ten servers (see mcp_layer.registry):

    USER    (5)  patients | doctors | appointments | prescriptions | billing
    ADMIN   (3)  notifications | reviews | analytics
    SYSTEM  (2)  security | ops

Quick start
-----------
    python -m mcp_layer.gateway            # all 10 servers on one port (:8100)
    python -m mcp_layer.servers.patients   # one server standalone  (:8101)
    python backup/chatbot_mcp/run.py       # backup chatbot bundle   (:8200)
"""

from .registry import SERVERS, SERVERS_BY_KEY, tools_total  # noqa: F401

__all__ = ["SERVERS", "SERVERS_BY_KEY", "tools_total"]
