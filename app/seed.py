"""Seed the database with roles, permissions, masters, demo staff, doctors and patients.

Run directly:   python -m app.seed
Runs automatically on startup when the users table is empty (MySQL mode).
"""
from __future__ import annotations

import logging
import sys
import random
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .database import SessionLocal, init_db
from .rbac import PERMISSIONS, ROLE_PERMISSIONS, ROLES
from .security import hash_password, utcnow
from .services import billing, slots as slot_service

log = logging.getLogger("vvh.seed")

random.seed(7)

SPECIALTIES = [
    ("GENMED", "General Medicine", "Fever, infections, diabetes, blood pressure and general complaints", 500,
     ["fever", "cold", "cough", "throat", "flu", "weakness", "body ache", "typhoid", "infection",
      "diabetes", "sugar", "blood pressure", "bp", "fatigue", "vomiting", "loose motion", "diarrhoea"]),
    ("CARDIOLOGY", "Cardiology", "Heart, chest pain, blood pressure and ECG evaluation", 900,
     ["chest pain", "heart", "palpitations", "breathless", "cholesterol", "ecg", "angina", "dhak-dhak"]),
    ("ORTHO", "Orthopaedics", "Bones, joints, fractures, knee and back pain", 700,
     ["knee", "joint", "fracture", "back pain", "sprain", "shoulder", "bone", "neck pain", "ghutna",
      "kamar dard", "sciatica"]),
    ("DERMA", "Dermatology", "Skin, hair, nails, acne and allergies", 600,
     ["rash", "skin", "itching", "acne", "pimple", "hair fall", "eczema", "fungal", "allergy", "khujli"]),
    ("PEDIA", "Paediatrics", "Newborn and child health, vaccination and growth", 600,
     ["child", "baby", "infant", "vaccination", "teething", "children", "bacche", "newborn"]),
    ("GYNAE", "Obstetrics & Gynaecology", "Pregnancy, menstrual health and women's wellness", 800,
     ["pregnancy", "periods", "menstrual", "pcos", "white discharge", "gynae", "pregnan", "irregular periods"]),
    ("ENT", "ENT", "Ear, nose, throat, sinus and hearing complaints", 600,
     ["ear", "nose", "throat", "sinus", "tonsil", "hearing", "snoring", "kaan", "naak"]),
    ("NEURO", "Neurology", "Headache, migraine, seizures and nerve problems", 1000,
     ["headache", "migraine", "seizure", "dizziness", "numbness", "vertigo", "neuro", "fits", "sir dard"]),
    ("GASTRO", "Gastroenterology", "Stomach, liver, acidity and digestion", 800,
     ["stomach", "acidity", "gas", "ulcer", "liver", "jaundice", "constipation", "abdomen", "pet dard"]),
    ("PULMO", "Pulmonology", "Asthma, breathing difficulty and chest infections", 800,
     ["asthma", "breathing", "wheezing", "tuberculosis", "tb", "bronchitis", "saans", "smoking"]),
    ("OPHTHAL", "Ophthalmology", "Eye check-up, vision and eye infections", 600,
     ["eye", "vision", "blurred", "redness in eye", "cataract", "chashma", "aankh"]),
    ("PSYCH", "Psychiatry", "Stress, anxiety, sleep and mental wellbeing", 900,
     ["stress", "anxiety", "depression", "sleep", "insomnia", "panic", "mental", "tension"]),
]

