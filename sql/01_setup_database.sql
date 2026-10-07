# =============================================================================
#  VIJAY VARGIYA GROUP OF HOSPITALS  -  MySQL bootstrap
#  Run as root:      mysql -u root -p < sql/01_setup_database.sql
#  Then the schema:  mysql -u root -p vijay_vargiya_hospital < sql/02_schema.sql
#  Then the seed:    mysql -u root -p vijay_vargiya_hospital < sql/03_seed.sql
# =============================================================================

-- ---------------------------------------------------------------------------
-- 1. DATABASE
-- ---------------------------------------------------------------------------
DROP DATABASE IF EXISTS `vijay_vargiya_hospital`;
CREATE DATABASE `vijay_vargiya_hospital`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE `vijay_vargiya_hospital`;

-- ---------------------------------------------------------------------------
-- 2. APPLICATION USERS
--    vvh_app       -> runtime user for the FastAPI application (least privilege)
--    vvh_readonly  -> analytics / reporting / BI user (SELECT only)
--    vvh_backup    -> backup user (dump + lock tables)
--    vvh_migrator  -> schema owner used by DDL scripts / alembic
-- ---------------------------------------------------------------------------
DROP USER IF EXISTS 'vvh_app'@'localhost';
DROP USER IF EXISTS 'vvh_app'@'%';
DROP USER IF EXISTS 'vvh_readonly'@'localhost';
DROP USER IF EXISTS 'vvh_backup'@'localhost';
DROP USER IF EXISTS 'vvh_migrator'@'localhost';

CREATE USER 'vvh_app'@'localhost'      IDENTIFIED BY 'CHANGE_ME_APP_PASSWORD';
CREATE USER 'vvh_app'@'%'              IDENTIFIED BY 'CHANGE_ME_APP_PASSWORD';
CREATE USER 'vvh_readonly'@'localhost' IDENTIFIED BY 'CHANGE_ME_READONLY_PASSWORD';
CREATE USER 'vvh_backup'@'localhost'   IDENTIFIED BY 'CHANGE_ME_BACKUP_PASSWORD';
CREATE USER 'vvh_migrator'@'localhost' IDENTIFIED BY 'CHANGE_ME_MIGRATOR_PASSWORD';

-- ---------------------------------------------------------------------------
-- 3. GRANTS  (app: DML + routines; migrator: DDL; readonly: SELECT; backup: dump)
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW, CREATE TEMPORARY TABLES
    ON `vijay_vargiya_hospital`.* TO 'vvh_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE, SHOW VIEW, CREATE TEMPORARY TABLES
    ON `vijay_vargiya_hospital`.* TO 'vvh_app'@'%';

GRANT ALL PRIVILEGES ON `vijay_vargiya_hospital`.* TO 'vvh_migrator'@'localhost';

GRANT SELECT, SHOW VIEW, EXECUTE ON `vijay_vargiya_hospital`.* TO 'vvh_readonly'@'localhost';

--  NOTE: RELOAD is a GLOBAL privilege and cannot be mixed into a database-level
--  GRANT (MySQL/Galera/MariaDB all reject that with ERROR 1221), so it is granted
--  separately on *.*  -- the database-level part stays scoped to this schema.
GRANT SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER
    ON `vijay_vargiya_hospital`.* TO 'vvh_backup'@'localhost';
GRANT RELOAD ON *.* TO 'vvh_backup'@'localhost';

-- ---------------------------------------------------------------------------
-- 4. FLUSH PRIVILEGES  (reload grant tables so changes apply immediately)
-- ---------------------------------------------------------------------------
FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- 5. SANITY CHECKS
-- ---------------------------------------------------------------------------
SELECT user, host, plugin FROM mysql.user WHERE user LIKE 'vvh%';
SHOW GRANTS FOR 'vvh_app'@'localhost';
SHOW GRANTS FOR 'vvh_readonly'@'localhost';

-- ---------------------------------------------------------------------------
-- 6. OPTIONAL: session defaults for the app user
-- ---------------------------------------------------------------------------
SET GLOBAL time_zone = '+05:30';
SET GLOBAL max_connections = 200;
SET GLOBAL innodb_file_per_table = ON;

-- ---------------------------------------------------------------------------
-- 7. REPORTING VIEWS  ->  see the end of sql/02_schema.sql
--    (views reference the tables, so they must be created after the schema;
--     running them here on a fresh install would fail with ERROR 1146)
-- ---------------------------------------------------------------------------

SELECT 'Database, users and grants are ready. Now run sql/02_schema.sql' AS status;
