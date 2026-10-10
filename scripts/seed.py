#!/usr/bin/env python3
"""
Seed / reset helper for the Vijay Vargiya Group of Hospitals database.

Examples
--------
    python scripts/seed.py                 # seed only when the DB is empty (idempotent)
    python scripts/seed.py --force          # wipe the demo data and re-seed from scratch
    python scripts/seed.py --force --sql    # also refresh sql/03_seed.sql (MySQL dump)
    python scripts/seed.py --json           # machine-readable summary

What gets created: RBAC roles + permissions, 12 specialties with symptom map,
20 medicines, 8 doctors with weekly schedules, 4 staff accounts, 12 patients with
history, sample AI conversations and 14 days of appointment slots.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the VVH database")
    parser.add_argument("--force", action="store_true",
                        help="delete existing rows of the seeded tables and re-create the demo data")
    parser.add_argument("--sql", action="store_true",
                        help="also regenerate sql/03_seed.sql from the freshly seeded database")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print a JSON summary only")
    args = parser.parse_args(argv)

    from app.database import db_health, init_db

    if args.force:
        # Reproducible demo state: recreate the schema before seeding.
        init_db(drop=True)

    from app.seed import STAFF, seed_all

    summary = seed_all(force=args.force)
    health = db_health()

    if args.sql:
        from app.tools.export_seed import export as export_seed_sql

        path = export_seed_sql(str(BASE_DIR / "sql" / "03_seed.sql"))
        summary["sql_seed"] = str(path)

    if args.as_json:
        print(json.dumps({"summary": summary, "database": health}, indent=2, default=str))
        return 0

    print("=" * 74)
    print("  Vijay Vargiya Group of Hospitals - database seed")
    print("=" * 74)
    print(f"  Backend   : {health.get('backend')} -> {health.get('database')}"
          )
    print(f"  Outcome   : {'already seeded - nothing to do (use --force to rebuild)' if summary.get('skipped') else 'seeded'}")
    if summary.get("slots_created") is not None:
        print(f"  New slots : {summary['slots_created']}")
    if args.sql:
        print(f"  SQL dump  : {summary.get('sql_seed')}")
    print()
    print("  Staff logins (email / password)")
    print("  " + "-" * 70)
    for full_name, email, role_name, password, phone in STAFF:
        print(f"   {role_name:<12} {email:<46} {password}")
    print(f"   {'DOCTOR':<12} {'dr.arjunmehra@vijayvargiyahospital.in':<46} Doctor@123")
    print(f"   {'PATIENT':<12} {'ramesh.yadav@example.com':<46} Patient@123")
    print()
    print("  Every seeded doctor uses the password  Doctor@123  and every seeded")
    print("  patient uses  Patient@123 .")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
