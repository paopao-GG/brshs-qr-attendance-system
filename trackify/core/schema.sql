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
    -- See audit_log.actor_name. corrected_by stays NULL until real logins exist.
    corrected_by_name TEXT,
    correction_reason TEXT,
    correction_type   TEXT,
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
                            ('arrival', 'departure', 'late', 'absent', 'incident')),
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
    -- Earliest time a failed row may be claimed again. NULL means "now".
    -- Without it a failure is re-tried on the very next 4s drain tick and the whole
    -- retry_limit burns out inside 20 seconds -- see notifications.backoff_seconds.
    next_attempt_at     TEXT,
    sent_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_status   ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notif_guardian ON notifications(guardian_mobile, status);

-- Small key/value store for things a person changes at runtime. The records
-- password hash lives here rather than in config.toml, which is committed to git.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    old_value   TEXT,
    new_value   TEXT,
    reason      TEXT,
    -- A name someone TYPED, not an identity the system verified. Kept apart from
    -- actor_id on purpose: one is a claim, the other would be proof, and conflating
    -- them would overstate what the audit trail can support.
    actor_name  TEXT,
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
    -- Confirmed prohibited-item incidents, and which rule decided the band. Without
    -- band_source a stored 'High' cannot be explained later: the composite alone would
    -- not account for it, because an incident floor raises the band without touching
    -- the score. See docs/prohibited-items.md section 9.
    incidents             INTEGER NOT NULL DEFAULT 0,
    band_source           TEXT    NOT NULL DEFAULT 'composite',
    weights_version       INTEGER REFERENCES ahp_weights(version)
);
CREATE INDEX IF NOT EXISTS idx_risk_student ON risk_scores(student_id, computed_at);

CREATE TABLE IF NOT EXISTS sms_ledger (
    date          TEXT PRIMARY KEY,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    breaker_hit   INTEGER NOT NULL DEFAULT 0,
    breaker_at    TEXT
);

-- ---------------------------------------------------------------------------
-- Screening, incidents, and custody of hazardous school tools.
--
-- The detector is a SEPARATE DEVICE operated by a person, not something this system
-- reads. Every row below therefore records a human judgement, which is why none of
-- them carry a sensor reading and why screening_events has no student_id: attribution
-- flows only through the arming scan (docs/flow.md Rule 2), so the rule is structural
-- rather than a convention someone has to remember.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS screening_events (
    id              INTEGER PRIMARY KEY,
    -- The arming scan. NOT NULL: a screening that cannot be traced to a scan cannot
    -- be attributed to anyone, and an unattributable safety record is worse than none.
    scan_event_id   INTEGER NOT NULL REFERENCES scan_events(id) ON DELETE RESTRICT,
    occurred_at     TEXT    NOT NULL,
    metal_detected  INTEGER NOT NULL DEFAULT 0,   -- what the operator observed
    outcome         TEXT    NOT NULL CHECK (outcome IN
                        ('clear', 'common_items', 'prohibited', 'school_hazard',
                         'pending_verification', 'not_screened', 'overridden')),
    declared_items  TEXT,                          -- the declaration tray
    override_reason TEXT,
    notes           TEXT,
    operator_id     INTEGER REFERENCES users(id) ON DELETE SET NULL
);
-- One screening per scan. A second reading of the same student is a second scan.
CREATE UNIQUE INDEX IF NOT EXISTS idx_screening_scan ON screening_events(scan_event_id);
CREATE INDEX IF NOT EXISTS idx_screening_outcome ON screening_events(outcome);

CREATE TABLE IF NOT EXISTS incidents (
    id                  INTEGER PRIMARY KEY,
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
    screening_event_id  INTEGER NOT NULL REFERENCES screening_events(id) ON DELETE RESTRICT,
    occurred_at         TEXT    NOT NULL,
    category            TEXT    NOT NULL CHECK (category IN
                            ('bladed', 'blunt', 'pointed', 'tool', 'other')),
    -- Mandatory. The category is for counting; this is for knowing what happened.
    item_description    TEXT    NOT NULL,
    severity            INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 4),
    severity_reason     TEXT,                      -- required when severity != default
    notes               TEXT,
    confirmed_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- Sensitive personal information under RA 10173: a record naming a minor and
    -- describing a prohibited item. Guidance and administrators only.
    visibility          TEXT    NOT NULL DEFAULT 'restricted'
);
CREATE INDEX IF NOT EXISTS idx_incident_student ON incidents(student_id, occurred_at);

CREATE TABLE IF NOT EXISTS custody_items (
    id                  INTEGER PRIMARY KEY,
    student_id          INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
    screening_event_id  INTEGER REFERENCES screening_events(id) ON DELETE SET NULL,
    item_description    TEXT    NOT NULL,
    category            TEXT,
    purpose             TEXT,                      -- "for art class" -- why they had it
    -- The physical tag or bin number. Without it "held" does not tell anyone where the
    -- item actually is, and a box of forty confiscated cutters is unsearchable.
    storage_ref         TEXT,
    status              TEXT    NOT NULL DEFAULT 'held' CHECK (status IN
                            ('held', 'released', 'returned', 'disposed')),
    collected_at        TEXT    NOT NULL,
    collected_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    released_at         TEXT,
    released_to         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    release_reason      TEXT,
    -- True when an item was released with no matching hazard_request on file. The
    -- request is the control; releasing without one is the exception worth seeing.
    released_unbacked   INTEGER NOT NULL DEFAULT 0,
    returned_at         TEXT,
    returned_to         TEXT CHECK (returned_to IN ('storage', 'student'))
);
CREATE INDEX IF NOT EXISTS idx_custody_status  ON custody_items(status);
CREATE INDEX IF NOT EXISTS idx_custody_student ON custody_items(student_id);

-- A teacher declaring in advance that a section needs hazardous tools for a subject,
-- so releasing them to the adviser is an expected event rather than a judgement call.
CREATE TABLE IF NOT EXISTS hazard_requests (
    id           INTEGER PRIMARY KEY,
    section_id   INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    date         TEXT    NOT NULL,
    subject      TEXT    NOT NULL,
    item_type    TEXT    NOT NULL,
    notes        TEXT,
    requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hazard_section_date ON hazard_requests(section_id, date);
