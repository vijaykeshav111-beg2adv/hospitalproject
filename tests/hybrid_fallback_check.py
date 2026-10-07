"""Hybrid semantic fallback checks: local Hindi/Hinglish understanding, zero-token
fast paths, the new-concern state reset, and the Groq fallback boundary.

Run with:  python -m tests.hybrid_fallback_check
(Uses the same database the app uses; no Groq key is required - the fallback is
exercised with a stub so the boundary can be verified offline.)
"""
from __future__ import annotations

import sys
import uuid

from app.database import SessionLocal
from app.schemas import AIChatRequest
from app.services import ai_engine, groq_engine, medical_language
from app.services.ai_tools import ToolContext

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


def section(title: str):
    print("\n" + "-" * 78)
    print(title)
    print("-" * 78)


def main() -> int:
    db = SessionLocal()
    run = uuid.uuid4().hex[:8]

    def ask(text: str, session: str, **kwargs) -> dict:
        payload = AIChatRequest(message=text, session_id=f"{session}-{run}", channel="WEB", **kwargs)
        return ai_engine.handle_message(db, payload)

    # ------------------------------------------------------------------
    section("1. local dictionary: Hindi / Hinglish / misspellings -> canonical terms")
    cases = {
        "bukhar 3 din se h": "fever",
        "taav chadh gya": "fever",
        "ghutne mein dard": "knee",
        "kamar dard": "back pain",
        "meri pith pe laal laal dane aa gye": "rash",
        "khujli ho rahi hai": "itching",
        "sar dard aur chakkar": "headache",
        "pet mein jalan": "acidity",
        # "saans phoolna" is mapped to the breathing family (Pulmonology covers both)

        "बुखार और खांसी": "fever",
        "dast ho rahe hain": "loose motion",
        "ulti ho rahi hai": "vomiting",
    }
    for text, expected in cases.items():
        result = medical_language.normalise_medical_text(text)
        check(f"local: {text!r} -> {expected!r}", expected in result["canonicals"],
              f"got {result['canonicals']}")
    breath = medical_language.normalise_medical_text("saans phool rahi hai")["canonicals"]
    check("local: 'saans phool rahi hai' -> breathing/breathless",
          any(t in breath for t in ("breathing", "breathless")), f"got {breath}")

    check("unrecognised slang is NOT understood (never guessed)",
          medical_language.normalise_medical_text("tavda lg gya")["understood"] is False)
    check("'kan' (ear) does not match inside 'kamar' (back)",
          "ear" in medical_language.normalise_medical_text("kaan mein dard")["canonicals"]
          and "ear" not in medical_language.normalise_medical_text("kamar dard")["canonicals"])

    # ------------------------------------------------------------------
    section("2. emergency safety is local, first, and language-aware")
    emergencies = [
        "chest mein bahut tez dard ho rha",
        "seene mein dard ho raha hai",
        "saans nahi aa rahi",
        "wo behosh ho gaya",
        "bahut khoon beh raha hai",
        "दिल का दौरा",
        "accident ho gaya",
    ]
    for text in emergencies:
        check(f"EMERGENCY: {text!r}", ai_engine.scan_safety(text)["level"] == "EMERGENCY",
              str(ai_engine.scan_safety(text)))
    for text in ["kamar dard", "bukhar 3 din se h", "taav chadh gya", "1", "yes"]:
        check(f"not an emergency: {text!r}", ai_engine.scan_safety(text)["level"] != "EMERGENCY")

    # ------------------------------------------------------------------
    section("3. routing: regional complaints reach the right department")
    routing = {
        "knee pain while climbing stairs": "Orthopaedics",
        "ghutne mein dard": "Orthopaedics",
        "meri pith pe laal laal dane aa gye": "Dermatology",
        "taav chadh gya": "General Medicine",
        "bukhar 3 din se h": "General Medicine",
    }
    for text, expected in routing.items():
        db.rollback()
        response = ask(text, f"hf-route-{abs(hash(text)) % 9999}")
        got = (response["specialty"] or {}).get("specialty_name")
        check(f"route {text!r} -> {expected}", got == expected, f"got {got}")

    # ------------------------------------------------------------------
    section("4. unknown concerns ask instead of guessing")
    db.rollback()
    response = ask("tavda lg gya", "hf-unknown")
    check("unknown slang requests clarification",
          (response.get("understanding") or {}).get("action") == "clarification_requested",
          str(response.get("understanding")))
    check("unknown slang invents no specialty", response["specialty"] is None and response["doctors"] == [])
    check("unknown slang mentions the option of calling 108", "108" in response["reply"])

    # ------------------------------------------------------------------
    section("5. the removed '6+ words = medical problem' rule")
    check("looks_like_concern() no longer accepts long sentences",
          ai_engine.looks_like_concern("no please show me the other available slots instead") is False)
    check("is_new_medical_concern('bukhar 3 din se h') is True",
          ai_engine.is_new_medical_concern("bukhar 3 din se h") is True)
    check("is_new_medical_concern('cancel appointment') is False",
          ai_engine.is_new_medical_concern("cancel appointment") is False)
    check("is_new_medical_concern('yes') is False", ai_engine.is_new_medical_concern("yes") is False)
    check("a Hindi symptom is never stored as a patient name",
          ai_engine.plausible_name("meri pith pe laal laal dane") is False)
    check("a real name is still accepted", ai_engine.plausible_name("Sunil Kumar") is True)

    # ------------------------------------------------------------------
    section("6. a new concern interrupts the doctor-selection state")
    db.rollback()
    session = "hf-interrupt"
    phone = "9" + uuid.uuid4().hex[:9].translate(str.maketrans("abcdef", "012345"))
    phone = (phone + "0000000000")[:10]
    ask("knee pain while climbing stairs", session)
    ask("Sunil Kumar", session)
    third = ask(phone, session)
    check("at doctor choice for Orthopaedics",
          third["stage"] == "AWAIT_DOCTOR_CHOICE"
          and (third["specialty"] or {}).get("specialty_name") == "Orthopaedics",
          f"stage={third['stage']} specialty={third['specialty']}")
    fourth = ask("meri pith pe laal laal dane aa gye aur khujli hori", session)
    check("new concern re-routes to Dermatology",
          (fourth["specialty"] or {}).get("specialty_name") == "Dermatology",
          f"got {fourth['specialty']}")
    check("stale Orthopaedics doctors are cleared",
          all("Ortho" not in (d.get("specialty") or "") for d in fourth["doctors"]))
    fifth = ask("1", session)
    check("the patient is not trapped (can still pick a doctor)",
          fifth["stage"] == "AWAIT_SLOT_CHOICE" and len(fifth["slots"]) >= 1, f"stage={fifth['stage']}")

    # ------------------------------------------------------------------
    section("7. full booking flow still works end to end (no regression)")
    db.rollback()
    session = "hf-booking"
    phone = "9" + uuid.uuid4().hex[:9].translate(str.maketrans("abcdef", "012345"))
    phone = (phone + "0000000000")[:10]
    ask("knee pain while climbing stairs", session)
    ask("Meena Sharma", session)
    ask(phone, session)
    ask("1", session)
    held = ask("1", session)
    check("slot held -> AWAIT_CONFIRMATION", held["stage"] == "AWAIT_CONFIRMATION", f"stage={held['stage']}")
    booked = ask("yes", session)
    check("booking confirmed",
          booked["stage"] == "POST_BOOK" and bool((booked.get("appointment") or {}).get("appointment_code")),
          str(booked.get("appointment"))[:120])

    # ------------------------------------------------------------------
    section("8. Groq fallback boundary (stubbed client - verifies the logic offline)")
    original_available = groq_engine.is_available
    original_understand = groq_engine.semantic_understand
    original_analyze = groq_engine.analyze_message
    original_polish = groq_engine.polish_reply
    from app.config import settings
    original_mode = type(settings).ai_mode
    try:
        type(settings).ai_mode = property(lambda self: "hybrid")
        groq_engine.is_available = lambda: True
        groq_engine.polish_reply = lambda *a, **k: None
        calls = {"n": 0}

        def fake_semantic(text: str):
            calls["n"] += 1
            return {"concern": "knee problem", "body_area": "knee", "symptoms": ["pain"],
                    "canonical_terms": ["knee", "pain"], "confidence": 0.8}

        groq_engine.semantic_understand = fake_semantic
        groq_engine.analyze_message = fake_semantic
        db.rollback()
        response = ask("tavda lg gya", "hf-semantic")
        check("unknown slang routed via the semantic fallback",
              response["specialty"] is not None and calls["n"] == 1,
              f"specialty={response['specialty']} groq_calls={calls['n']}")

        before = calls["n"]
        for i, text in enumerate(["bukhar 3 din se h", "ghutne mein dard", "gutno me dard",
                                  "meri aankhein laal ho rahi hain", "mere baal jhd rhe hai",
                                  "knee pain while climbing stairs", "1", "yes", "cancel appointment",
                                  "seene mein bahut tez dard ho rha"]):
            db.rollback()
            ask(text, f"hf-zero-{i}")
        check("0 Groq calls for locally-understood / operational messages", calls["n"] == before,
              f"before={before} after={calls['n']}")

        # ...but a NATURAL sentence the dictionary cannot read must go to the AI
        before = calls["n"]
        db.rollback()
        natural = ask("kal se body mein kuch problem hai", "hf-natural")
        check("a natural sentence the dictionary cannot read DOES go to Groq",
              calls["n"] == before + 1, f"calls={calls['n'] - before} route={(natural.get('understanding') or {}).get('route')}")

        groq_engine.semantic_understand = lambda text: {"concern": None, "body_area": None, "symptoms": [],
                                                        "canonical_terms": [], "confidence": 0.2}
        groq_engine.analyze_message = groq_engine.semantic_understand
        db.rollback()
        low = ask("tavda lg gya", "hf-low")
        check("low-confidence interpretation asks instead of guessing",
              low["specialty"] is None and "ask rather than guess" in low["reply"])

        invalid = lambda text: {"concern": "cancer", "body_area": "blood", "symptoms": ["tumour"],
                                "canonical_terms": ["cancer", "tumour"], "confidence": 0.99}
        groq_engine.semantic_understand = invalid
        groq_engine.analyze_message = invalid
        db.rollback()
        bad = ask("tavda lg gya", "hf-invalid")
        check("terms outside the hospital vocabulary are rejected (no diagnosis)",
              bad["specialty"] is None and "ask rather than guess" in bad["reply"])

        def broken(text: str):
            raise RuntimeError("groq outage")

        groq_engine.semantic_understand = broken
        groq_engine.analyze_message = broken
        db.rollback()
        try:
            survived = ask("tavda lg gya", "hf-outage")
            check("a Groq outage never breaks the front desk",
                  survived["specialty"] is None and "ask rather than guess" in survived["reply"])
        except Exception as exc:  # noqa: BLE001
            check("a Groq outage never breaks the front desk", False, repr(exc))
    finally:
        groq_engine.is_available = original_available
        groq_engine.semantic_understand = original_understand
        groq_engine.analyze_message = original_analyze
        groq_engine.polish_reply = original_polish
        type(settings).ai_mode = original_mode


    # ------------------------------------------------------------------
    section("9b. vocabulary expansion: the sentence variants patients really type")
    variants = {
        # knee - the variants that used to fall through to clarification
        "gutno mein dard": "knee",
        "gutne mein dard": "knee",
        "gutnon me dard": "knee",
        "ghutnon mein dard": "knee",
        "ghutne ka dard": "knee",
        "ghutne me sujan": "knee",
        # eye - plural / oblique / misspelled forms
        "meri aankhein laal ho rahi hain": "redness in eye",
        "meri aankhon mein laalpan hai": "redness in eye",
        "meri ammkein laal ho rhi hai": "redness in eye",
        "eye red": "redness in eye",
        "aankh se pani aa raha hai": "eye",
        "nazar kam dikh raha hai": "vision",
        # hair
        "mere baal jhd rhe hai": "hair fall",
        "baal bahut gir rahe hain": "hair fall",
        "baal toot rahe hain": "hair fall",
        "hairloss": "hair fall",
        # cold / throat / fever
        "sardi lag rahi hai": "cold",
        "naak beh rahi hai": "nose",
        "gala kharab": "throat",
        "zukam ho gaya": "cold",
        "taav chadh gya": "fever",
        # pain in its many spellings
        "sir dard": "headache",
        "sar dukh": "headache",
        "kamar dard": "back pain",
        "peeth me dard": "back pain",
        "pet mein jalan": "acidity",
        "pet me marod": "pet dard",
        # ortho / general natural phrasing
        "pair me kheechav aur chalne me dikkat": "leg",
        "kandhe me dard": "shoulder",
        "edi me dard": "heel",
        "daant me dard": "toothache",
    }
    for text, expected in variants.items():
        result = medical_language.normalise_medical_text(text)
        check(f"vocab: {text!r} -> {expected}", expected in result["canonicals"],
              f"got {result['canonicals']}")

    check("unknown slang is still NOT understood (never guessed)",
          medical_language.normalise_medical_text("tavda lg gya")["understood"] is False)

    # ------------------------------------------------------------------
    section("9c. typo tolerance: any one-letter mistake still matches")
    typos = {
        "ghutonne mein dard": "knee",          # extra letter
        "ghutne me drad": "pain",              # swapped letters
        "aankhen laal hai": "redness in eye",  # plural spelling
        "ammkein laal": "redness in eye",      # missing letter
        "bhukhar hai": "fever",
        "khujli horhi hai": "itching",
        "peit me dard": "pet dard",
        "gardaan me dard": "neck",
    }
    for text, expected in typos.items():
        result = medical_language.normalise_medical_text(text)
        # the body+symptom combiner may return the phrase form ("neck pain")
        ok = expected in result["canonicals"] or any(expected in c for c in result["canonicals"])
        check(f"typo: {text!r} -> {expected}", ok, f"got {result['canonicals']}")

    check("fuzzy matching stays out of the safety path",
          medical_language.has_emergency_term("ghutonne mein dard") is None)

    # ------------------------------------------------------------------
    section("9d. who understands what: local vs the AI (the balance)")
    decisions = {
        "1": "local", "yes": "local", "cancel appointment": "local",
        "ghutne mein dard": "local", "gutno me dard": "local",
        "meri aankhein laal ho rahi hain": "local", "mere baal jhd rhe hai": "local",
        "subah uthne ke baad se meri aankh ajeeb si laal hai aur paani bhi aa raha hai": "local",
        "taav chadh gya": "local",
        "kal se body mein kuch problem hai": "semantic",
        "pata nahi kya ho raha hai": "semantic",
        "tavda lg gya": "semantic",
    }
    for text, expected in decisions.items():
        local = medical_language.normalise_medical_text(text)
        mode = ai_engine.routing_mode(local, text)
        check(f"route {text[:44]!r} -> {expected}", mode == expected, f"got {mode}")

    sentence = "subah uthne ke baad se meri aankh ajeeb si laal hai aur paani bhi aa raha hai"
    db.rollback()
    sentence_row = ask(sentence, "hf-sentence")
    check("a long natural Hinglish sentence is routed without the AI",
          (sentence_row["specialty"] or {}).get("specialty_name") == "Ophthalmology",
          f"got {(sentence_row['specialty'] or {}).get('specialty_name')}")
    check("...and it reports that the local layer understood it",
          (sentence_row.get("understanding") or {}).get("route") == "local",
          str(sentence_row.get("understanding")))
    check("...with route/source fields the staff portal can read",
          {"route", "source", "language", "confidence", "local_terms"} <= set(sentence_row.get("understanding") or {}),
          str(sentence_row.get("understanding")))


    # ------------------------------------------------------------------
    section("9e. 45 real patient-style messages through the whole engine")
    # The same table is run against the live HTTP endpoint by hand; here it goes
    # through extract_concern -> route_to_specialty in one process, so the whole
    # pipeline (vocabulary -> canonicals -> keyword table -> body area) can be
    # checked without starting the server.
    routing_table = [
        # bad knees, every spelling a patient actually types
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
        ("aankhon se dhua dikhta hai", "Ophthalmology", "NORMAL"),
        ("subah uthne ke baad se meri aankh ajeeb si laal hai aur paani bhi aa raha hai",
         "Ophthalmology", "NORMAL"),
        # skin and hair
        ("mere baal jhd rhe hai", "Dermatology", "NORMAL"),
        ("baal bahut gir rahe hain", "Dermatology", "NORMAL"),
        ("khujli hori hai", "Dermatology", "NORMAL"),
        ("meri pith pe laal laal dane aa gye", "Dermatology", "NORMAL"),
        ("chamdi pe daane", "Dermatology", "NORMAL"),
        ("safed daane", "Dermatology", "NORMAL"),
        # fever / general
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
        # throat / teeth / ear / gut / heart / sleep / chest
        ("gala kharab hai", "ENT", "NORMAL"),
        ("gale me kharash hai", "ENT", "NORMAL"),
        ("daant me dard", "ENT", "NORMAL"),
        ("kaan me dard", "ENT", "NORMAL"),
        ("pet mein jalan", "Gastroenterology", "NORMAL"),
        ("pet me gas", "Gastroenterology", "NORMAL"),
        ("seene mein dhak dhak", "Cardiology", "NORMAL"),
        ("seene me jalan", "Cardiology", "NORMAL"),
        ("neend nahi aati", "Psychiatry", "NORMAL"),
        # breathless -> urgent, not an emergency: a doctor is alerted and booked
        ("saans phool rahi hai", "Pulmonology", "HIGH"),
        # emergencies stay local, never routed, and never guessed
        ("saans nahi aa rahi", None, "EMERGENCY"),
        ("chest mein bahut tez dard ho rha", None, "EMERGENCY"),
        # not understood locally -> the AI layer is asked first
        ("tavda lg gya", None, "NORMAL"),
        # ...and with no key the last-resort keyword search lands on the general
        # physician, which is the honest default for a vague whole-body complaint.
        # With a Groq key, the AI's own reading of the sentence decides this one.
        ("kal se body mein kuch problem hai", "General Medicine", "NORMAL"),
    ]
    routing_ctx = ToolContext(db, caller="hybrid-check")
    for text, department, level in routing_table:
        safety = ai_engine.scan_safety(text)
        if department:
            concern = ai_engine.extract_concern(db, text)
            specialty = ai_engine.route_to_specialty(routing_ctx, concern, None)
            name = (specialty or {}).get("specialty_name") or ""
            ok = department in name and safety["level"] == level
            got = f"{name or 'no department'} / {safety['level']}"
        else:
            # an emergency, or language the local layer does not own: the handler
            # must not name a department, and the AI layer decides - so this one
            # is driven through handle_message exactly as the patient would.
            db.rollback()
            row = ask(text, "hf-table-" + uuid.uuid4().hex[:6])
            name = (row["specialty"] or {}).get("specialty_name") or ""
            ok = not name and safety["level"] == level
            got = f"{name or 'no department'} / {safety['level']}"
        check(f"route {text[:44]!r} -> {department or 'no department'} [{level}]", ok, f"got {got}")

    db.rollback()

    # ------------------------------------------------------------------
    section("9. without a key the fallback stays off (AI_MODE=local)")
    check("groq_engine.is_available() is False with no GROQ_API_KEY", groq_engine.is_available() is False)
    db.rollback()
    offline = ask("tavda lg gya", "hf-offline")
    check("no key -> clarification, not a crash",
          offline["specialty"] is None and "ask rather than guess" in offline["reply"])

    db.close()

    print("\n" + "=" * 78)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failed checks:")
        for name in FAIL:
            print("  -", name)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
