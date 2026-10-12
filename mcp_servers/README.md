# Hospital MCP server

See [`../docs/MCP_STEP_BY_STEP.md`](../docs/MCP_STEP_BY_STEP.md) for setup, explanation, testing, extension patterns, and security guidance.

Run from the project root: `python -m mcp_servers.hospital_mcp_server`
Local endpoint: `http://127.0.0.1:8010/mcp`

This server is read-only and does not expose patient records or write operations.