MEDICINES = [
    ("Paracetamol", "Acetaminophen", "TABLET", "500 mg", "1-0-1", "After food", "5 days"),
    ("Azithromycin", "Azithromycin", "TABLET", "500 mg", "0-1-0", "After food", "3 days"),
    ("Amoxicillin + Clavulanate", "Co-amoxiclav", "TABLET", "625 mg", "1-0-1", "After food", "5 days"),
    ("Cetirizine", "Cetirizine", "TABLET", "10 mg", "0-0-1", "After food", "5 days"),
    ("Pantoprazole", "Pantoprazole", "TABLET", "40 mg", "1-0-0", "Before food", "14 days"),
    ("Ondansetron", "Ondansetron", "TABLET", "4 mg", "SOS", "When needed", "3 days"),
    ("ORS", "Oral Rehydration Salt", "SACHET", "-", "1-1-1", "With water", "3 days"),
    ("Metformin", "Metformin", "TABLET", "500 mg", "1-0-1", "After food", "30 days"),
    ("Amlodipine", "Amlodipine", "TABLET", "5 mg", "1-0-0", "Morning", "30 days"),
    ("Atorvastatin", "Atorvastatin", "TABLET", "10 mg", "0-0-1", "Night", "30 days"),
    ("Ibuprofen", "Ibuprofen", "TABLET", "400 mg", "1-0-1", "After food", "5 days"),
    ("Calcium + Vitamin D3", "Calcium carbonate", "TABLET", "500 mg", "0-0-1", "After food", "60 days"),
    ("Iron + Folic Acid", "Ferrous ascorbate", "TABLET", "100 mg", "1-0-0", "After food", "30 days"),
    ("Salbutamol Inhaler", "Salbutamol", "INHALER", "100 mcg", "SOS", "2 puffs when breathless", "30 days"),
    ("Montelukast", "Montelukast", "TABLET", "10 mg", "0-0-1", "Night", "30 days"),
    ("Levothyroxine", "Levothyroxine", "TABLET", "50 mcg", "1-0-0", "Empty stomach", "30 days"),
    ("Vitamin B Complex", "B-complex", "CAPSULE", "-", "1-0-0", "After food", "15 days"),
    ("Diclofenac Gel", "Diclofenac", "GEL", "1%", "Local", "Apply twice daily", "7 days"),
    ("Cefixime", "Cefixime", "TABLET", "200 mg", "1-0-1", "After food", "5 days"),
    ("Ranitidine", "Ranitidine", "TABLET", "150 mg", "1-0-1", "Before food", "10 days"),
]

DOCTORS = [
    ("Dr Arjun Mehra", "GENMED", "MBBS, MD (Internal Medicine)", 14, 500, 300, 30, 24, "101"),
    ("Dr Nidhi Sharma", "CARDIOLOGY", "MBBS, MD, DM (Cardiology)", 18, 900, 500, 30, 18, "102"),
    ("Dr Rakesh Choudhary", "ORTHO", "MBBS, MS (Orthopaedics)", 12, 700, 400, 20, 24, "103"),
    ("Dr Priya Agarwal", "DERMA", "MBBS, MD (Dermatology)", 9, 600, 350, 20, 20, "104"),
    ("Dr Sameer Khan", "PEDIA", "MBBS, MD (Paediatrics)", 15, 600, 350, 20, 24, "105"),
    ("Dr Anjali Rathore", "GYNAE", "MBBS, MS (Obs & Gynae)", 16, 800, 450, 30, 18, "106"),
    ("Dr Vikas Jain", "ENT", "MBBS, MS (ENT)", 11, 600, 350, 15, 24, "107"),
    ("Dr Meena Kumari", "NEURO", "MBBS, MD, DM (Neurology)", 13, 1000, 600, 30, 15, "108"),
]

PATIENTS = [
    ("Ramesh Yadav", "9876500001", "M", 46, "B+", "Jaipur"),
    ("Sunita Devi", "9876500002", "F", 38, "O+", "Jaipur"),
    ("Mohammed Irfan", "9876500003", "M", 29, "A+", "Ajmer"),
    ("Kavita Sharma", "9876500004", "F", 52, "AB+", "Jaipur"),
    ("Deepak Verma", "9876500005", "M", 61, "O-", "Sikar"),
    ("Pooja Bansal", "9876500006", "F", 24, "B-", "Jaipur"),
    ("Harish Meena", "9876500007", "M", 35, "A-", "Alwar"),
    ("Farida Sheikh", "9876500008", "F", 44, "O+", "Jaipur"),
    ("Anil Gupta", "9876500009", "M", 58, "B+", "Jodhpur"),
    ("Radha Chauhan", "9876500010", "F", 31, "AB-", "Jaipur"),
    ("Suresh Patel", "9876500011", "M", 40, "A+", "Tonk"),
    ("Baby Aarav", "9876500012", "M", 3, "O+", "Jaipur"),
]

