"""Configuration for the MCP layer.

Everything is env-tunable so the gateway can run:
  * inside the hospital app   (mounted at /mcp by app.main)
  * standalone                (python -m mcp_layer.gateway)
  * per-server                (python -m mcp_layer.servers.<key>  -> ports 8101+)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def _load_root_dotenv() -> None:
    """Best-effort .env load so standalone runs see the same secrets as app/."""
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        root = Path(__file__).resolve().parent.parent
        env_file = root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
    except Exception:  # noqa: BLE001 - dotenv is optional at runtime
        pass


_load_root_dotenv()


@dataclass
class MCPSettings:
    # ---- transport / ports ---------------------------------------------
    gateway_host: str = field(default_factory=lambda: _env("MCP_HOST", "0.0.0.0"))
    gateway_port: int = field(default_factory=lambda: int(_env("MCP_PORT", "8100")))
    first_server_port: int = field(default_factory=lambda: int(_env("MCP_FIRST_PORT", "8101")))

    # ---- security ------------------------------------------------------
    # System callers (automation, other services) authenticate with this key
    # instead of a user JWT.  Role "SYSTEM" -> level SYSTEM (all 10 servers).
    system_api_key: str = field(default_factory=lambda: _env("MCP_SYSTEM_API_KEY", ""))
    # Demo/teaching mode lets the dashboard impersonate tiers WITHOUT a JWT
    # only when explicitly enabled.  Production must leave this off.
    allow_demo_roles: bool = field(
        default_factory=lambda: _env("MCP_ALLOW_DEMO_ROLES", "false").lower() in {"1", "true", "yes"}
    )
    # JWT secret is shared with the hospital app so its login tokens work here.
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", "change-this-to-a-long-random-secret-string-in-production"))
    jwt_algorithm: str = field(default_factory=lambda: _env("JWT_ALGORITHM", "HS256"))

    # ---- guard / hallucination control ---------------------------------
    # How long a DB probe result is trusted before re-checking (seconds).
    db_probe_ttl_seconds: float = field(default_factory=lambda: float(_env("MCP_DB_PROBE_TTL", "3")))
    # Hard rule: when the DB is down every tool answers "no" and the chatbot
    # must not generate.  These flags exist only so tests can toggle paths;
    # they default to the safe behaviour and must never be flipped in prod.
    refuse_when_db_down: bool = field(
        default_factory=lambda: _env("MCP_REFUSE_WHEN_DB_DOWN", "true").lower() in {"1", "true", "yes"}
    )

    # ---- chatbot --------------------------------------------------------
    chatbot_model: str = field(default_factory=lambda: _env("MCP_CHATBOT_MODEL", "grounded-router-v1"))
    # Optional Groq polish (wording only).  Disabled by default: answers are
    # built strictly from tool data, never from a free-form LLM.
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-20b"))

    @property
    def server_ports(self) -> dict[str, int]:
        """Standalone port for each of the 10 servers (registry order)."""
        from .registry import SERVERS  # local import avoids cycle

        return {s.key: self.first_server_port + i for i, s in enumerate(SERVERS)}


settings = MCPSettings()
