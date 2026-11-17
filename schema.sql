-- Nadhir dashboard — SQLite schema
-- Load with:  sqlite3 nadhir.db < schema.sql   (or executescript() from Python)
-- Timestamps are stored as ISO-8601 UTC text ("2026-11-20T02:14:05Z"):
-- they sort correctly as text and match the API format exactly.

PRAGMA foreign_keys = ON;

-- ───────────── Configuration (Settings screen) ─────────────

-- One row per dial or lamp. Dials carry their thresholds.
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   TEXT PRIMARY KEY,                       -- "oil_temp", "alarm_red"
    site_id         TEXT NOT NULL DEFAULT 'site-01',
    kind            TEXT NOT NULL CHECK (kind IN ('dial','lamp')),
    label           TEXT NOT NULL,                          -- display name
    unit            TEXT,                                   -- dials only
    watch_threshold REAL,                                   -- dials only
    limit_threshold REAL,                                   -- dials only
    max_rise_per_min REAL                                   -- dials only, NULL = no rate rule
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN
                      ('electrician','guard','maintenance_manager','plant_manager')),
    phone             TEXT NOT NULL,
    escalation_order  INTEGER                               -- 1 = first called, NULL = not in the chain
);

-- Key/value: escalation_delay_s (300), offline_timeout_s (30), ...
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('escalation_delay_s', '300'),
    ('offline_timeout_s',  '30');

-- ───────────── Measurements (Overview + Instrument detail) ─────────────

-- One row per received reading (= one POST). Siren lives here: it is 1:1 with a reading.
CREATE TABLE IF NOT EXISTS readings (
    reading_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id           TEXT NOT NULL,
    ts                TEXT NOT NULL,                        -- device timestamp
    received_at       TEXT NOT NULL,                        -- server clock, used for "last signal"
    system_state      TEXT NOT NULL CHECK (system_state IN
                      ('NORMAL','WATCH','INTERVENE_NOW','SYSTEM_OFFLINE')),
    siren_detected    INTEGER NOT NULL CHECK (siren_detected IN (0,1)),
    siren_confidence  REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_site_ts ON readings (site_id, ts);

CREATE TABLE IF NOT EXISTS dial_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id  INTEGER NOT NULL REFERENCES readings(reading_id) ON DELETE CASCADE,
    dial_id     TEXT NOT NULL REFERENCES instruments(instrument_id),
    value       REAL,                                       -- NULL when REJECTED, never invented
    unit        TEXT,
    confidence  REAL CHECK (confidence BETWEEN 0 AND 1),
    status      TEXT NOT NULL CHECK (status IN ('OK','UNCERTAIN','REJECTED')),
    message     TEXT NOT NULL DEFAULT ''                    -- "check needed" when REJECTED
);
-- Curves: one dial over time. "Latest unreadable readings": status filter.
CREATE INDEX IF NOT EXISTS idx_dial_readings_dial ON dial_readings (dial_id, reading_id);
CREATE INDEX IF NOT EXISTS idx_dial_readings_status ON dial_readings (status, dial_id);

CREATE TABLE IF NOT EXISTS lamp_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id  INTEGER NOT NULL REFERENCES readings(reading_id) ON DELETE CASCADE,
    lamp_id     TEXT NOT NULL REFERENCES instruments(instrument_id),
    state       TEXT NOT NULL CHECK (state IN ('on','off')),
    color       TEXT CHECK (color IN ('red','orange','green') OR color IS NULL),
    confidence  REAL CHECK (confidence BETWEEN 0 AND 1),
    status      TEXT NOT NULL CHECK (status IN ('OK','UNCERTAIN','REJECTED'))
);
CREATE INDEX IF NOT EXISTS idx_lamp_readings_reading ON lamp_readings (reading_id);

-- "Every state change is recorded with its time."
CREATE TABLE IF NOT EXISTS state_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    old_state  TEXT,                                        -- NULL for the very first state
    new_state  TEXT NOT NULL,
    reason     TEXT                                         -- the trigger string from rules.py
);
CREATE INDEX IF NOT EXISTS idx_state_changes_site ON state_changes (site_id, changed_at);

-- ───────────── Alerts (Alert log, Alert detail, Morning review) ─────────────

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            TEXT PRIMARY KEY,                   -- "A-0007"
    site_id             TEXT NOT NULL,
    level               TEXT NOT NULL CHECK (level IN ('WATCH','INTERVENE_NOW','SYSTEM_OFFLINE')),
    created_at          TEXT NOT NULL,
    trigger             TEXT NOT NULL,
    outcome             TEXT,                               -- filled the next morning
    snapshot_reading_id INTEGER REFERENCES readings(reading_id)  -- values/lamps/siren at alert time
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_level   ON alerts (level, created_at);

-- Who was notified. Escalation steps (voice call, managers) are extra rows.
CREATE TABLE IF NOT EXISTS alert_recipients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     TEXT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL,
    notified_at  TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'sms' CHECK (channel IN ('sms','call')),
    step         INTEGER NOT NULL DEFAULT 0                 -- 0 = first wave, 1+ = escalation
);
CREATE INDEX IF NOT EXISTS idx_recipients_alert ON alert_recipients (alert_id);

CREATE TABLE IF NOT EXISTS alert_responses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    TEXT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    reply       TEXT NOT NULL,                              -- '1','2','COMING','WATCH','FALSE ALARM'
    replied_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_alert ON alert_responses (alert_id, replied_at);

-- ───────────── Handy queries ─────────────
-- Response time of the first responder, per alert:
--   SELECT a.alert_id,
--          MIN(strftime('%s', r.replied_at)) - strftime('%s', a.created_at) AS first_response_s
--   FROM alerts a LEFT JOIN alert_responses r USING (alert_id) GROUP BY a.alert_id;
--
-- Morning review (last night's false alarms):
--   SELECT COUNT(*) FROM alert_responses WHERE reply = 'FALSE ALARM' AND replied_at >= :since;
