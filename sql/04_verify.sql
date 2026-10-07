-- =============================================================================
--  VIJAY VARGIYA GROUP OF HOSPITALS - post-setup verification
--  mysql -u root -p vijay_vargiya_hospital < sql/04_verify.sql
-- =============================================================================
USE `vijay_vargiya_hospital`;

SELECT '--- expected data sanity (FAIL rows need attention) ---' AS section;
SELECT 'roles seeded (6)' AS expected,
       IF((SELECT COUNT(*) FROM roles) = 6, 'OK', 'FAIL') AS result,
       (SELECT COUNT(*) FROM roles) AS actual_value
UNION ALL SELECT 'permissions (50)', IF((SELECT COUNT(*) FROM permissions) = 50, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM permissions)
UNION ALL SELECT 'role_permissions (175)', IF((SELECT COUNT(*) FROM role_permissions) = 175, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM role_permissions)
UNION ALL SELECT 'specialties (12)', IF((SELECT COUNT(*) FROM specialties) = 12, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM specialties)
UNION ALL SELECT 'specialty_concerns (107)', IF((SELECT COUNT(*) FROM specialty_concerns) = 107, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM specialty_concerns)
UNION ALL SELECT 'medicines (10)', IF((SELECT COUNT(*) FROM medicines) = 10, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM medicines)
UNION ALL SELECT 'doctors (8)', IF((SELECT COUNT(*) FROM doctors) = 8, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM doctors)
UNION ALL SELECT 'doctor_schedules (>= 48 = 8 doctors x 6 days)',
       IF((SELECT COUNT(*) FROM doctor_schedules) >= 48, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM doctor_schedules)
UNION ALL SELECT 'staff logins (12)',
       IF((SELECT COUNT(*) FROM users) >= 12, 'OK', 'CHECK'),
       (SELECT COUNT(*) FROM users)
UNION ALL SELECT 'every doctor has a schedule',
       IF(NOT EXISTS (SELECT 1 FROM doctors d WHERE NOT EXISTS
            (SELECT 1 FROM doctor_schedules ds WHERE ds.doctor_id = d.id)), 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM doctors d WHERE NOT EXISTS
            (SELECT 1 FROM doctor_schedules ds WHERE ds.doctor_id = d.id))
UNION ALL SELECT 'reporting views (4)',
       IF((SELECT COUNT(*) FROM information_schema.views
            WHERE table_schema = 'vijay_vargiya_hospital') = 4, 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM information_schema.views WHERE table_schema = 'vijay_vargiya_hospital')
UNION ALL SELECT 'all tables InnoDB',
       IF(NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'vijay_vargiya_hospital'
            AND table_type = 'BASE TABLE' AND engine <> 'InnoDB'), 'OK', 'FAIL'),
       (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'vijay_vargiya_hospital'
            AND table_type = 'BASE TABLE' AND engine <> 'InnoDB')
--  NB: patients / slots stay empty after the SQL seed on purpose; run
--      `python -m app.seed` (stage aware) to add demo patients + 14 days of slots.
UNION ALL SELECT 'demo patients (0 until app.seed runs)',
       IF((SELECT COUNT(*) FROM patients) = 0, 'OK', 'INFO'), (SELECT COUNT(*) FROM patients)
UNION ALL SELECT 'future slots (0 until app.seed runs)',
       IF((SELECT COUNT(*) FROM slots WHERE slot_date >= CURDATE()) = 0, 'OK', 'INFO'),
       (SELECT COUNT(*) FROM slots WHERE slot_date >= CURDATE());

SELECT '--- idempotency probe (safe to re-run; insert then roll back) ---' AS section;
START TRANSACTION;
INSERT INTO specialties (code, name, description, consultation_fee, is_active, created_at, updated_at)
VALUES ('VERIFY_PROBE', 'Verify Probe', 'temporary row used by 04_verify.sql', 1, 0, NOW(), NOW());
SELECT 'insert worked, rolling back' AS probe, COUNT(*) AS rows_visible_in_txn
FROM specialties WHERE code = 'VERIFY_PROBE';
ROLLBACK;
SELECT 'rollback left no trace' AS probe,
       IF((SELECT COUNT(*) FROM specialties WHERE code = 'VERIFY_PROBE') = 0, 'OK', 'FAIL') AS result;

SELECT '--- tables ---' AS section;
SELECT table_name, table_rows, engine, table_collation
FROM information_schema.tables
WHERE table_schema = 'vijay_vargiya_hospital'
ORDER BY table_name;

SELECT '--- row counts ---' AS section;
SELECT 'roles' AS t, COUNT(*) AS n FROM roles
UNION ALL SELECT 'permissions', COUNT(*) FROM permissions
UNION ALL SELECT 'role_permissions', COUNT(*) FROM role_permissions
UNION ALL SELECT 'users', COUNT(*) FROM users
UNION ALL SELECT 'patients', COUNT(*) FROM patients
UNION ALL SELECT 'specialties', COUNT(*) FROM specialties
UNION ALL SELECT 'specialty_concerns', COUNT(*) FROM specialty_concerns
UNION ALL SELECT 'doctors', COUNT(*) FROM doctors
UNION ALL SELECT 'doctor_schedules', COUNT(*) FROM doctor_schedules
UNION ALL SELECT 'slots', COUNT(*) FROM slots
UNION ALL SELECT 'appointments', COUNT(*) FROM appointments
UNION ALL SELECT 'consultations', COUNT(*) FROM consultations
UNION ALL SELECT 'medical_records', COUNT(*) FROM medical_records
UNION ALL SELECT 'prescriptions', COUNT(*) FROM prescriptions
UNION ALL SELECT 'invoices', COUNT(*) FROM invoices
UNION ALL SELECT 'payments', COUNT(*) FROM payments
UNION ALL SELECT 'ai_conversations', COUNT(*) FROM ai_conversations
UNION ALL SELECT 'ai_tool_calls', COUNT(*) FROM ai_tool_calls
UNION ALL SELECT 'audit_logs', COUNT(*) FROM audit_logs;

SELECT '--- role / permission matrix ---' AS section;
SELECT r.name AS role, COUNT(rp.id) AS permissions
FROM roles r LEFT JOIN role_permissions rp ON rp.role_id = r.id
GROUP BY r.name ORDER BY permissions DESC;

SELECT '--- user accounts ---' AS section;
SELECT u.id, u.full_name, u.email, r.name AS role, u.is_active, u.last_login_at
FROM users u JOIN roles r ON r.id = u.role_id ORDER BY u.id;

SELECT '--- grants for the application user ---' AS section;
SHOW GRANTS FOR 'vvh_app'@'localhost';

SELECT '--- upcoming slots per doctor ---' AS section;
SELECT d.full_name AS doctor, s.name AS specialty, COUNT(sl.id) AS open_slots,
       MIN(sl.slot_date) AS first_date, MAX(sl.slot_date) AS last_date
FROM doctors d
JOIN specialties s ON s.id = d.specialty_id
LEFT JOIN slots sl ON sl.doctor_id = d.id AND sl.status = 'AVAILABLE' AND sl.slot_date >= CURDATE()
GROUP BY d.id ORDER BY open_slots DESC;
