"""Send a test email with the SMTP settings from .env - no patient involved.

    python scripts/send_test_email.py you@gmail.com
    python scripts/send_test_email.py               # sends to EMAIL_FROM

Use this to prove the email setup before you rely on it for real patients.
If it fails, the printed error is exactly what the SMTP server said.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services import notify  # noqa: E402


def main() -> int:
    to = sys.argv[1] if len(sys.argv) > 1 else settings.email_from
    print("=" * 74)
    print("  EMAIL TEST - Vijay Vargiya Group of Hospitals")
    print("=" * 74)
    print(f"  EMAIL_ENABLED : {settings.email_enabled}")
    print(f"  SMTP server   : {settings.smtp_host}:{settings.smtp_port}")
    print(f"  SMTP user     : {settings.smtp_user or '(empty)'}")
    print(f"  From          : {settings.email_from}")
    print(f"  To            : {to}")
    print("-" * 74)

    if not settings.email_enabled:
        print("  EMAIL_ENABLED=false in .env")
        print()
        print("  Turn it on first:")
        print("    1. copy .env.example -> .env   (already done if the app runs)")
        print("    2. set EMAIL_ENABLED=true, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM")
        print("    3. re-run this script")
        return 2

    ok, reference, error = notify._send_email(
        to,
        f"{settings.app_name} - email test",
        ("This is a test email from the hospital management system.\n\n"
         "If you can read this, EMAIL notifications are working: appointment "
         "confirmations, payment receipts, prescription-ready and reminders "
         "will reach patients by email as well.\n"),
    )
    if ok:
        print(f"  [ OK ] sent via {reference} - check the inbox (and spam folder)")
        return 0
    print(f"  [FAIL] {error}")
    print()
    print("  Common causes:")
    print("    * Gmail needs an APP PASSWORD (2-step verification), not your login password")
    print("    * wrong SMTP_PORT: 587 = STARTTLS (default), 465 = SSL, 25 = plain")
    print("    * EMAIL_FROM must belong to the same account as SMTP_USER on most providers")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
