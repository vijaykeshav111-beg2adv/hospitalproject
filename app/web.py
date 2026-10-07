"""Server-rendered UI (Jinja2).

The UI uses the authenticated user's JWT through the
vvh_access_token browser cookie. API requests can still
use normal Authorization: Bearer authentication.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .deps import CurrentUser, get_optional_user

templates = Jinja2Templates(
    directory=str(
        Path(__file__).resolve().parent / "templates"
    )
)

router = APIRouter(
    prefix="/ui",
    tags=["Web UI"],
    include_in_schema=False,
)


def _ctx(
    request: Request,
    page: str,
    **kwargs,
):
    return {
        "request": request,
        "page": page,
        "app_name": settings.app_name,
        "env": settings.app_env,
        **kwargs,
    }


# ------------------------------------------------------------------
# PAGE AUTHENTICATION
# ------------------------------------------------------------------
# API routes answer 401 JSON, but a browser must never be shown a raw
# {"detail": "Not authenticated"} blob. Page routes use page_guard(), which
# raises UILoginRequired and lets the app send the browser to the login page
# (or back to the dashboard that belongs to the signed-in role).
class UILoginRequired(Exception):
    """Raised by page guards so browsers get a redirect instead of 401 JSON."""

    def __init__(self, next_url: str, role: str | None = None):
        self.next_url = next_url
        self.role = role


# Role -> landing page after login (mirrors login.html).
ROLE_HOME: dict[str, str] = {
    "SUPER_ADMIN": "/ui/super-admin",
    "ADMIN": "/ui/admin",
    "DOCTOR": "/ui/doctor",
    "RECEPTIONIST": "/ui/reception",
    "ACCOUNTANT": "/ui/billing",
    "PATIENT": "/ui/patient",
}


def home_for_role(role: str | None) -> str:
    return ROLE_HOME.get((role or "").upper(), "/ui")


def page_guard(*roles: str):
    """require_roles() for HTML pages: redirects instead of raising 401."""
    allowed = set(roles)

    def dependency(
        request: Request,
        user: CurrentUser | None = Depends(get_optional_user),
    ) -> CurrentUser:

        if user is None:
            raise UILoginRequired(request.url.path)

        if allowed and user.role not in allowed and user.role != "SUPER_ADMIN":
            # Signed in, but this page belongs to another role.
            raise UILoginRequired(request.url.path, role=user.role)

        return user

    return dependency


# ------------------------------------------------------------------
# PUBLIC PAGES
# ------------------------------------------------------------------

@router.get(
    "",
    response_class=HTMLResponse,
)
@router.get(
    "/",
    response_class=HTMLResponse,
)
def landing(request: Request):

    return templates.TemplateResponse(
        request,
        "landing.html",
        _ctx(request, "home"),
    )


@router.get(
    "/login",
    response_class=HTMLResponse,
)
def login_page(request: Request):

    return templates.TemplateResponse(
        request,
        "login.html",
        _ctx(request, "login"),
    )


@router.get(
    "/register",
    response_class=HTMLResponse,
)
def register_page(request: Request):

    return templates.TemplateResponse(
        request,
        "register.html",
        _ctx(request, "register"),
    )


@router.get(
    "/forgot-password",
    response_class=HTMLResponse,
)
def forgot_page(request: Request):

    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        _ctx(request, "forgot"),
    )


@router.get(
    "/reset-password",
    response_class=HTMLResponse,
)
def reset_page(request: Request):

    return templates.TemplateResponse(
        request,
        "reset_password.html",
        _ctx(request, "reset"),
    )


# ------------------------------------------------------------------
# DASHBOARDS
# ------------------------------------------------------------------

@router.get(
    "/super-admin",
    response_class=HTMLResponse,
)
def super_admin_dashboard(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("SUPER_ADMIN")
    ),
):

    return templates.TemplateResponse(
        request,
        "super_admin_dashboard.html",
        _ctx(request, "super_admin"),
    )


@router.get(
    "/admin",
    response_class=HTMLResponse,
)
def admin_dashboard(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("ADMIN")
    ),
):

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        _ctx(request, "admin"),
    )


@router.get(
    "/doctor",
    response_class=HTMLResponse,
)
def doctor_dashboard(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("DOCTOR")
    ),
):

    return templates.TemplateResponse(
        request,
        "doctor_dashboard.html",
        _ctx(request, "doctor"),
    )


@router.get(
    "/patient",
    response_class=HTMLResponse,
)
def patient_dashboard(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("PATIENT")
    ),
):

    return templates.TemplateResponse(
        request,
        "patient_dashboard.html",
        _ctx(request, "patient"),
    )


@router.get(
    "/reception",
    response_class=HTMLResponse,
)
def reception(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("RECEPTIONIST")
    ),
):

    return templates.TemplateResponse(
        request,
        "reception.html",
        _ctx(request, "reception"),
    )


@router.get(
    "/billing",
    response_class=HTMLResponse,
)
def billing(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("ACCOUNTANT")
    ),
):

    return templates.TemplateResponse(
        request,
        "billing.html",
        _ctx(request, "billing"),
    )


# ------------------------------------------------------------------
# CLINICAL
# ------------------------------------------------------------------

@router.get(
    "/clinical",
    response_class=HTMLResponse,
)
def clinical(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "DOCTOR",
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "clinical.html",
        _ctx(request, "clinical"),
    )


# ------------------------------------------------------------------
# PATIENT / STAFF OPERATIONS
# ------------------------------------------------------------------

@router.get(
    "/book",
    response_class=HTMLResponse,
)
def booking(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "PATIENT",
            "RECEPTIONIST",
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "booking.html",
        _ctx(request, "book"),
    )


@router.get(
    "/patients",
    response_class=HTMLResponse,
)
def patients(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "DOCTOR",
            "RECEPTIONIST",
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "patients.html",
        _ctx(request, "patients"),
    )


@router.get(
    "/appointments",
    response_class=HTMLResponse,
)
def appointments(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "PATIENT",
            "DOCTOR",
            "RECEPTIONIST",
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "appointments.html",
        _ctx(request, "appointments"),
    )


# ------------------------------------------------------------------
# AI
# ------------------------------------------------------------------

@router.get(
    "/ai-chat",
    response_class=HTMLResponse,
)
def ai_chat(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "PATIENT",
            "DOCTOR",
            "RECEPTIONIST",
            "ACCOUNTANT",
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "ai_chat.html",
        _ctx(request, "ai"),
    )


@router.get(
    "/ai-monitor",
    response_class=HTMLResponse,
)
def ai_monitor(
    request: Request,
    _: CurrentUser = Depends(
        page_guard(
            "ADMIN",
        )
    ),
):

    return templates.TemplateResponse(
        request,
        "ai_monitor.html",
        _ctx(request, "ai_monitor"),
    )


# ------------------------------------------------------------------
# ADMIN
# ------------------------------------------------------------------

@router.get(
    "/security",
    response_class=HTMLResponse,
)
def security(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("ADMIN")
    ),
):

    return templates.TemplateResponse(
        request,
        "security.html",
        _ctx(request, "security"),
    )


@router.get(
    "/system",
    response_class=HTMLResponse,
)
def system(
    request: Request,
    _: CurrentUser = Depends(
        page_guard("ADMIN")
    ),
):

    return templates.TemplateResponse(
        request,
        "system.html",
        _ctx(request, "system"),
    )