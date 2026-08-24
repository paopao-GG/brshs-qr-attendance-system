-- TRACKIFY V1 schema.
-- Central rule (docs/flow.md 4.2): raw scans are immutable, derived attendance is correctable.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('operator', 'adviser', 'admin')),
    full_name     TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    grade_level INTEGER NOT NULL,
    adviser_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (grade_level, name)
);

CREATE TABLE IF NOT EXISTS students (
    id              INTEGER PRIMARY KEY,
    lrn             TEXT    NOT NULL UNIQUE,
    first_name      TEXT    NOT NULL,
    last_name       TEXT    NOT NULL,
    section_id      INTEGER NOT NULL REFERENCES sections(id) ON DELETE RESTRICT,
    guardian_name   TEXT,
    guardian_mobile TEXT,               -- stored normalised as 639XXXXXXXXX
    photo_path      TEXT,
    consent_on_file INTEGER NOT NULL DEFAULT 0,
    notify_optin    INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_students_section  ON students(section_id);
CREATE INDEX IF NOT EXISTS idx_students_guardian ON students(guardian_mobile);

CREATE TABLE IF NOT EXISTS school_days (
    date                   TEXT PRIMARY KEY,       -- YYYY-MM-DD
    is_school_day          INTEGER NOT NULL DEFAULT 1,
    suspension_reason      TEXT,
    entry_open             TEXT NOT NULL,
    late_threshold         TEXT NOT NULL,
    dismissal_time         TEXT NOT NULL,
    early_departure_cutoff TEXT NOT NULL
);

-- APPEND ONLY. Never updated, never deleted. Corrections go to attendance_days.
CREATE TABLE IF NOT EXISTS scan_events (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
    scanned_at  TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('in', 'out')),
    method      TEXT    NOT NULL CHECK (method IN ('scan', 'manual')),
    raw_payload TEXT,
    operator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    override_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_student_date ON scan_events(student_id, date);

CREATE TABLE IF NOT EXISTS attendance_days (
    id                INTEGER PRIMARY KEY,
    student_id        INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
    date              TEXT    NOT NULL,
    entry_scan_id     INTEGER REFERENCES scan_events(id) ON DELETE SET NULL,
    exit_scan_id      INTEGER REFERENCES scan_events(id) ON DELETE SET NULL,
    status            TEXT    NOT NULL CHECK (status IN
                          ('present', 'late', 'absent', 'excused', 'online')),
    flags             TEXT    NOT NULL DEFAULT '',   -- comma-separated
    minutes_on_campus INTEGER,
    superseded_by     INTEGER REFERENCES attendance_days(id) ON DELETE SET NULL,
    corrected_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    correction_reason TEXT,
    created_at        TEXT    NOT NULL
);
-- One live row per student per day; superseded rows are exempt so history is kept.
CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_live
    ON attendance_days(student_id, date) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY,
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
    guardian_mobile     TEXT    NOT NULL,
    trigger             TEXT    NOT NULL CHECK (trigger IN
                            ('arrival', 'departure', 'late', 'absent')),
    idempotency_key     TEXT    NOT NULL UNIQUE,
    body                TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN
                            ('pending', 'sending', 'sent', 'failed', 'unknown', 'suppressed')),
    retry_count         INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    coalesce_group      TEXT,
    last_error          TEXT,
    event_at            TEXT    NOT NULL,   -- when the scan happened, not when queued
    queued_at           TEXT    NOT NULL,
    claimed_at          TEXT,
    sent_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_status   ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notif_guardian ON notifications(guardian_mobile, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    old_value   TEXT,
    new_value   TEXT,
    reason      TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(occurred_at);

CREATE TABLE IF NOT EXISTS ahp_weights (
    id            INTEGER PRIMARY KEY,
    version       INTEGER NOT NULL UNIQUE,
    matrix_json   TEXT    NOT NULL,
    weights_json  TEXT    NOT NULL,
    lambda_max    REAL    NOT NULL,
    ci            REAL    NOT NULL,
    cr            REAL    NOT NULL,
    elicited_from TEXT    NOT NULL,
    elicited_at   TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS risk_scores (
    id                    INTEGER PRIMARY KEY,
    student_id            INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    computed_at           TEXT    NOT NULL,
    p_absent              REAL    NOT NULL,
    tardiness_score       REAL    NOT NULL,
    early_departure_score REAL    NOT NULL,
    composite             REAL    NOT NULL,
    band                  TEXT    NOT NULL,
    weights_version       INTEGER REFERENCES ahp_weights(version)
);
CREATE INDEX IF NOT EXISTS idx_risk_student ON risk_scores(student_id, computed_at);

CREATE TABLE IF NOT EXISTS sms_ledger (
    date          TEXT PRIMARY KEY,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    breaker_hit   INTEGER NOT NULL DEFAULT 0,
    breaker_at    TEXT
);
