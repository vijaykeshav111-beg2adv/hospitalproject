#!/usr/bin/env python3
"""
UI + API health check for a RUNNING server (default http://127.0.0.1:8000).

    python3 run.py                     # start the app in another terminal
    python3 -m tests.page_check        # every /ui page must render, key APIs must answer 200

Checks:
  * public pages (landing, login, register, password reset) render anonymously
  * every PROTECTED page redirects an anonymous browser to /ui/login?next=...
    instead of answering with a raw 401 JSON body
  * every protected page renders 200 for the role that owns it, with no
    un-rendered {{ }} / {% %} left in the HTML
  * static assets (favicon/logo), /docs, /openapi.json and the / → /ui redirect
  * logins for all six demo roles
  * the read endpoints behind every portal page
  * anonymous AI chat still answers (public widget)
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request

PUBLIC_PAGES = ["/ui", "/ui/", "/ui/login", "/ui/register", "/ui/forgot-password", "/ui/reset-password"]

# page -> the role that is allowed to open it (page_guard in app/web.py).
# SUPER_ADMIN can open every page, so it is not listed separately.
PROTECTED_PAGES = {
    "/ui/super-admin": "SUPER_ADMIN",
    "/ui/admin": "ADMIN",
    "/ui/doctor": "DOCTOR",
    "/ui/patient": "PATIENT",
    "/ui/reception": "RECEPTIONIST",
    "/ui/billing": "ACCOUNTANT",
    "/ui/clinical": "DOCTOR",
    "/ui/access": "ADMIN",
    "/ui/book": "RECEPTIONIST",
    "/ui/patients": "RECEPTIONIST",
    "/ui/appointments": "RECEPTIONIST",
    "/ui/ai-chat": "PATIENT",
    "/ui/ai-monitor": "ADMIN",
    "/ui/security": "ADMIN",
    "/ui/system": "ADMIN",
}

UI_PAGES = PUBLIC_PAGES + list(PROTECTED_PAGES)

DEMO = {
    "SUPER_ADMIN": ("superadmin@vijayvargiyahospital.in", "SuperAdmin@123"),
    "ADMIN": ("admin@vijayvargiyahospital.in", "Admin@123"),
    "RECEPTIONIST": ("reception@vijayvargiyahospital.in", "Reception@123"),
    "DOCTOR": ("dr.arjunmehra@vijayvargiyahospital.in", "Doctor@123"),
    "ACCOUNTANT": ("accounts@vijayvargiyahospital.in", "Accounts@123"),
    "PATIENT": ("ramesh.yadav@example.com", "Patient@123"),
}

READ_ENDPOINTS = [
    "/api/v1/admin/health", "/api/v1/admin/system", "/api/v1/admin/settings", "/api/v1/admin/jobs",
    "/api/v1/admin/dashboard?days=30", "/api/v1/admin/dashboard/doctor?doctor_id=1",
    "/api/v1/analytics/overview?days=30", "/api/v1/analytics/revenue?days=30",
    "/api/v1/queue/desk", "/api/v1/appointments?limit=10", "/api/v1/appointments/today?limit=20",
    "/api/v1/patients?limit=10", "/api/v1/patients/search/suggest?q=a",
    "/api/v1/doctors?active_only=true", "/api/v1/specialties", "/api/v1/schedules",
    "/api/v1/slots?limit=10", "/api/v1/medicines?limit=10", "/api/v1/leaves?limit=10",
    "/api/v1/invoices?limit=10", "/api/v1/payments?limit=10",
    "/api/v1/payments/reports/collection?days=30", "/api/v1/reviews?limit=10",
    "/api/v1/notifications?limit=10", "/api/v1/medical-files?limit=10",
    "/api/v1/consultations?limit=10", "/api/v1/prescriptions?limit=10",
    "/api/v1/auth/audit-logs?limit=10", "/api/v1/auth/login-activity?limit=10",
    "/api/v1/auth/sessions", "/api/v1/users?limit=10", "/api/v1/permissions/matrix",
    "/api/v1/ai/monitor?days=30", "/api/v1/ai/tools", "/api/v1/ai/escalations",
    "/api/v1/ai/conversations?limit=10",
]

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 30x response instead of silently following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def request(base: str, path: str, method: str = "GET", body=None, token: str | None = None,
            cookie: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    def _headers(message) -> dict:
        # HTTP header names are case-insensitive: normalise so lookups work.
        return {k.lower(): v for k, v in message.items()}

    try:
        with _opener.open(req, timeout=30) as resp:
            return resp.status, resp.read(), _headers(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _headers(exc.headers)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check every page and key endpoints of a running server")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    print(f"UI pages @ {base}")
    for page in PUBLIC_PAGES:
        status, raw, _ = request(base, page)
        html = raw.decode("utf-8", "replace")
        title = re.search(r"<title>(.*?)</title>", html)
        check(f"{page:<22} {title.group(1) if title else '-'}",
              status == 200 and "<html" in html.lower() and "{{" not in html and "{%" not in html,
              f"status={status}")

    print("\nProtected pages redirect anonymous browsers to the login page")
    for page in PROTECTED_PAGES:
        status, _, headers = request(base, page)
        location = headers.get("location", "")
        check(f"{page:<22} -> {location or 'no redirect'}",
              status in (301, 302, 303, 307) and "/ui/login" in location and "next=" in location,
              f"status={status} location={location!r}")

    print("\nStatic assets and docs")
    for path in ["/static/favicon.svg", "/static/logo.svg", "/docs", "/openapi.json"]:
        status, _, _ = request(base, path)
        check(path, status == 200, f"status={status}")

    print("\nRoles and API")
    tokens: dict[str, str] = {}
    for role, (email, password) in DEMO.items():
        status, raw, _ = request(base, "/api/v1/auth/login", "POST", {"email": email, "password": password})
        check(f"login {role}", status == 200, f"status={status}")
        if status == 200:
            tokens[role] = json.loads(raw)["access_token"]

    print("\nProtected pages render for the role that owns them")
    for page, role in PROTECTED_PAGES.items():
        tok = tokens.get(role)
        if not tok:
            check(f"{page:<22} (no {role} token)", False, "login failed")
            continue
        status, raw, _ = request(base, page, cookie=f"vvh_access_token={tok}")
        html = raw.decode("utf-8", "replace")
        title = re.search(r"<title>(.*?)</title>", html)
        check(f"{page:<22} {title.group(1) if title else '-'}",
              status == 200 and "<html" in html.lower() and "{{" not in html and "{%" not in html,
              f"status={status}")

    token = tokens.get("SUPER_ADMIN")
    for path in READ_ENDPOINTS:
        status, raw, _ = request(base, path, token=token)
        check(f"GET {path}", status == 200, f"status={status} {raw[:80]!r}")

    if "PATIENT" in tokens:
        for path in ["/api/v1/patients/me", "/api/v1/admin/dashboard/patient"]:
            status, _, _ = request(base, path, token=tokens["PATIENT"])
            check(f"GET {path} (patient)", status == 200, f"status={status}")

    status, raw, _ = request(base, "/api/v1/ai/chat", "POST", {"message": "hello"})
    check("anonymous AI chat", status == 200 and "reply" in json.loads(raw or b"{}"),
          f"status={status}")

    # PDF downloads must work with ?token= from the browser
    if token:
        status, raw, _ = request(base, "/api/v1/invoices?limit=1", token=token)
        if status == 200 and json.loads(raw):
            invoice_id = json.loads(raw)[0]["id"]
            for suffix in ("receipt", "pdf"):
                st, body, _ = request(base, f"/api/v1/invoices/{invoice_id}/{suffix}?token={token}")
                check(f"invoice {suffix} PDF with ?token=", st == 200 and body[:4] == b"%PDF", f"status={st}")

    # ------------------------------------------------------------------
    # The demo logins printed on the login page must really work. This is the
    # guard for the bug where three of the six emails used a different domain
    # ("@vijayvargiyahospital.in") than the seeded users, so doctor / reception /
    # accounts demo logins always failed with "Invalid email or password".
    print("\nDemo logins advertised on /ui/login")
    status, raw, _ = request(base, "/ui/login")
    login_html = raw.decode("utf-8", "replace")
    advertised = re.findall(r"'([\w.+-]+@[\w.-]+)',\s*\n?\s*'([^']+)'", login_html)
    if not advertised:
        # the templates are generated with one pair per line - fall back to a
        # loose scan of the demos array
        block = login_html.split("const demos", 1)[-1].split("];", 1)[0]
        pairs = re.findall(r"'([^']+)'", block)
        advertised = [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)]
    check("the login page advertises the demo accounts", len(advertised) >= 6,
          f"found {len(advertised)}")
    for email, password in advertised[:8]:
        st, body, _ = request(base, "/api/v1/auth/login", "POST",
                              {"email": email, "password": password})
        check(f"demo login works: {email}", st == 200, f"status={st} {body[:60]!r}")

    # ------------------------------------------------------------------
    # /ui/access + /api/v1/auth/roles/matrix must agree with the page guards
    print("\nRole access matrix")
    if token:
        st, body, _ = request(base, "/api/v1/auth/roles/matrix", token=token)
        check("GET /api/v1/auth/roles/matrix", st == 200, f"status={st}")
        if st == 200:
            matrix = json.loads(body)
            names = {p["name"] for p in matrix.get("permissions", [])}
            pages = {p["path"] for p in matrix.get("pages", [])}
            check("matrix lists the roles", len(matrix.get("roles", [])) == 6, str(matrix.get("roles")))
            check("matrix lists permissions", len(names) >= 40, f"{len(names)} permissions")
            check("matrix covers every protected page",
                  set(PROTECTED_PAGES) <= pages, f"missing {set(PROTECTED_PAGES) - pages}")
            by_name = {p["name"]: p["roles"] for p in matrix["permissions"]}
            check("payments:write -> reception/accountant, not doctor",
                  "RECEPTIONIST" in by_name.get("payments:write", [])
                  and "DOCTOR" not in by_name.get("payments:write", []),
                  str(by_name.get("payments:write")))
            access_html = request(base, "/ui/access", cookie=f"vvh_access_token={token}")[1].decode("utf-8", "replace")
            check("/ui/access renders the matrix", "Role access matrix" in access_html)

    # ------------------------------------------------------------------
    # Round 5: the chat upload / verify UI must actually be served, and the
    # document endpoints must answer for the right roles.
    print("\nChat document upload (round 5)")
    if tokens.get("PATIENT"):
        st, body, _ = request(base, "/ui/ai-chat", cookie=f"vvh_access_token={tokens['PATIENT']}")
        chat_html = body.decode("utf-8", "replace")
        for needle in ("fileInput", "onFilePicked", "uploadFile", "consultDoctor",
                       "Files in this chat", "Parchi / report / X-ray"):
            check(f"/ui/ai-chat ships {needle!r}", needle in chat_html)
        st, body, _ = request(base, "/ui/doctor", cookie=f"vvh_access_token={tokens['DOCTOR']}")
        doc_html = body.decode("utf-8", "replace")
        for needle in ("loadReview", "verifyFile", "Documents sent in chat", "Correct karein"):
            check(f"/ui/doctor ships {needle!r}", needle in doc_html)
        st, body, _ = request(base, "/api/v1/ai/files/review", token=tokens["DOCTOR"])
        check("GET /api/v1/ai/files/review (doctor)", st == 200, f"status={st} {body[:80]!r}")
        st, _, _ = request(base, "/api/v1/ai/files/review", token=tokens["PATIENT"])
        check("GET /api/v1/ai/files/review (patient -> 403)", st == 403, f"status={st}")

    print(f"\nRESULT: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
