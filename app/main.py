"""Vijay Vargiya Group of Hospitals - FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import db_health, init_db
from .routers import (admin, ai, ai_files, analytics, appointments, auth, consultations, doctors,
                      invoices, leaves, medical_files, medical_records, medicines, notifications,
                      patients, payments, prescriptions, queue, reviews, schedules, specialties,
                      users)
from .services import jobs
from .web import UILoginRequired, home_for_role
from .web import router as web_router
from .routers import legacy_ml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("vvh")


def _log_config_warnings() -> None:
    """Surface dangerous .env combinations immediately at startup.

    These are warnings, never fatal - the app keeps serving - but they are the
    kind of misconfiguration that is otherwise only discovered much later as
    "the app works but the tests / PDF / CLI say access denied".
    """
    for warning in settings.database_config_warnings():
        log.warning("DATABASE CONFIG: %s", warning)

    placeholder = {"", "change-this-to-a-long-random-secret-string-in-production",
                   "change-me", "secret"}
    if settings.jwt_secret in placeholder or len(settings.jwt_secret) < 32:
        log.warning(
            "SECURITY: JWT_SECRET is a placeholder or shorter than 32 characters. "
            "Anyone who knows it can forge login tokens for every role including "
            "SUPER_ADMIN. Generate one with:  python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\""
        )

    log.info("AI front desk mode: %s (model=%s, key=%s)",
             settings.ai_mode, settings.groq_model,
             "set" if settings.groq_api_key else "not set - running local")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log_config_warnings()
    init_db()
    log.info("Schema ready on mysql")
    try:
        from .seed import seed_all
        seed_all()
    except Exception as exc:  # noqa: BLE001
        log.warning("Seeding skipped: %s", exc)
    scheduler = jobs.start_scheduler()
    log.info("%s started | database=%s | scheduler=%s", settings.app_name,
             db_health().get("backend"), bool(scheduler))
    yield
    jobs.stop_scheduler()
    log.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} - Hospital Management Platform",
        description=(
            "Authentication & RBAC, patient system, doctor scheduling, slot engine, appointments & "
            "queue, clinical records, prescriptions, billing, WhatsApp notifications, analytics and an "
            "AI front-desk assistant powered by Groq with 14 controlled tools, memory and medical-safety guardrails."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- API v1 ---------------------------------------------------------
    api_routers = [
        auth.router, users.router, patients.router, doctors.router, specialties.router,
        schedules.router, schedules.slot_router, leaves.router, appointments.router, queue.router,
        consultations.router, medical_records.router, medical_files.router, medicines.router,
        prescriptions.router, invoices.router, payments.router, reviews.router,
        notifications.router, notifications.wa_router, ai.router, ai_files.router,
        analytics.router,
        analytics.admin_router, admin.router,
    ]
    for r in api_routers:
        app.include_router(r, prefix="/api/v1")

    # ---- UI -------------------------------------------------------------
    app.include_router(web_router)
    app.include_router(legacy_ml.router)

    static_dir = settings.storage_dir.parent / "app" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    @app.exception_handler(UILoginRequired)
    async def ui_login_required(request: Request, exc: UILoginRequired):
        """Browser navigation never gets a raw 401 JSON body.

        Anonymous visitor  -> /ui/login?next=<page>
        Signed-in, wrong role -> that role's own dashboard
        """
        if exc.role:
            return RedirectResponse(url=f"{home_for_role(exc.role)}?denied=1", status_code=302)
        return RedirectResponse(url=f"/ui/login?next={quote(exc.next_url)}", status_code=302)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": "Internal server error",
            "path": request.url.path,
            "hint": "Check the server logs for the full traceback.",
        })

    return app


app = create_app()