STAFF = [
    ("Vijay Vargiya", "superadmin@vijayvargiyahospital.in", "SUPER_ADMIN", "SuperAdmin@123", "9000000001"),
    ("Hospital Admin", "admin@vijayvargiyahospital.in", "ADMIN", "Admin@123", "9000000002"),
    ("Front Desk - Ritu", "reception@vijayvargiyahospital.in", "RECEPTIONIST", "Reception@123", "9000000003"),
    ("Accounts - Mahesh", "accounts@vijayvargiyahospital.in", "ACCOUNTANT", "Accounts@123", "9000000004"),
]


# ==========================================================================
def seed_rbac(db: Session) -> None:
    for name, description in ROLES.items():
        if not db.scalar(select(models.Role).where(models.Role.name == name)):
            db.add(models.Role(name=name, description=description, is_system=True))
    db.commit()

    for code, (module, description) in PERMISSIONS.items():
        if not db.scalar(select(models.Permission).where(models.Permission.code == code)):
            db.add(models.Permission(code=code, module=module, description=description))
    db.commit()

    for role_name, codes in ROLE_PERMISSIONS.items():
        role = db.scalar(select(models.Role).where(models.Role.name == role_name))
        wanted = list(PERMISSIONS.keys()) if "*" in codes else codes
        for code in wanted:
            permission = db.scalar(select(models.Permission).where(models.Permission.code == code))
            if not permission:
                continue
            exists = db.scalar(select(models.RolePermission).where(
                models.RolePermission.role_id == role.id,
                models.RolePermission.permission_id == permission.id))
            if not exists:
                db.add(models.RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()
    log.info("RBAC seeded: %d roles, %d permissions", len(ROLES), len(PERMISSIONS))


def seed_masters(db: Session) -> None:
    for code, name, description, fee, concerns in SPECIALTIES:
        specialty = db.scalar(select(models.Specialty).where(models.Specialty.code == code))
        if not specialty:
            specialty = models.Specialty(code=code, name=name, description=description,
                                         consultation_fee=fee)
            db.add(specialty)
            db.commit()
            db.refresh(specialty)
        for keyword in concerns:
            exists = db.scalar(select(models.SpecialtyConcern).where(
                models.SpecialtyConcern.specialty_id == specialty.id,
                models.SpecialtyConcern.keyword == keyword))
            if not exists:
                db.add(models.SpecialtyConcern(specialty_id=specialty.id, keyword=keyword, weight=2))
    db.commit()

    for name, generic, form, strength, dosage, instructions, duration in MEDICINES:
        if not db.scalar(select(models.Medicine).where(models.Medicine.name == name)):
            db.add(models.Medicine(name=name, generic_name=generic, form=form, strength=strength,
                                   default_dosage=dosage, default_frequency=dosage,
                                   instructions=instructions, default_duration=duration))
    db.commit()

    holidays = [
        ("Republic Day", date(date.today().year, 1, 26)),
        ("Holi", date(date.today().year, 3, 4)),
        ("Independence Day", date(date.today().year, 8, 15)),
        ("Diwali", date(date.today().year, 11, 8)),
        ("New Year", date(date.today().year, 12, 31)),
    ]
    for name, day in holidays:
        if not db.scalar(select(models.Holiday).where(models.Holiday.holiday_date == day)):
            db.add(models.Holiday(name=name, holiday_date=day, is_full_day=True))
    db.commit()
    log.info("Masters seeded: specialties, medicines, holidays")


def seed_users_and_doctors(db: Session) -> None:
    for full_name, email, role_name, password, phone in STAFF:
        if db.scalar(select(models.User).where(models.User.email == email)):
            continue
        role = db.scalar(select(models.Role).where(models.Role.name == role_name))
        db.add(models.User(uuid=uuid.uuid4().hex, full_name=full_name, email=email, phone=phone,
                           password_hash=hash_password(password), role_id=role.id,
                           is_active=True, is_verified=True))
    db.commit()

    doctor_role = db.scalar(select(models.Role).where(models.Role.name == "DOCTOR"))
    for idx, (name, specialty_code, quals, exp, fee, follow_up, slot_min, capacity, room) in enumerate(DOCTORS):
        specialty = db.scalar(select(models.Specialty).where(models.Specialty.code == specialty_code))
        email = f"{name.lower().replace('dr ', 'dr.').replace(' ', '')}@vijayvargiyahospital.in"
        user = db.scalar(select(models.User).where(models.User.email == email))
        if not user:
            user = models.User(
                uuid=uuid.uuid4().hex, full_name=name, email=email, phone=f"90000001{idx:02d}",
                password_hash=hash_password("Doctor@123"), role_id=doctor_role.id,
                is_active=True, is_verified=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        doctor = db.scalar(select(models.Doctor).where(models.Doctor.email == email))
        if not doctor:
            doctor = models.Doctor(
                doctor_code=f"DOC-{idx + 1:04d}", user_id=user.id, specialty_id=specialty.id,
                full_name=name, email=email, phone=user.phone, qualifications=quals,
                experience_years=exp, registration_no=f"RMC/RJ/{2010 + idx}/{1000 + idx}",
                bio=f"{name} has {exp} years of clinical experience in {specialty.name}.",
                languages="Hindi, English", consultation_fee=fee, follow_up_fee=follow_up,
                slot_duration_minutes=slot_min, daily_capacity=capacity, room_number=room,
                is_available_for_ai_booking=True, is_active=True,
            )
            db.add(doctor)
            db.commit()
            db.refresh(doctor)

        # weekly schedule: Mon-Sat, morning + evening OPD with a lunch break
        for day in range(6):
            exists = db.scalar(select(models.DoctorSchedule).where(
                models.DoctorSchedule.doctor_id == doctor.id,
                models.DoctorSchedule.day_of_week == day))
            if exists:
                continue
            db.add(models.DoctorSchedule(
                doctor_id=doctor.id, day_of_week=day, start_time=time(9, 30), end_time=time(17, 30),
                break_start=time(13, 0), break_end=time(14, 0), slot_duration_minutes=slot_min,
                capacity_per_slot=1, is_active=True,
            ))
        db.commit()
    log.info("Staff + %d doctors seeded (doctor login password: Doctor@123)", len(DOCTORS))


def seed_patients_and_history(db: Session) -> None:
    patient_role = db.scalar(select(models.Role).where(models.Role.name == "PATIENT"))
    doctors = db.scalars(select(models.Doctor)).all()
    medicines = db.scalars(select(models.Medicine)).all()
    today = date.today()

    for idx, (name, phone, gender, age, blood, city) in enumerate(PATIENTS):
        email = f"{name.lower().replace(' ', '.')}@example.com"
        user = db.scalar(select(models.User).where(models.User.email == email))
        if not user:
            user = models.User(uuid=uuid.uuid4().hex, full_name=name, email=email, phone=phone,
                               password_hash=hash_password("Patient@123"), role_id=patient_role.id,
                               is_active=True, is_verified=True)
            db.add(user)
            db.commit()
            db.refresh(user)
        patient = db.scalar(select(models.Patient).where(models.Patient.phone == phone))
        if patient:
            continue
        patient = models.Patient(
            patient_code=f"VVH-{today.year}-{idx + 1:05d}", user_id=user.id, full_name=name,
            phone=phone, email=email, gender=gender, age=age, blood_group=blood,
            date_of_birth=date(today.year - age, 1 + (idx % 12), 1 + (idx % 27)),
            city=city, state="Rajasthan", pincode=f"302{idx:03d}",
            address_line=f"{10 + idx}, Vargiiya Nagar", emergency_contact_name="Family contact",
            emergency_contact_phone=f"98100000{idx:02d}", emergency_contact_relation="Spouse",
            allergies="Penicillin" if idx % 4 == 0 else None,
            chronic_conditions="Diabetes Type 2" if idx % 5 == 0 else
                               ("Hypertension" if idx % 3 == 0 else None),
        )
        db.add(patient)
        db.commit()
        db.refresh(patient)

        # ---- historical visits: 2 completed, 1 today, 1 upcoming ----
        for visit_no in range(2):
            doctor = random.choice(doctors)
            visit_day = today - timedelta(days=random.randint(20, 90) + visit_no * 10)
            slot = _make_slot(db, doctor, visit_day, time(10 + (idx + visit_no) % 6, 0))
            appointment = models.Appointment(
                appointment_code=f"APT-{visit_day:%Y%m%d}-{idx:02d}{visit_no}", patient_id=patient.id,
                doctor_id=doctor.id, specialty_id=doctor.specialty_id, slot_id=slot.id if slot else None,
                appointment_date=visit_day, start_time=time(10 + (idx + visit_no) % 6, 0),
                token_number=visit_no + 1, status="COMPLETED", queue_status="COMPLETED",
                source="RECEPTION" if visit_no == 0 else "AI",
                reason=random.choice(["Fever since 3 days", "Knee pain", "Acidity and bloating",
                                      "Skin rash", "Blood pressure review", "Child vaccination"]),
                checked_in_at=datetime.combine(visit_day, time(9, 55)),
                checked_out_at=datetime.combine(visit_day, time(11, 30)),
            )
            db.add(appointment)
            db.commit()
            db.refresh(appointment)

            consultation = models.Consultation(
                consultation_code=f"CON-{visit_day:%Y%m%d}-{idx:02d}{visit_no}",
                appointment_id=appointment.id, patient_id=patient.id, doctor_id=doctor.id,
                status="COMPLETED", chief_complaint=appointment.reason,
                vitals={"bp": f"{118 + idx}/{78 + idx % 10}", "pulse": 72 + idx % 12,
                        "temp_c": 98.2 + (idx % 3) * 0.4, "spo2": 97 + idx % 3, "weight_kg": 60 + idx},
                observations="No acute distress. Systemic examination unremarkable.",
                diagnosis=random.choice(["Acute viral febrile illness", "Osteoarthritis knee",
                                         "Gastritis / acid peptic disease", "Allergic dermatitis",
                                         "Essential hypertension - controlled"]),
                clinical_notes="Advised hydration, rest and review if symptoms persist.",
                advice="Complete the course, drink plenty of fluids, follow up if fever persists beyond 3 days.",
                follow_up_required=visit_no == 1, follow_up_date=today + timedelta(days=random.randint(5, 20)),
                follow_up_notes="Review with reports",
                started_at=datetime.combine(visit_day, time(10, 5)),
                completed_at=datetime.combine(visit_day, time(10, 25)),
            )
            db.add(consultation)
            db.commit()
            db.refresh(consultation)

            from .services import patients as patient_service
            patient_service.add_medical_record(
                db, patient_id=patient.id, consultation_id=consultation.id,
                appointment_id=appointment.id, doctor_id=doctor.id, record_type="DIAGNOSIS",
                title="Diagnosis", description=consultation.diagnosis,
                values_json=consultation.vitals, entered_by=doctor.user_id,
            )

            # prescription
            chosen = random.sample(medicines, k=2)
            prescription = models.Prescription(
                prescription_code=f"RX-{visit_day:%Y%m%d}-{idx:02d}{visit_no}", consultation_id=consultation.id,
                patient_id=patient.id, doctor_id=doctor.id, diagnosis_summary=consultation.diagnosis,
                advice=consultation.advice, status="ISSUED",
                issued_at=datetime.combine(visit_day, time(10, 30)),
                valid_until=visit_day + timedelta(days=30),
            )
            db.add(prescription)
            db.commit()
            db.refresh(prescription)
            for med in chosen:
                db.add(models.PrescriptionItem(
                    prescription_id=prescription.id, medicine_id=med.id, medicine_name=med.name,
                    dosage=med.default_dosage, frequency=med.default_dosage,
                    duration=med.default_duration, instructions=med.instructions, quantity=1))
            db.commit()

            # invoice + payment
            invoice = billing.create_invoice(
                db, patient=patient,
                items=[{"item_type": "CONSULTATION", "description": f"Consultation - {doctor.full_name}",
                        "quantity": 1, "unit_price": float(doctor.consultation_fee)},
                       {"item_type": "PROCEDURE", "description": "Nebulisation / dressing",
                        "quantity": 1, "unit_price": 150 if visit_no % 2 == 0 else 0}],
                appointment_id=appointment.id, consultation_id=consultation.id, doctor_id=doctor.id,
                tax_percent=0, discount_amount=50 if idx % 7 == 0 else 0,
                discount_reason="Senior citizen discount" if idx % 7 == 0 else None,
            )
            method = random.choice(["CASH", "UPI", "CARD"])
            if visit_no == 0:
                billing.record_payment(db, patient=patient, amount=float(invoice.total_amount), method=method,
                                       invoice=invoice, status="PAID",
                                       transaction_reference=f"TXN{idx}{visit_no}0001")
            else:
                billing.record_payment(db, patient=patient,
                                       amount=round(float(invoice.total_amount) * 0.5, 2), method=method,
                                       invoice=invoice, status="PAID",
                                       transaction_reference=f"TXN{idx}{visit_no}0002")

            # review for the first visit
            if visit_no == 0 and idx % 3 == 0:
                db.add(models.Review(
                    patient_id=patient.id, doctor_id=doctor.id, appointment_id=appointment.id,
                    review_type="DOCTOR", rating=random.choice([4, 5, 5, 3]),
                    title="Good experience", comment="Doctor explained clearly and staff was helpful.",
                    status="APPROVED" if idx % 2 == 0 else "PENDING"))
                db.commit()

        # ---- one upcoming appointment for every other patient ----
        if idx % 2 == 0:
            doctor = random.choice(doctors)
            future_day = today + timedelta(days=random.randint(1, 6))
            slot = _make_slot(db, doctor, future_day, time(11 + (idx % 5), 0))
            if slot:
                appointment = models.Appointment(
                    appointment_code=f"APT-{future_day:%Y%m%d}-{idx:02d}9", patient_id=patient.id,
                    doctor_id=doctor.id, specialty_id=doctor.specialty_id, slot_id=slot.id,
                    appointment_date=future_day, start_time=slot.start_time, end_time=slot.end_time,
                    token_number=1, status="CONFIRMED", queue_status="WAITING",
                    source="AI" if idx % 4 == 0 else "RECEPTION",
                    reason="Follow-up review",
                )
                db.add(appointment)
                db.commit()
                slot_service.mark_slot_booked(db, slot)
                billing.ensure_consultation_invoice(db, appointment, doctor)

    log.info("Patients + clinical history seeded (%d patients)", len(PATIENTS))


def _make_slot(db: Session, doctor: models.Doctor, day: date, start: time):
    """Create (or reuse) a slot at a specific time for seeding history."""
    end = (datetime.combine(day, start) + timedelta(minutes=doctor.slot_duration_minutes)).time()
    slot = db.scalar(select(models.Slot).where(
        models.Slot.doctor_id == doctor.id, models.Slot.slot_date == day,
        models.Slot.start_time == start))
    if slot:
        return slot
    slot = models.Slot(doctor_id=doctor.id, slot_date=day, start_time=start, end_time=end,
                       capacity=1, booked_count=1, status="BOOKED", generated_by="SEED")
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


def seed_ai_activity(db: Session) -> None:
    """A couple of sample AI conversations so the AI monitor is not empty."""
    patients = db.scalars(select(models.Patient)).all()
    if not patients:
        return
    if db.scalar(select(func.count(models.AIConversation.id))):
        return
    samples = [
        ("fever since 3 days", "BOOK_APPOINTMENT", 1, 1),
        ("knee pain while walking", "BOOK_APPOINTMENT", 1, 0),
        ("where is my report", "MEDICAL_FILE_LOOKUP", 0, 0),
    ]
    for idx, (text, intent, bookings, escalations) in enumerate(samples):
        patient = patients[idx % len(patients)]
        conv = models.AIConversation(
            session_id=uuid.uuid4().hex, patient_id=patient.id, channel="WEB", status="CLOSED",
            stage="POST_BOOK", patient_type="EXISTING", primary_intent=intent,
            intent_history=[{"intent": intent, "at": utcnow().isoformat()}],
            concern_summary=text, summary=f"Patient reported {text}. Routed and handled.",
            message_count=6, tool_call_count=4, booking_attempts=1,
            successful_bookings=bookings, failed_bookings=0, escalation_count=escalations,
            avg_response_ms=random.randint(280, 900),
            started_at=utcnow() - timedelta(days=idx + 1),
            last_message_at=utcnow() - timedelta(days=idx + 1),
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        db.add(models.AIMessage(conversation_id=conv.id, role="user", content=text, intent=intent,
                                confidence=0.9))
        db.add(models.AIMessage(conversation_id=conv.id, role="assistant",
                                content="I found the right specialist and shared live slots.",
                                intent=intent, confidence=0.9, latency_ms=420,
                                tool_calls=["search_specialties", "search_doctors", "get_available_slots"]))
        db.add(models.AIConcern(patient_id=patient.id, conversation_id=conv.id, concern_text=text,
                                keywords=[text.split()[0]], urgency="LOW", status="ADDRESSED"))
        db.add(models.AIToolCall(conversation_id=conv.id, tool_name="search_specialties",
                                 arguments={"query": text}, status="SUCCESS", duration_ms=45))
        db.commit()
    log.info("AI sample conversations seeded")


def seed_all(force: bool = False) -> dict:
    """Idempotent, *stage aware* seeding.

    Each area is filled only when it is empty, so this is safe to run
    repeatedly and - importantly - it also completes a database that was
    bootstrapped by the SQL scripts:

        sql/01_setup_database.sql  -> database + users + grants
        sql/02_schema.sql          -> 40 tables + views
        sql/03_seed.sql            -> RBAC, specialties, medicines, doctors
        python -m app.seed         -> demo patients/history, AI samples, slots   <-- this function

    A blank check is per-area rather than "any user exists", because the SQL
    seed already creates the staff logins and would otherwise cause the
    patients/slot stage to be skipped on a real MySQL install.
    """
    init_db()
    db = SessionLocal()
    try:
        counts = {
            "roles": db.scalar(select(func.count(models.Role.id))) or 0,
            "permissions": db.scalar(select(func.count(models.Permission.id))) or 0,
            "specialties": db.scalar(select(func.count(models.Specialty.id))) or 0,
            "medicines": db.scalar(select(func.count(models.Medicine.id))) or 0,
            "users": db.scalar(select(func.count(models.User.id))) or 0,
            "doctors": db.scalar(select(func.count(models.Doctor.id))) or 0,
            "patients": db.scalar(select(func.count(models.Patient.id))) or 0,
            "ai_conversations": db.scalar(select(func.count(models.AIConversation.id))) or 0,
            "future_slots": db.scalar(select(func.count(models.Slot.id)).where(
                models.Slot.slot_date >= date.today())) or 0,
        }
        done: dict[str, object] = {}

        if force or not counts["roles"] or not counts["permissions"]:
            seed_rbac(db)
            done["rbac"] = True

        if force or not counts["specialties"] or not counts["medicines"]:
            seed_masters(db)
            done["masters"] = True

        if force or not counts["users"] or not counts["doctors"]:
            seed_users_and_doctors(db)
            done["staff_and_doctors"] = True

        if force or not counts["patients"]:
            seed_patients_and_history(db)
            done["patients"] = True

        if force or not counts["ai_conversations"]:
            seed_ai_activity(db)
            done["ai_samples"] = True

        if force or not counts["future_slots"]:
            result = slot_service.generate_all_slots(db, start_date=date.today(), days_ahead=14)
            done["slots_created"] = result["created"]
            log.info("Slot engine generated %s slots for the next 14 days", result["created"])

        skipped = not done
        if skipped:
            log.info("Nothing to seed - database already complete (%s users, %s patients)",
                     counts["users"], counts["patients"])
        return {"skipped": skipped, "existing": counts, "seeded": done}
    finally:
        db.close()


if __name__ == "__main__":
    init_db(drop=False)
    summary = seed_all(force="--force" in sys.argv)
    print("Seed complete:", summary)
    print("\nLogins (password in brackets):")
    for full_name, email, role_name, password, phone in STAFF:
        print(f"  {role_name:<12} {email:<45} [{password}]")
    print(f"  {'DOCTOR':<12} dr.arjunmehra@vijayvargiyahospital.in            [Doctor@123]")
    print(f"  {'PATIENT':<12} ramesh.yadav@example.com                        [Patient@123]")
