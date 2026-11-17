-- Nadhir dashboard — SQLite schema (simplified)
-- Load with:  sqlite3 nadhir.db < schema.sql   (or db.executescript() from Python)
-- In Python, run db.execute("PRAGMA foreign_keys = ON") on every connection.
-- Timestamps are ISO-8601 UTC text ("2026-11-20T02:14:05Z"): they sort correctly
-- as text and match the API format exactly.

PRAGMA foreign_keys = ON;

-- ── Configuration (Settings screen) ─────────────────────────────────────

-- One row per dial or lamp; dials carry the thresholds used by rules.py.
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id    TEXT PRIMARY KEY,            -- "oil_temp", "alarm_red"
    kind             TEXT NOT NULL CHECK (kind IN ('dial','lamp')),
    label            TEXT NOT NULL,               -- display name
    unit             TEXT,                        -- dials only
    watch_threshold  REAL,                        -- dials: WATCH level
    limit_threshold  REAL,                        -- dials: limit level
    max_rise_per_min REAL                          -- dials: drift-rate rule, NULL = none
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN
                     ('electrician','guard','maintenance_manager','plant_manager')),
    phone            TEXT NOT NULL,
    escalation_order INTEGER                       -- 1 = first called, NULL = not in chain
);

-- Key/value: escalation_delay_s (300), offline_timeout_s (30), ...
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('escalation_delay_s', '300'),
    ('offline_timeout_s',  '30');

-- ── Measurements (Overview + instrument detail) ─────────────────────────

-- One row per received reading (= one POST). Siren lives here: 1:1 with a reading.
CREATE TABLE IF NOT EXISTS readings (
    reading_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id          TEXT NOT NULL,
    ts               TEXT NOT NULL,               -- device timestamp
    received_at      TEXT NOT NULL,               -- server clock; MAX(received_at) drives SYSTEM_OFFLINE
    system_state     TEXT NOT NULL CHECK (system_state IN
                     ('NORMAL','WATCH','INTERVENE_NOW','SYSTEM_OFFLINE')),
    siren_detected   INTEGER NOT NULL CHECK (siren_detected IN (0,1)),
    siren_confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_site_ts ON readings (site_id, ts);

-- One row per dial or lamp value inside a reading. Dials fill value/unit/message,
-- lamps fill state/color; the other side stays NULL. No FK on instrument_id so a
-- reading can never be rejected just because its instrument is not configured yet.
CREATE TABLE IF NOT EXISTS instrument_readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id    INTEGER NOT NULL REFERENCES readings(reading_id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('OK','UNCERTAIN','REJECTED')),
    confidence    REAL CHECK (confidence BETWEEN 0 AND 1),
    value         REAL,                           -- dials; NULL when REJECTED, never invented
    unit          TEXT,
    message       TEXT NOT NULL DEFAULT '',       -- "check needed" when REJECTED
    state         TEXT CHECK (state IN ('on','off')),            -- lamps
    color         TEXT CHECK (color IN ('red','orange','green')) -- lamps; NULL when off
);
CREATE INDEX IF NOT EXISTS idx_inst_readings_instrument ON instrument_readings (instrument_id, reading_id);  -- curves
CREATE INDEX IF NOT EXISTS idx_inst_readings_status     ON instrument_readings (status, instrument_id);      -- "latest unreadable"

-- Every state change is recorded with its time.
CREATE TABLE IF NOT EXISTS state_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    old_state  TEXT,                              -- NULL for the very first state
    new_state  TEXT NOT NULL,
    reason     TEXT                               -- trigger string from rules.py
);

-- ── Alerts (alert log, alert detail, morning review) ────────────────────

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            TEXT PRIMARY KEY,         -- "A-0007"
    site_id             TEXT NOT NULL,
    level               TEXT NOT NULL CHECK (level IN ('WATCH','INTERVENE_NOW','SYSTEM_OFFLINE')),
    created_at          TEXT NOT NULL,
    trigger             TEXT NOT NULL,
    outcome             TEXT,                     -- filled in the next morning
    snapshot_reading_id INTEGER REFERENCES readings(reading_id)  -- values/lamps/siren at alert time
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_level   ON alerts (level, created_at);

-- Who was notified; escalation steps are extra rows, ordered by notified_at.
CREATE TABLE IF NOT EXISTS alert_recipients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    TEXT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    notified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipients_alert ON alert_recipients (alert_id);

CREATE TABLE IF NOT EXISTS alert_responses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id   TEXT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    reply      TEXT NOT NULL,                     -- '1','2','COMING','WATCH','FALSE ALARM'
    replied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_alert ON alert_responses (alert_id);

-- ── Handy queries ───────────────────────────────────────────────────────
-- Last signal received (offline check):  SELECT MAX(received_at) FROM readings;
-- Response time of the first responder, per alert:
--   SELECT a.alert_id,
--          MIN(strftime('%s', r.replied_at)) - strftime('%s', a.created_at) AS first_response_s
--   FROM alerts a JOIN alert_responses r USING (alert_id) GROUP BY a.alert_id;
-- Nightly false alarms:
--   SELECT COUNT(*) FROM alert_responses WHERE reply = 'FALSE ALARM' AND replied_at >= :since;
