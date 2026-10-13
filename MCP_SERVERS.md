# MCP Layer — 10 MCP Servers + Grounded Chatbot + Dashboard

Vijay Vargiya Group of Hospitals ke liye **Model Context Protocol (MCP)**
integration. Ye layer existing FastAPI + MySQL project ke upar baithta hai aur
teen cheezein deliver karta hai:

1. **10 MCP servers** (FastMCP) — hospital ke poore domain par, RBAC ke saath
2. **MCP chatbot** (`/ui/mcp-chat`) — jo jawab **sirf** in MCP servers/DB se deta hai
3. **MCP Control Center dashboard** (`/ui/mcp`) — servers, RBAC matrix, tool playground

```
Hallucination control:   db not available  ->  not generate  ->  no
RBAC contract:           10 MCP servers    ->  5 users + 3 admin + 2 system
```

---

## 1. The 10 servers (5 USER + 3 ADMIN + 2 SYSTEM)

| # | Server | Tier | Tools | Endpoint (app me) |
|---|--------|------|-------|-------------------|
| 1 | **patients** — profiles, search, timeline, summary | USER | 5 | `/mcp/servers/patients/mcp` |
| 2 | **doctors** — doctors, specialties, schedule, availability | USER | 6 | `/mcp/servers/doctors/mcp` |
| 3 | **appointments** — slots, book/cancel/reschedule, queue | USER | 7 | `/mcp/servers/appointments/mcp` |
| 4 | **prescriptions** — prescriptions, medicines, records, consultations | USER | 5 | `/mcp/servers/prescriptions/mcp` |
| 5 | **billing** — invoices, payments, outstanding, refunds | USER | 5 | `/mcp/servers/billing/mcp` |
| 6 | **notifications** — WhatsApp/Email/In-App queue + stats | ADMIN | 4 | `/mcp/servers/notifications/mcp` |
| 7 | **reviews** — reviews, moderation, doctor ratings | ADMIN | 4 | `/mcp/servers/reviews/mcp` |
| 8 | **analytics** — revenue, appointments, doctor load, growth, AI monitor, KPI | ADMIN | 6 | `/mcp/servers/analytics/mcp` |
| 9 | **security** — users, roles matrix, sessions, audit, login activity | SYSTEM | 6 | `/mcp/servers/security/mcp` |
| 10 | **ops** — health, DB probe, job runs, settings, mcp_status | SYSTEM | 5 | `/mcp/servers/ops/mcp` |

**Total: 10 servers / 53 tools.** Har tool ek typed FastMCP tool hai — MCP
clients (Claude Desktop, Cursor, VS Code, custom agents) ise seedha call kar
sakte hain.

---

## 2. RBAC — Role → Access

| Access tier | Servers (count) | Hospital roles | Level |
|-------------|-----------------|----------------|-------|
| **USER** | patients, doctors, appointments, prescriptions, billing (**5**) | PATIENT, DOCTOR, RECEPTIONIST, ACCOUNTANT | 10 |
| **ADMIN** | notifications, reviews, analytics (**3**) | ADMIN | 20 |
| **SYSTEM** | security, ops (**2**) | SUPER_ADMIN, service key | 30 |

* Caller ka tier uske **JWT `role` claim** se banta hai (wahi token jo
  `/api/v1/auth/login` deta hai).
* Automation ke liye `X-MCP-API-Key: <MCP_SYSTEM_API_KEY>` → SYSTEM tier.
* Row-level scoping: ek **PATIENT** sirf apne hi rows padh sakta hai (doosre
  ka `patient_id` maangne par grounded `"answer":"no"`).
* Tier se upar ka server call karne par tool
  `{"answer":"no","reason":"rbac_denied"}` return karta hai — data kabhi nahi.

---

## 3. Hallucination control (guard policy)

Har tool ka jawab ek **envelope** me aata hai:

```json
{
  "ok": true,
  "grounded": true,
  "generated": true,
  "source": "mysql",
  "server": "patients",
  "tool": "get_patient",
  "data": { "... sirf DB rows ..." }
}
```

| Situation | Envelope | Meaning |
|-----------|----------|---------|
| DB up, rows mile | `grounded=true, generated=true, data=…` | Answer bolo (sources ke saath) |
| DB up, kuch nahi mila | `grounded=true, answer="no", data=null` | Grounded negative — "no record found" |
| **DB down / query fail** | `grounded=false, generated=false, answer="no", reason="db_unavailable"` | **Not generate** — chatbot sirf refusal dikhata hai |
| Tool crash | `generated=false, answer="no", reason="tool_error"` | Same — kabhi guess nahi |
| RBAC denied | `answer="no", reason="rbac_denied"` | Access denied (text safe hai, data nahi) |

Chatbot (`mcp_layer/chatbot.py`) inhi envelopes se text banata hai. Agar kisi
bhi envelope me `generated=false` hai to poora reply refusal hota hai —
**partial answer bhi nahi**. Har grounded reply me `sources: ["server.tool"]`
hota hai.

