"""RBAC for the MCP layer.

Three access tiers (matching the platform's Role -> Access contract):

    Role          Access        May call
    ------------  ------------  --------------------------------------------
    User          USER          the 5 USER servers
    Admin         ADMIN         the 5 USER + 3 ADMIN servers
    Super Admin   SYSTEM        all 10 servers (USER + ADMIN + SYSTEM)
    (service)     SYSTEM        all 10 servers via MCP_SYSTEM_API_KEY

Hospital JWT roles are mapped onto the tiers:

    PATIENT / DOCTOR / RECEPTIONIST / ACCOUNTANT  ->  USER
    ADMIN                                         ->  ADMIN
    SUPER_ADMIN                                   ->  SYSTEM

Every server in mcp_layer.registry declares `tier` in {"USER","ADMIN","SYSTEM"}
and invoke() refuses calls above the principal's tier with an RbacDenied error
which the tools render as a grounded refusal (never as fake data).
"""
from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass, field

from .config import settings

# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------
USER = "USER"
ADMIN = "ADMIN"
SYSTEM = "SYSTEM"

LEVELS: dict[str, int] = {USER: 10, ADMIN: 20, SYSTEM: 30}
TIER_ORDER = (USER, ADMIN, SYSTEM)

# Hospital role (as carried in the JWT "role" claim) -> MCP tier.
HOSPITAL_ROLE_TIER: dict[str, str] = {
    "PATIENT": USER,
    "DOCTOR": USER,
    "RECEPTIONIST": USER,
    "ACCOUNTANT": USER,
    "ADMIN": ADMIN,
    "SUPER_ADMIN": SYSTEM,
    # machine / automation principal
    "SYSTEM": SYSTEM,
    # aliases kept for compatibility with older tokens
    "VIJAY_ADMIN": SYSTEM,
    "USER": USER,
}

# Which hospital roles a tier in the 5/3/2 split is *for* (dashboard matrix).
TIER_AUDIENCE: dict[str, tuple[str, ...]] = {
    USER: ("PATIENT", "DOCTOR", "RECEPTIONIST", "ACCOUNTANT"),
    ADMIN: ("ADMIN",),
    SYSTEM: ("SUPER_ADMIN", "SYSTEM"),
}


class RbacDenied(PermissionError):
    """Principal's tier is below the server's required tier."""


@dataclass
class Principal:
    """An authenticated MCP caller."""

    subject: str                      # email / service name
    role: str                         # hospital role (or SYSTEM)
    tier: str = USER                  # USER | ADMIN | SYSTEM
    user_id: int | None = None
    auth: str = "none"                # jwt | api-key | demo | anonymous
    permissions: set[str] = field(default_factory=set)

    @property
    def level(self) -> int:
        return LEVELS.get(self.tier, 0)

    def can_call(self, required_tier: str) -> bool:
        return self.level >= LEVELS.get(required_tier, 999)

    def describe(self) -> dict:
        return {
            "subject": self.subject,
            "role": self.role,
            "tier": self.tier,
            "user_id": self.user_id,
            "auth": self.auth,
        }


ANONYMOUS = Principal(subject="anonymous", role="ANONYMOUS", tier="NONE", auth="anonymous")

# The principal of the call currently being executed.  Raw tool bodies read
# this for row-level scoping (e.g. a PATIENT may only see their own rows).
_current_principal: ContextVar[Principal] = ContextVar("mcp_current_principal", default=ANONYMOUS)


def set_current_principal(principal: Principal):
    return _current_principal.set(principal)


def reset_current_principal(token) -> None:
    _current_principal.reset(token)


def current_actor() -> Principal:
    return _current_principal.get()


def tier_for_role(role: str | None) -> str:
    return HOSPITAL_ROLE_TIER.get((role or "").upper(), USER)


def check(principal: Principal, required_tier: str) -> None:
    if required_tier not in LEVELS:
        raise RbacDenied(f"unknown tier {required_tier!r}")
    if not principal.can_call(required_tier):
        raise RbacDenied(
            f"tier {principal.tier} (role {principal.role}) cannot access {required_tier} servers"
        )


# --------------------------------------------------------------------------
# Principal extraction (shared by gateway, REST bridge and MCP tools)
# --------------------------------------------------------------------------
def principal_from_jwt(token: str) -> Principal | None:
    """Verify a hospital-app access token.  Pure HMAC - works even if the DB
    is down, so RBAC keeps protecting the MCP surface during outages."""
    if not token:
        return None
    try:
        import jwt  # PyJWT

        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except Exception:  # noqa: BLE001
        return None

    role = str(payload.get("role") or "PATIENT").upper()
    return Principal(
        subject=str(payload.get("email") or payload.get("sub") or "user"),
        role=role,
        tier=tier_for_role(role),
        user_id=int(payload.get("uid") or payload.get("user_id") or payload.get("sub")) if str(payload.get("uid") or payload.get("user_id") or payload.get("sub") or "").isdigit() else None,
        auth="jwt",
    )


def principal_from_api_key(key: str) -> Principal | None:
    if not key or not settings.system_api_key:
        return None
    if not hmac.compare_digest(key, settings.system_api_key):
        return None
    return Principal(subject="system-service", role="SYSTEM", tier=SYSTEM, auth="api-key")


def principal_from_headers(headers: dict, demo_role: str | None = None) -> Principal:
    """Resolve the caller from an HTTP header bag.

    Order: Bearer JWT -> X-MCP-API-Key -> optional demo role (only when
    MCP_ALLOW_DEMO_ROLES=true, used by the classroom dashboard).
    """
    # Normalise header dict (case-insensitive).
    h = {str(k).lower(): (v or "") for k, v in (headers or {}).items()}

    auth = h.get("authorization", "")
    if auth.lower().startswith("bearer "):
        p = principal_from_jwt(auth.split(" ", 1)[1].strip())
        if p:
            return p

    raw = h.get("x-access-token") or h.get("token")
    if raw:
        p = principal_from_jwt(raw)
        if p:
            return p

    p = principal_from_api_key(h.get("x-mcp-api-key", ""))
    if p:
        return p

    if demo_role and settings.allow_demo_roles:
        role = demo_role.upper()
        return Principal(subject=f"demo:{role}", role=role, tier=tier_for_role(role), auth="demo")

    return ANONYMOUS
