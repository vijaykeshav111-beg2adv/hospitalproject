"""FastAPI dependencies: authentication, RBAC, permission checks and auditing."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .rbac import permissions_for_role
from .security import decode_token, utcnow

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_ALIASES = {"VIJAY_ADMIN": "SUPER_ADMIN"}

# Cookie used by server-rendered /ui pages.
UI_ACCESS_COOKIE = "vvh_access_token"


class CurrentUser:
    """Lightweight authenticated principal."""

    def __init__(
        self,
        user: models.User,
        session: models.UserSession | None,
        payload: dict,
    ):
        self.user = user
        self.session = session
        self.payload = payload

    @property
    def id(self) -> int:
        return self.user.id

    @property
    def role(self) -> str:
        return ROLE_ALIASES.get(self.user.role_name, self.user.role_name)

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def permissions(self) -> set[str]:
        return set(permissions_for_role(self.role))

    def has_permission(self, code: str) -> bool:
        perms = self.permissions
        return code in perms or f"{code.split(':')[0]}:*" in perms

    def is_patient(self) -> bool:
        return self.role == "PATIENT"


def _extract_token(
    credentials: HTTPAuthorizationCredentials | None,
    authorization: str | None,
    token_param: str | None,
    cookie_token: str | None,
) -> str | None:
    """
    Extract access token in this priority:

    1. Authorization: Bearer <token>
    2. Explicit Authorization header
    3. X-Access-Token / ?token=
    4. Server-rendered UI cookie
    """

    # Standard API authentication.
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials

    # Explicit Authorization header.
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()

    # Existing compatibility mechanism.
    if token_param:
        return token_param

    # Server-rendered UI authentication.
    if cookie_token:
        return cookie_token

    return None


def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    authorization: str | None = Header(default=None),
    x_access_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser | None:

    # Browser / server-rendered UI authentication.
    cookie_token = request.cookies.get(UI_ACCESS_COOKIE)

    token = _extract_token(
        credentials=credentials,
        authorization=authorization,
        token_param=x_access_token or request.query_params.get("token"),
        cookie_token=cookie_token,
    )

    if not token:
        return None

    try:
        payload = decode_token(token)
    except Exception:  # noqa: BLE001
        return None

    if payload.get("type") != "access":
        return None

    try:
        user_id = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        return None

    if not user_id:
        return None

    user = db.get(models.User, user_id)

    if not user or not user.is_active:
        return None

    session = None
    jti = payload.get("jti")

    if jti:
        session = db.scalar(
            select(models.UserSession).where(
                models.UserSession.access_jti == jti,
                models.UserSession.is_revoked.is_(False),
            )
        )

    return CurrentUser(
        user=user,
        session=session,
        payload=payload,
    )


def get_current_user(
    user: CurrentUser | None = Depends(get_optional_user),
) -> CurrentUser:

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated or token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_roles(*roles: str) -> Callable[[CurrentUser], CurrentUser]:
    allowed = {ROLE_ALIASES.get(r, r) for r in roles}

    def checker(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:

        if user.role not in allowed and user.role != "SUPER_ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role '{user.role}' is not allowed. "
                    f"Required: {sorted(allowed)}"
                ),
            )

        return user

    return checker


def require_permission(
    *codes: str,
    any_of: bool = False,
) -> Callable[[CurrentUser], CurrentUser]:
    """RBAC + permission based access control decorator dependency."""

    def checker(
        user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:

        if user.role == "SUPER_ADMIN":
            return user

        checks = [
            user.has_permission(code)
            for code in codes
        ]

        ok = any(checks) if any_of else all(checks)

        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Missing permission(s): "
                    f"{', '.join(codes)}"
                ),
            )

        return user

    return checker


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")

    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.client.host if request.client else "unknown"


def audit(
    db: Session,
    *,
    action: str,
    resource: str,
    resource_id: Any = None,
    user: CurrentUser | models.User | None = None,
    request: Request | None = None,
    previous_value: Any = None,
    new_value: Any = None,
    commit: bool = True,
) -> models.AuditLog:
    """Write an audit-log row."""

    actor_user = getattr(user, "user", user) if user is not None else None

    role_value = getattr(user, "role", None)

    if hasattr(role_value, "name"):
        role_value = role_value.name

    if not isinstance(role_value, str):
        role_value = getattr(actor_user, "role_name", None)

    entry = models.AuditLog(
        user_id=getattr(actor_user, "id", None),
        user_email=getattr(actor_user, "email", None),
        user_role=role_value,
        action=action,
        resource=resource,
        resource_id=(
            str(resource_id)
            if resource_id is not None
            else None
        ),
        previous_value=_jsonable(previous_value),
        new_value=_jsonable(new_value),
        ip_address=(
            client_ip(request)
            if request
            else None
        ),
        user_agent=(
            request.headers.get("user-agent", "")[:255]
            if request
            else None
        ),
    )

    db.add(entry)

    if commit:
        db.commit()

    return entry


def _jsonable(value: Any) -> Any:

    if value is None:
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            k: _jsonable(v)
            for k, v in value.items()
        }

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [
            _jsonable(v)
            for v in value
        ]

    if hasattr(value, "__table__"):
        return {
            c.name: _jsonable(
                getattr(value, c.name)
            )
            for c in value.__table__.columns
            if c.name not in {"password_hash"}
        }

    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def ensure_patient_scope(
    user: CurrentUser,
    db: Session,
    patient_id: int,
) -> models.Patient:

    patient = db.get(
        models.Patient,
        patient_id,
    )

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    if user.role == "PATIENT":
        if patient.user_id != user.id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "You can only access "
                    "your own records"
                ),
            )

    return patient


def patient_for_user(
    db: Session,
    user_id: int,
) -> models.Patient | None:

    return db.scalar(
        select(models.Patient).where(
            models.Patient.user_id == user_id
        )
    )


def current_patient(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.Patient:

    patient = patient_for_user(
        db,
        user.id,
    )

    if not patient:
        raise HTTPException(
            status_code=404,
            detail=(
                "No patient profile linked "
                "to this login"
            ),
        )

    return patient


def paginate(
    query,
    page: int = 1,
    page_size: int = 20,
):

    page = max(1, page)
    page_size = min(
        max(1, page_size),
        200,
    )

    total = query.order_by(None).count()

    items = (
        query
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return (
        total,
        items,
        page,
        page_size,
    )