Medical safety (project AI policy ke same): diagnosis / treatment / prescription
requests → refusal; emergency phrases (chest pain, bleeding…) → 108 guidance.

---

## 4. Pages & links

| Link | Kya hai |
|------|---------|
| `/ui/mcp` | **MCP Control Center** — 10 server cards, RBAC matrix (5/3/2), DB guard banner, tool playground, live endpoints |
| `/ui/mcp-chat` | **MCP chatbot** — grounded chat UI (sources, refusals, tier info) |
| `/mcp` | Gateway index — saare 10 MCP endpoints ek page par |
| `/mcp/api/status` | Live JSON: DB guard, RBAC matrix, caller tier |
| `/mcp/api/tools/call` | REST se koi bhi tool call (same guarded pipeline) |
| `/mcp/api/chat` | Chatbot REST API |
| `/mcp/docs` | Gateway OpenAPI docs |
| `:8101`..`:8110` | Har server standalone bhi chal sakta hai (`python -m mcp_layer.servers.<key>`) |

### MCP client connect kaise karein (Claude Desktop example)

```json
{
  "mcpServers": {
    "vvh-patients": {
      "url": "http://127.0.0.1:8000/mcp/servers/patients/mcp",
      "headers": { "Authorization": "Bearer <hospital JWT>" }
    },
    "vvh-appointments": {
      "url": "http://127.0.0.1:8000/mcp/servers/appointments/mcp",
      "headers": { "Authorization": "Bearer <hospital JWT>" }
    }
  }
}
```

---

## 5. Kaise chalayein

```bash
# normal hospital app (MCP gateway /mcp par auto-mount hota hai)
python3 run.py

# ya sirf MCP gateway standalone
python3 -m mcp_layer.gateway            # http://127.0.0.1:8100/mcp

# ya ek server standalone
python3 -m mcp_layer.servers.patients   # http://127.0.0.1:8101/mcp

# ya backup bundle (chatbot + MCP servers integrated)
python3 backup/chatbot_mcp/run.py       # http://127.0.0.1:8200
```

Tests:

```bash
ALLOW_DB_DOWNTIME=1 python3 -m pytest tests/mcp_layer_test.py -q
```

`.env` knobs (optional):

```
MCP_PORT=8100                  # gateway port (standalone mode)
MCP_FIRST_PORT=8101            # standalone ports 8101..8110
MCP_SYSTEM_API_KEY=...         # service callers -> SYSTEM tier
MCP_ALLOW_DEMO_ROLES=false     # classroom demo only!
ALLOW_DB_DOWNTIME=1            # gateway boots during outage -> serves "no" refusals
```

> **Note:** `ALLOW_DB_DOWNTIME=1` sirf MCP gateway/chatbot ke liye hai taaki DB
> outage ke dauran bhi refusal path (`"no"`) serve ho sake. Hospital app ka
> default fail-fast behaviour unchanged hai.

---

## 6. Code map

```
mcp_layer/
├── config.py          ports, API key, guard settings
├── guard.py           hallucination control: session_scope, envelopes, run_guarded
├── rbac.py            tiers (USER/ADMIN/SYSTEM), JWT/API-key principals
├── registry.py        ServerSpec catalogue (the 10 servers) + 5/3/2 matrix
├── invoke.py          single enforcement path: RBAC -> guard -> tool -> audit
├── chatbot.py         grounded chatbot engine (deterministic planner)
├── gateway.py         FastAPI app: 10 FastMCP mounts + REST + chat API
└── servers/
    ├── base.py        FastMCP factory + typed wrappers (headers -> principal)
    ├── patients.py    doctors.py  appointments.py  prescriptions.py  billing.py
    ├── notifications.py  reviews.py  analytics.py
    └── security.py    ops.py

backup/chatbot_mcp/    standalone chatbot+MCP bundle (port 8200)
app/templates/
├── mcp_dashboard.html /ui/mcp      (new page)
└── mcp_chat.html       /ui/mcp-chat (new page)
```

### Enforcement path (ek hi jagah)

```
MCP wire / REST / chatbot
        │
        ▼
mcp_layer.invoke.invoke(principal, server, tool, args)
        │  1. registry lookup
        │  2. RBAC tier check  (USER<ADMIN<SYSTEM)
        │  3. guard.run_guarded(tool_fn)   ← DB down? -> refuse "no"
        │  4. audit (ai_tool_calls)
        ▼
envelope {ok, grounded, generated, answer, source, data}
```

---

## 7. Security notes

* JWT verify `JWT_SECRET` se hota hai (app ke saath shared) — DB down ho tab
  bhi RBAC kaam karti hai.
* `MCP_ALLOW_DEMO_ROLES` production me **hamesha false** rakhein.
* `MCP_SYSTEM_API_KEY` ko `.env` me rakhein, repo me commit na karein.
* Write tools (`book_appointment`, `cancel_appointment`, `send_notification`,
  `moderate_review`, `reschedule_appointment`) audit-logged hain aur owner/staff
  scoping follow karte hain.
* Medical safety policy chatbot me enforced hai — koi diagnosis/prescription
  generation nahi.
