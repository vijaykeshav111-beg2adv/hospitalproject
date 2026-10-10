#!/usr/bin/env python3
"""
Vijay Vargiya Group of Hospitals -- application launcher.

Usage
-----
    python run.py                      # start on 0.0.0.0:8000 (MySQL)
    python run.py --port 9000          # custom port
    python run.py --reload             # auto-reload for development
    python run.py --workers 4          # production (no reload)
    python run.py --no-scheduler       # disable APScheduler background jobs
    python run.py --seed               # create/refresh the demo dataset, then serve

The same app object can be served by any ASGI server:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

BANNER = r"""
  __      ___ _                  __      ___            _  _
  \ \    / (_| | __ _ _   _     / /     / _ \ _ __ __ _(_)(_)_   _  __ _
   \ \/\/ /| | |/ _` | | | |   / /_____| | | | '__/ _` | || | | | |/ _` |
    \  /\  /| | | (_| | |_| | / /______| |_| | | | (_| | || |_| | | (_| |
     \/  \/ |_|_|\__,_|\__, | \/        \___/|_|  \__,_|_|/\__,_|_|\__,_|
                       |___/
      V I J A Y   V A R G I I Y A   G R O U P   O F   H O S P I T A L
              Hospital Management, AI Front-Desk & Billing
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Vijay Vargiya Group of Hospitals server")
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"), help="bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")), help="bind port (default 8000)")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    p.add_argument("--workers", type=int, default=int(os.getenv("WEB_CONCURRENCY", "1")),
                   help="number of worker processes (ignored with --reload)")
    p.add_argument("--no-scheduler", action="store_true", help="do not start APScheduler background jobs")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"),
                   choices=["critical", "error", "warning", "info", "debug", "trace"])
    p.add_argument("--seed", action="store_true", help="run the idempotent seeder before serving")
    p.add_argument("--quiet", action="store_true", help="do not print the startup banner")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.no_scheduler:
        os.environ["ENABLE_SCHEDULER"] = "false"

    # Imported after env tweaks so Settings picks up the flags above.
    from app.config import settings
    from app.database import db_health

    if args.seed:
        from app.seed import seed_all

        summary = seed_all(force=False)
        print("[seed]", summary)

    health = db_health()
    try:
        import uvicorn
    except ModuleNotFoundError:  # pragma: no cover
        print("uvicorn is not installed. Run:  pip install -r requirements.txt", file=sys.stderr)
        return 1

    if not args.quiet:
        print(BANNER)
        print(f"  Application : {settings.app_name}  ({settings.app_env})")
        print(f"  Database    : {health.get('backend')} -> {health.get('database')}"
              )
        print(f"  Storage     : {settings.storage_dir}")
        print(f"  AI provider : {settings.ai_provider}    WhatsApp: {'on' if settings.whatsapp_enabled else 'off (dry-run)'}")
        print(f"  Scheduler   : {'on' if settings.enable_scheduler else 'off'}")
        print()
        print(f"  Web portals : http://127.0.0.1:{args.port}/ui")
        print(f"  API docs    : http://127.0.0.1:{args.port}/docs")
        print(f"  Health      : http://127.0.0.1:{args.port}/api/v1/admin/health")
        print()
        print("  Demo logins : superadmin@vijayvargiyahospital.in / SuperAdmin@123")
        print("                admin@vijayvargiyahospital.in      / Admin@123")
        print("                dr.arjunmehra@vijayvargiyahospital.in / Doctor@123")
        print("                reception@vijayvargiyahospital.in  / Reception@123")
        print("                accounts@vijayvargiyahospital.in   / Accounts@123")
        print("                ramesh.yadav@example.com            / Patient@123")
        print()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        workers=1 if args.reload else max(1, args.workers),
        log_level=args.log_level,
        access_log=args.log_level in {"info", "debug", "trace"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
