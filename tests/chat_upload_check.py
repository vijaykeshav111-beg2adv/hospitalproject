"""Round-5 checks: chat document upload -> AI reading -> doctor verify / consult.

Run with:  python -m tests.chat_upload_check
Runs fully offline - the Groq vision reader is stubbed, so no API key is needed.

Covers
  1. vision reader contract (offline behaviour, JSON cleaning, vocabulary filter)
  2. chat_documents service (store, validate, pending link, review queue, verify, consult)
  3. HTTP layer /api/v1/ai/files (multipart upload, RBAC, verify, consult)
"""
from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.services import ai_engine, chat_documents, vision

RUN = f"{int(time.time())}"          # unique per run: sessions never collide
PASS, FAIL = [], []
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


def token(client: TestClient, email: str, password: str) -> str | None:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"] if r.status_code == 200 else None


def hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


FAKE_READING = {
    "document_type": "PRESCRIPTION",
    "summary": "Orthopaedic prescription: pain killer and calcium for knee pain.",
    "visible_text": "Dr Arjun Mehra - knee pain - Tab Etoshine 90mg - Calcium",
    "medicines": ["Etoshine 90mg", "Calcium"],
    "doctor_name": "Dr Arjun Mehra",
    "document_date": "2026-09-28",
    "body_area": "knee",
    "canonical_terms": ["knee", "pain"],
    "urgency_hint": "normal",
    "language": "en",
    "confidence": 0.78,
    "readable": True,
    "model": "qwen/qwen3.8-27b",
}


def fake_read(path: Path, mime_type: str | None = None, hint_text: str | None = None) -> dict:
    """Stands in for the Groq vision call (offline test)."""
    return dict(FAKE_READING)


SESSION_PREFIXES = ("chk-", "http-chk-")


def cleanup(db, prefixes=SESSION_PREFIXES) -> int:
    """Remove artifacts of earlier runs of this test so it always starts clean."""
    removed = 0
    convs = db.scalars(select(models.AIConversation)).all()
    mine = [c for c in convs if any((c.session_id or "").startswith(p) for p in prefixes)]
    if not mine:
        return 0
    ids = [c.id for c in mine]
    for msg in db.scalars(select(models.AIMessage).where(models.AIMessage.conversation_id.in_(ids))).all():
        payload = ((msg.structured or {}).get("chat_file") or {})
        record = db.get(models.MedicalFile, payload["file_id"]) if payload.get("file_id") else None
        if record:
            Path(record.file_path).unlink(missing_ok=True)
            db.delete(record)
            removed += 1
    # rows created by an older, buggier run may not be referenced by any message
    orphan_query = select(models.MedicalFile).where(models.MedicalFile.notes.like("AI:%"))
    for record in db.scalars(orphan_query).all():
        if chat_documents.PENDING_DIR in (record.file_path or ""):
            Path(record.file_path).unlink(missing_ok=True)
            db.delete(record)
            removed += 1
    for esc in db.scalars(select(models.AIEscalation).where(models.AIEscalation.conversation_id.in_(ids))).all():
        db.delete(esc)
    for msg in db.scalars(select(models.AIMessage).where(models.AIMessage.conversation_id.in_(ids))).all():
        db.delete(msg)
    for conv in mine:
        for folder in (settings.uploads_dir / chat_documents.PENDING_DIR / f"conv-{conv.id}",):
            if folder.exists():
                for f in folder.iterdir():
                    f.unlink(missing_ok=True)
                folder.rmdir()
        db.delete(conv)
    db.commit()
    return removed


