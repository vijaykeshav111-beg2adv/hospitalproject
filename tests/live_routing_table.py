"""Live routing table: 58 real patient-style messages through the running API.

This is the end-to-end companion to ``tests/hybrid_fallback_check.py``. That
suite drives the engine in-process; this one talks to the real HTTP endpoint
``POST /api/v1/ai/chat`` so the FastAPI layer, the response model and the
database are all exercised exactly like the patient's browser does.

Run it with the server up:

    python -m uvicorn app.main:app --port 8000      (in one terminal)
    python -m tests.live_routing_table              (in another)

Every case uses a fresh session id, so conversation state can never leak from
one message into the next.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000/api/v1/ai/chat"

# (message, expected department fragment or None, expected safety level)
CASES: list[tuple[str, str | None, str]] = [
    # knees - every spelling a patient actually types
    ("gutno mein dard", "Orthopaedics", "NORMAL"),
    ("gutne mein dard", "Orthopaedics", "NORMAL"),
    ("gutnon me dard", "Orthopaedics", "NORMAL"),
    ("ghutnon mein dard", "Orthopaedics", "NORMAL"),
    ("ghutonne mein dard", "Orthopaedics", "NORMAL"),
    ("knee pain while climbing stairs", "Orthopaedics", "NORMAL"),
    ("ghutne me sujan aur dard", "Orthopaedics", "NORMAL"),
    # eyes
    ("meri aankhein laal ho rahi hain", "Ophthalmology", "NORMAL"),
    ("meri aankhon mein laalpan hai", "Ophthalmology", "NORMAL"),
    ("meri ammkein laal ho rhi hai", "Ophthalmology", "NORMAL"),
    ("eye red", "Ophthalmology", "NORMAL"),
    ("aankh se pani aa raha hai", "Ophthalmology", "NORMAL"),
    ("aankh dukh rahi hai", "Ophthalmology", "NORMAL"),
    ("nazar kam dikh raha hai", "Ophthalmology", "NORMAL"),
    ("subah uthne ke baad se meri aankh ajeeb si laal hai aur paani bhi aa raha hai",
     "Ophthalmology", "NORMAL"),
    # skin and hair
    ("mere baal jhd rhe hai", "Dermatology", "NORMAL"),
    ("baal bahut gir rahe hain", "Dermatology", "NORMAL"),
    ("khujli hori hai", "Dermatology", "NORMAL"),
    ("meri pith pe laal laal dane aa gye", "Dermatology", "NORMAL"),
    ("chamdi pe daane", "Dermatology", "NORMAL"),
    ("safed daane", "Dermatology", "NORMAL"),
    # fever and general complaints
    ("taav chadh gya", "General Medicine", "NORMAL"),
    ("sardi lag rahi hai", "General Medicine", "NORMAL"),
    ("khansi aur jukam hai", "General Medicine", "NORMAL"),
    ("bukhar aur khansi", "General Medicine", "NORMAL"),
    ("dast ho rahe hain", "General Medicine", "NORMAL"),
    ("ulti ho rahi hai", "General Medicine", "NORMAL"),
    ("peshab me jalan", "General Medicine", "NORMAL"),
    # bones and joints
    ("pair me kheechav aur chalne me dikkat", "Orthopaedics", "NORMAL"),
    ("pair me sujan", "Orthopaedics", "NORMAL"),
    ("kamar dard", "Orthopaedics", "NORMAL"),
    ("kamar me sujan", "Orthopaedics", "NORMAL"),
    ("kohni me dard", "Orthopaedics", "NORMAL"),
    # nerves
    ("chakkar aa rahe hain", "Neurology", "NORMAL"),
    ("haath sunn ho gaya", "Neurology", "NORMAL"),
    ("sir me dard", "Neurology", "NORMAL"),
    # children
    ("bachhe ko bukhar hai", "Paediatrics", "NORMAL"),
    ("bache ko dast", "Paediatrics", "NORMAL"),
    ("104 bukhar hai bachhe ko", "Paediatrics", "HIGH"),
    # women
    ("periods late ho gaye", "Obstetrics", "NORMAL"),
    ("periuds late hai", "Obstetrics", "NORMAL"),
    ("garbhvati hoon aur pet me dard", "Obstetrics", "NORMAL"),
    ("shaadi ke 2 saal baad bhi bachcha nahi hua", "Obstetrics", "NORMAL"),
    ("kokh me dard hai", "Obstetrics", "NORMAL"),
    # throat, teeth, ear
    ("gala kharab hai", "ENT", "NORMAL"),
    ("gale me kharash hai", "ENT", "NORMAL"),
    ("daant me dard", "ENT", "NORMAL"),
    ("kaan me dard", "ENT", "NORMAL"),
    # gut, heart, sleep
    ("pet mein jalan", "Gastroenterology", "NORMAL"),
    ("pet me gas", "Gastroenterology", "NORMAL"),
    ("seene mein dhak dhak", "Cardiology", "NORMAL"),
    ("seene me jalan", "Cardiology", "NORMAL"),
    ("neend nahi aati", "Psychiatry", "NORMAL"),
    # breathless is urgent, not an emergency: the doctor is alerted and the
    # patient is still routed (nobody is dropped at the alert)
    ("saans phool rahi hai", "Pulmonology", "HIGH"),
    # emergencies stay local, are never routed and are never guessed at
    ("saans nahi aa rahi", None, "EMERGENCY"),
    ("chest mein bahut tez dard ho rha", None, "EMERGENCY"),
    # language the local layer does not own -> the AI layer is asked first
    ("tavda lg gya", None, "NORMAL"),
    # ...and with no key the last-resort keyword search lands on the general
    # physician, which is the honest default for a vague whole-body complaint.
    # With GROQ_API_KEY set the AI's own reading decides this one instead.
    ("kal se body mein kuch problem hai", "General Medicine", "NORMAL"),
]


def chat(message: str) -> dict:
    body = json.dumps({"message": message, "session_id": f"live-{uuid.uuid4().hex[:8]}",
                       "channel": "WEB"}).encode()
    request = urllib.request.Request(BASE, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    print(f"{'':<3}{'MESSAGE':<58} | {'DEPARTMENT':<26} | {'ROUTE':<9} | SAFETY")
    print("-" * 118)
    passed = failed = 0
    problems: list[tuple[str, str | None, str]] = []
    for message, department, level in CASES:
        try:
            data = chat(message)
        except urllib.error.URLError as exc:
            print(f"\nCannot reach {BASE} - is uvicorn running?  ({exc})")
            return 2
        got = (data.get("specialty") or {}).get("specialty_name") or ""
        safety = (data.get("safety") or {}).get("level")
        route = ((data.get("understanding") or {}).get("route")) or "none"
        ok = ((department in got) if department else not got) and safety == level
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        if not ok:
            problems.append((message, department, got or "no department"))
        booking = (data.get("specialty") or {}).get("booking_specialty_name")
        shown = got + (f"  (book with {booking})" if booking else "")
        print(f"{'Y' if ok else 'N':<3}{message[:56]:<58} | {shown[:26]:<26} | {route:<9} | {safety}")
    print("-" * 118)
    print(f"RESULT: {passed} passed, {failed} failed")
    for message, expected, got in problems:
        print(f"  - {message!r}: expected {expected or 'no department'}, got {got}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
