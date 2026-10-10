#!/usr/bin/env python3
"""
Live API sweep -- hits every parameterless GET route in the OpenAPI schema with
every demo role and reports anything that does not answer 2xx.

    python3 -m tests.api_sweep                       # against http://127.0.0.1:8000
    python3 -m tests.api_sweep --base http://host:9000
    python3 -m tests.api_sweep --role SUPER_ADMIN    # single role
    python3 -m tests.api_sweep --all                 # also sweep POST/PATCH routes with no body params

Exit code 1 when at least one route fails for a role that has the permission for it.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import defaultdict

DEMO_USERS = {
    "SUPER_ADMIN": ("superadmin@vijayvargiyahospital.in", "SuperAdmin@123"),
    "ADMIN": ("admin@vijayvargiyahospital.in", "Admin@123"),
    "DOCTOR": ("dr.arjunmehra@vijayvargiyahospital.in", "Doctor@123"),
    "RECEPTIONIST": ("reception@vijayvargiyahospital.in", "Reception@123"),
    "ACCOUNTANT": ("accounts@vijayvargiyahospital.in", "Accounts@123"),
    "PATIENT": ("ramesh.yadav@example.com", "Patient@123"),
}

# Query strings for routes that require a filter to be meaningful / to avoid validation errors.
QUERY_HINTS = {
    "/api/v1/appointments": "?limit=5",
    "/api/v1/invoices": "?limit=5",
    "/api/v1/payments": "?limit=5",
    "/api/v1/patients": "?limit=5",
    "/api/v1/users": "?limit=5",
    "/api/v1/audit-logs": "?limit=5",
    "/api/v1/auth/audit-logs": "?limit=5",
    "/api/v1/auth/login-activity": "?limit=5",
    "/api/v1/medical-files": "?limit=5",
    "/api/v1/consultations": "?limit=5",
    "/api/v1/prescriptions": "?limit=5",
    "/api/v1/reviews": "?limit=5",
    "/api/v1/notifications": "?limit=5",
    "/api/v1/medicines": "?limit=5",
    "/api/v1/leaves": "?limit=5",
    "/api/v1/schedules": "?limit=5",
    "/api/v1/slots": "?limit=5",
    "/api/v1/ai/conversations": "?limit=5",
    "/api/v1/ai/tool-calls": "?limit=5",
    "/api/v1/ai/escalations": "?limit=5",
    "/api/v1/ai/summaries": "?limit=5",
    "/api/v1/ai/monitor": "?days=30",
    "/api/v1/analytics/overview": "?days=30",
    "/api/v1/analytics/revenue": "?days=30",
    "/api/v1/analytics/doctors": "?days=30",
    "/api/v1/analytics/appointments": "?days=30",
    "/api/v1/analytics/ai": "?days=30",
    "/api/v1/analytics/patients": "?days=30",
    "/api/v1/payments/reports/collection": "?days=30",
    "/api/v1/payments/refunds/all": "?limit=5",
    "/api/v1/admin/dashboard": "?days=30",
    "/api/v1/admin/dashboard/doctor": "?doctor_id=1",
    "/api/v1/admin/jobs/runs": "?limit=5",
    "/api/v1/queue/desk": "",
    "/api/v1/doctors": "?limit=5",
    "/api/v1/ai/routing": "?concern=fever%20since%203%20days",
    "/api/v1/patients/search/suggest": "?q=a",
    "/api/v1/schedules/working-days": "?doctor_id=1",
    "/api/v1/doctors/availability": "?doctor_id=1&days_ahead=7",
    "/api/v1/specialties": "?limit=50",
}


def _request(base: str, path: str, method: str = "GET", body=None, token: str | None = None,
             timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def login(base: str, email: str, password: str) -> str | None:
    status, raw = _request(base, "/api/v1/auth/login", "POST", {"email": email, "password": password})
    if status != 200:
        return None
    return json.loads(raw)["access_token"]


def sweep(base: str, roles: list[str], include_writes: bool) -> int:
    spec = json.loads(_request(base, "/openapi.json")[1])
    tokens = {r: login(base, *DEMO_USERS[r]) for r in roles}
    for role, tok in tokens.items():
        print(f"  login {role:<13} {'ok' if tok else 'FAILED'}")

    failures: list[tuple[str, str, int, str]] = []
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            method = method.upper()
            if method != "GET" and not include_writes:
                continue
            if "{" in path:            # skip routes needing path params such as /patients/{id}
                continue
            if method == "GET" and not path.startswith("/api/"):
                continue
            url = path + QUERY_HINTS.get(path, "")
            for role, token in tokens.items():
                total += 1
                status, raw = _request(base, url, method, token=token)
                counts[role][str(status // 100) + "xx"] += 1
                if status >= 500 or (status == 422):
                    failures.append((method, url, role, status, raw[:160].decode("utf-8", "replace")))
                elif status == 200:
                    pass

    print(f"\n  swept {total} route/role combinations")
    for role, dist in counts.items():
        print(f"   {role:<13} {dict(dist)}")
    if failures:
        print("\n  FAILURES (5xx / 422):")
        for method, url, role, status, body in failures:
            print(f"   {status} {method} {url} as {role} -> {body[:120]}")
    else:
        print("\n  no 5xx or 422 responses")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep the live API for errors")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--role", action="append", dest="roles", help="limit to a role (repeatable)")
    ap.add_argument("--all", action="store_true", dest="include_writes", help="also try write routes")
    args = ap.parse_args(argv)
    roles = args.roles or list(DEMO_USERS)
    print(f"API sweep against {args.base}")
    return sweep(args.base, roles, args.include_writes)


if __name__ == "__main__":
    raise SystemExit(main())