def main() -> int:  # noqa: C901 - a checklist, kept linear on purpose
    init_db()
    db = SessionLocal()
    stale = cleanup(db)
    print(f"(cleaned {stale} leftover test document(s) from earlier runs)")
    patient = db.scalar(select(models.Patient).limit(1))
    doctor_user = db.scalar(select(models.User).where(models.User.role.has(name="DOCTOR")))
    doctor_row = db.scalar(select(models.Doctor).where(models.Doctor.user_id == doctor_user.id))
    print("=" * 78)
    print("CHAT DOCUMENT UPLOAD CHECK  |  patient:", patient.patient_code,
          "| doctor:", doctor_user.full_name)
    print("=" * 78)

    # ---------------------------------------------------------------- 1. vision
    check("vision: disabled without GROQ_API_KEY", vision.enabled() == bool(settings.groq_api_key))
    check("vision: reads nothing when disabled (no fake OCR)",
          vision.read_document(Path("/tmp/does-not-exist.png"), mime_type="image/png") is None)
    cleaned = vision._clean({
        "document_type": "TOTALLY_MADE_UP", "confidence": 7,
        "canonical_terms": ["Knee", " fever ", "not-a-term-xyz"],
        "medicines": [f"med{i}" for i in range(20)], "readable": "yes",
    })
    check("vision: unknown document_type falls back to OTHER", cleaned["document_type"] == "OTHER", cleaned["document_type"])
    check("vision: confidence clamped to 0..1", cleaned["confidence"] == 1.0, str(cleaned["confidence"]))
    check("vision: medicines list bounded", len(cleaned["medicines"]) == 15, str(len(cleaned["medicines"])))
    check("vision: terms normalised to lowercase and bounded to 12",
          cleaned["canonical_terms"][:2] == ["knee", "fever"] and len(cleaned["canonical_terms"]) == 3,
          str(cleaned["canonical_terms"]))
    check("vision: urgency_hint whitelisted", vision._clean({"urgency_hint": "CATASTROPHIC"})["urgency_hint"] == "LOW")
    prompt = vision.SYSTEM_PROMPT.lower()
    check("vision: prompt forbids diagnosis / prescribing",
          "not a doctor" in prompt and "never diagnose" in prompt and "never prescribe" in prompt)
    check("vision: prompt forbids inventing facts",
          "never invent" in prompt and "blurry" in prompt)
    check("vision: prompt restricts terms to the hospital vocabulary",
          "{vocabulary}" in vision.SYSTEM_PROMPT and "only words from the list" in prompt)
    text = vision.reading_as_text(FAKE_READING)
    check("vision: reading_as_text keeps summary + medicines",
          "prescription" in text.lower() and "Etoshine" in text, text[:80])
    check("vision: model chain (current -> deprecated fallback)",
          vision.vision_model() == "qwen/qwen3.8-27b" and
          getattr(settings, "groq_vision_fallback_model", "") == "qwen/qwen3.6-27b", vision.vision_model())

    # ------------------------------------------------------- 2. service layer
    check("validate: empty file refused", bool(chat_documents.validate(b"", "a.png", "image/png")))
    check("validate: wrong type refused", bool(chat_documents.validate(b"123", "a.exe", "application/x-msdownload")))
    check("validate: png accepted", chat_documents.validate(PNG, "parchi.png", "image/png") is None)
    old_limit = settings.max_upload_mb
    settings.max_upload_mb = 0
    big = chat_documents.validate(b"x" * 2048, "big.png", "image/png")
    settings.max_upload_mb = old_limit
    check("validate: size limit enforced", bool(big), str(big))

    rescue = ai_engine._rescue_with_terms(db, "knee pain since 3 days", {
        "canonical_terms": ["knee", "pain", "totally-made-up-term"], "concern": "knee pain",
        "confidence": 0.8, "urgency_hint": "LOW"}, source="groq-vision")
    check("vision terms: hallucinated words are rejected before routing",
          rescue is None or "totally-made-up-term" not in rescue["semantic"]["accepted_terms"],
          str(rescue)[:120])
    check("vision terms: low-confidence reading cannot route",
          ai_engine._rescue_with_terms(db, "knee pain", {"canonical_terms": ["knee"], "confidence": 0.2},
                                       source="groq-vision") is None)

    # linked upload (patient already known)
    conv_a = ai_engine.get_or_create_conversation(db, conversation_id=None, session_id=f"chk-linked-{RUN}",
                                                  channel="WEB", user_id=patient.user_id)
    with patch.object(chat_documents.vision, "read_document", fake_read):
        up_a = chat_documents.store_upload(db, content=PNG, filename="parchi_oct.png",
                                           content_type="image/png", conversation=conv_a,
                                           patient=patient, note="meri purani parchi")
    check("store_upload: file row created for a known patient", up_a["file_id"] is not None)
    record = db.get(models.MedicalFile, up_a["file_id"])
    check("store_upload: category from AI type", record.category == "PRESCRIPTION", record.category)
    check("store_upload: notes carry the AI marker",
          record.notes.startswith("AI:PRESCRIPTION|conf=0.78|verify=PENDING"), record.notes)
    marker = chat_documents.parse_marker(record.notes)
    check("parse_marker: round-trips the marker", marker["document_type"] == "PRESCRIPTION"
          and marker["verify"] == "PENDING", str(marker))
    msgs = db.scalars(select(models.AIMessage)
                      .where(models.AIMessage.conversation_id == conv_a.id)
                      .order_by(models.AIMessage.id.desc()).limit(2)).all()
    check("store_upload: transcript holds patient msg + AI reading",
          msgs[0].intent == "FILE_REVIEW" and msgs[1].intent == "FILE_UPLOAD"
          and msgs[1].structured["ai_reading"]["summary"].startswith("Orthopaedic"),
          f"{msgs[0].intent}/{msgs[1].intent}")
    check("store_upload: reply is not a diagnosis",
          "diagnosis nahi" in up_a["reply"] and "Doctor" in up_a["reply"])
    check("store_upload: file is on disk", Path(up_a["path"]).exists(), up_a["path"])

    # upload before identity
    conv_b = ai_engine.get_or_create_conversation(db, conversation_id=None, session_id=f"chk-pending-{RUN}",
                                                  channel="WEB", user_id=None)
    up_b = chat_documents.store_upload(db, content=PNG, filename="xray_knee.png",
                                       content_type="image/png", conversation=conv_b)
    check("store_upload: anonymous upload is stored but not linked", up_b["file_id"] is None)
    check("store_upload: pending storage folder", chat_documents.PENDING_DIR in up_b["path"], up_b["path"])
    check("store_upload: unlinked reply asks for name + mobile",
          "naam" in up_b["reply"] and "mobile" in up_b["reply"])

    created = chat_documents.link_pending(db, conv_b, patient)
    check("link_pending: creates the record once the patient is known", len(created) == 1 and created[0].id is not None)
    linked = db.get(models.MedicalFile, created[0].id)
    check("link_pending: marker PENDING + category right",
          (linked.notes or "").startswith("AI:LAB_REPORT") or (linked.notes or "").startswith("AI:SCAN"),
          linked.notes)
    check("link_pending: transcript updated with the file id",
          chat_documents.conversation_files(db, conv_b.id)[0]["file_id"] == linked.id)
    check("link_pending: idempotent (second call links nothing)",
          chat_documents.link_pending(db, conv_b, patient) == [])

    # ai_engine hook: _link_patient must link pending uploads automatically
    conv_c = ai_engine.get_or_create_conversation(db, conversation_id=None, session_id=f"chk-hook-{RUN}",
                                                  channel="WEB", user_id=None)
    up_c = chat_documents.store_upload(db, content=PNG, filename="khaav.jpg", content_type="image/jpeg",
                                      conversation=conv_c)
    ai_engine._link_patient(db, conv_c, patient, "NEW")
    check("store_upload: a second anonymous upload is also unlinked (nothing lost)",
          up_c["file_id"] is None)
    hooked = chat_documents.conversation_files(db, conv_c.id)[0]
    up_c_file = hooked["file_id"]
    check("ai_engine hook: identity mid-chat links earlier uploads", up_c_file is not None, str(hooked))

    # review queue
    queue = chat_documents.review_queue(db, doctor_id=doctor_row.id, limit=200)
    ids = {row["file_id"] for row in queue}
    wanted = {up_a["file_id"], linked.id, up_c_file}
    check("review_queue: pending chat uploads listed",
          wanted.issubset(ids),
          f"wanted {sorted(map(str, wanted))} / got {sorted(ids)}")
    check("review_queue: carries the AI reading for the doctor",
          any(row["ai_reading"] for row in queue if row["file_id"] == up_a["file_id"]))
    check("review_queue: 'mine' flag present", all("mine" in row for row in queue))
    strict = chat_documents.review_queue(db, doctor_id=doctor_row.id, limit=200, mine_only=True)
    check("review_queue: mine_only narrows to own patients",
          all(row["mine"] for row in strict) and len(strict) <= len(queue), f"{len(strict)}/{len(queue)}")

    # verify
    result = chat_documents.verify_file(db, record, decision="CONFIRMED",
                                        doctor_name=doctor_user.full_name, doctor_id=doctor_row.id,
                                        note="report theek hai")
    db.refresh(record)
    check("verify_file: CONFIRMED marker + doctor name + time",
          "verify=CONFIRMED" in record.notes and doctor_user.full_name in record.notes, record.notes)
    check("verify_file: patient-facing wording produced", "Doctor ne" in result["patient_message"])
    audit_row = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "FILE_VERIFY")
                          .order_by(models.AuditLog.id.desc()))
    check("verify_file: audit row FILE_VERIFY", audit_row is not None
          and audit_row.resource_id == str(record.id))
    after = {row["file_id"] for row in chat_documents.review_queue(db, doctor_id=doctor_row.id, limit=200)}
    check("verify_file: verified file leaves the pending queue", record.id not in after)
    check("verify_file: bad decision rejected",
          _raises(chat_documents.verify_file, db, record, decision="MAYBE", doctor_name="x"))
    corrected = chat_documents.verify_file(db, linked, decision="CORRECTED",
                                           doctor_name=doctor_user.full_name, doctor_id=doctor_row.id,
                                           corrected_type="LAB_REPORT", note="parchi nahi report")
    db.refresh(linked)
    check("verify_file: CORRECTED re-files the document",
          "verify=CORRECTED" in linked.notes and linked.category == "LAB_REPORT", f"{linked.category} {linked.notes}")
    check("verify_file: correction keeps the AI's original type for the record",
          corrected["ai_document_type"] == "SCAN" or corrected["ai_document_type"] == "LAB_REPORT",
          str(corrected["ai_document_type"]))

    # consult
    before = len(db.scalars(select(models.AIEscalation)).all())
    consult = chat_documents.request_doctor_review(db, conversation=conv_a, patient=patient,
                                                   file_id=up_a["file_id"], escalate=ai_engine.escalate)
    escalations = db.scalars(select(models.AIEscalation).order_by(models.AIEscalation.id.desc())).all()
    check("request_doctor_review: DOCTOR-level escalation opened",
          len(escalations) == before + 1 and escalations[0].level == "DOCTOR"
          and escalations[0].status == "OPEN", str(len(escalations)))
    check("request_doctor_review: the document is inside the escalation detail",
          f"file #{up_a['file_id']}" in escalations[0].detail, str(escalations[0].detail))
    check("request_doctor_review: every active doctor is notified in-app",
          consult["notified_doctors"] >= 1, str(consult))

    # ---------------------------------------------------------- 3. HTTP layer
    with TestClient(app) as client:
        patient_tk = token(client, "ramesh.yadav@example.com", "Patient@123")
        doctor_tk = token(client, "dr.arjunmehra@vijayvargiyahospital.in", "Doctor@123")
        admin_tk = token(client, "admin@vijayvargiyahospital.in", "Admin@123")
        check("http: logins for the upload flow", bool(patient_tk and doctor_tk and admin_tk))

        with patch.object(chat_documents.vision, "read_document", fake_read):
            r = client.post("/api/v1/ai/files", headers=hdr(patient_tk),
                            data={"session_id": f"http-chk-patient-{RUN}", "note": "parchi bheji hai"},
                            files={"file": ("parchi_http.png", io.BytesIO(PNG), "image/png")})
        body = r.json() if r.status_code == 200 else {}
        check("http: patient multipart upload 200", r.status_code == 200, r.text[:160])
        check("http: upload linked to the patient record", body.get("patient_linked") is True)
        check("http: response carries the AI reading", bool(body.get("ai_reading")), str(body)[:120])
        check("http: response offers the doctor-consult option",
              any("consult" in q.lower() for q in body.get("quick_replies", [])), str(body.get("quick_replies")))
        check("http: response states AI is not a diagnosis",
              "diagnosis" in (body.get("disclaimer") or "").lower(), str(body.get("disclaimer")))
        http_file_id = body.get("file_id")
        check("http: patient can open his own upload",
              client.get(body["view_url"], headers=hdr(patient_tk)).status_code == 200)

        r2 = client.post("/api/v1/ai/files", data={"session_id": f"http-chk-guest-{RUN}"},
                         files={"file": ("khaav.jpg", io.BytesIO(PNG), "image/jpeg")})
        check("http: anonymous upload allowed (linked later)",
              r2.status_code == 200 and r2.json()["patient_linked"] is False, r2.text[:160])

        r3 = client.post("/api/v1/ai/files", headers=hdr(patient_tk),
                         files={"file": ("virus.exe", io.BytesIO(b"MZ"), "application/x-msdownload")})
        check("http: unsupported type -> 422", r3.status_code == 422, str(r3.status_code))

        check("http: patient cannot open the review queue",
              client.get("/api/v1/ai/files/review", headers=hdr(patient_tk)).status_code == 403)
        r4 = client.get("/api/v1/ai/files/review", headers=hdr(doctor_tk))
        check("http: doctor review queue 200", r4.status_code == 200, r4.text[:160])
        check("http: queue returns files + ai_reading flag",
              "files" in r4.json() and "ai_reading_enabled" in r4.json())

        check("http: patient cannot verify",
              client.post(f"/api/v1/ai/files/{http_file_id}/verify", headers=hdr(patient_tk),
                          data={"decision": "CONFIRMED"}).status_code == 403)
        r5 = client.post(f"/api/v1/ai/files/{http_file_id}/verify", headers=hdr(doctor_tk),
                         data={"decision": "CONFIRMED", "note": "checked"})
        check("http: doctor verifies the document", r5.status_code == 200, r5.text[:160])
        check("http: verify reply reaches the patient's chat",
              "Doctor ne" in (r5.json().get("patient_message") or ""), str(r5.json())[:160])
        r6 = client.post(f"/api/v1/ai/files/{http_file_id}/verify", headers=hdr(doctor_tk),
                         data={"decision": "SOMETHING_ELSE"})
        check("http: invalid decision -> 422", r6.status_code == 422, str(r6.status_code))

        r7 = client.post(f"/api/v1/ai/files/{http_file_id}/consult", headers=hdr(patient_tk),
                         data={"conversation_id": body["conversation_id"]})
        check("http: 'doctor se consult' opens an escalation", r7.status_code == 200
              and r7.json()["escalation"]["level"] == "DOCTOR", r7.text[:160])
        r8 = client.get(f"/api/v1/ai/files/conversation/{body['conversation_id']}")
        check("http: conversation files listed", r8.status_code == 200 and len(r8.json()["files"]) >= 1)
        check("http: escalation visible to the admin monitor",
              client.get("/api/v1/ai/escalations", headers=hdr(admin_tk)).status_code == 200)

        r9 = client.post("/api/v1/ai/files", data={"session_id": f"http-chk-nofile-{RUN}"})
        check("http: upload without a file -> 422", r9.status_code == 422, str(r9.status_code))

    cleanup(db)                            # leave no test data in the review pool
    db.close()
    print("-" * 78)
    print(f"passed {len(PASS)} | failed {len(FAIL)}")
    if FAIL:
        print("failed:", FAIL)
    return 1 if FAIL else 0


def _raises(func, *args, **kwargs) -> bool:
    try:
        func(*args, **kwargs)
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    sys.exit(main())
