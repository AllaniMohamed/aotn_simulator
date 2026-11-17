"""api.py — Nadhir dashboard API (Flask).

Replaces the teammates' FastAPI draft: same job, but everything is stored in the
normalized schema.sql (readings + instrument_readings per dial/lamp, state_changes,
alerts + recipients + responses — no more raw_json blobs), the section-5 format is
validated "to the letter", and every screen of the requirements is served:

    POST   /api/readings                       receive a reading (validated, normalized)
    GET    /api/readings?limit=20              list readings (exact section-5 shape, newest first)
    GET    /api/readings?dial=oil_temp&limit=200    curve points for one instrument
    GET    /api/readings?status=REJECTED       latest unreadable readings ("check needed")
    GET    /api/state                          overall state; SYSTEM_OFFLINE once no signal
                                               has arrived for offline_timeout_s (default 30 s)
    POST   /api/alerts                         create an alert (+ recipients; the latest reading
                                               is auto-saved as the snapshot for the detail screen)
    GET    /api/alerts?level=&since=&until=    alert log with filters
    GET    /api/alerts/<alert_id>              alert detail (recipients, replies, snapshot)
    POST   /api/alerts/<alert_id>/responses    add a reply (guard '1'/'2', electrician COMING/WATCH/FALSE ALARM)
    PATCH  /api/alerts/<alert_id>              fill in the outcome the next morning
    GET    /api/review?since=…                 morning review (totals, avg first response, false alarms)
    GET    /api/config                         instruments, contacts and settings

The teammates' old paths /readings and /state keep working without the /api prefix.
Replaying an alert WITH its responses is allowed (the PDF example loads as-is);
normally replies arrive one by one through the /responses route.

    pip install flask
    python api.py                       # http://localhost:8000
Env vars: API_PORT (8000) · NADHIR_DB (nadhir.db)
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, request

DB_FILE = os.getenv("NADHIR_DB", "nadhir.db")
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
PORT = int(os.getenv("API_PORT", "8080"))

if not os.path.exists(SCHEMA_FILE):
    sys.exit("schema.sql must sit next to api.py — take it from the team repository")

STATES = ("NORMAL", "WATCH", "INTERVENE_NOW", "SYSTEM_OFFLINE")
STATUSES = ("OK", "UNCERTAIN", "REJECTED")
LEVELS = ("WATCH", "INTERVENE_NOW", "SYSTEM_OFFLINE")
ROLES = ("electrician", "guard", "maintenance_manager", "plant_manager")
REPLIES = {"guard": ("1", "2"), "electrician": ("COMING", "WATCH", "FALSE ALARM")}

# section-5 shapes: every key required, no extra keys, values checked by rule
READING_SPEC = {"timestamp": "iso", "site_id": "str", "dials": "list", "lamps": "list",
                "siren": "obj", "system_state": ("enum", STATES)}
DIAL_SPEC = {"dial_id": "str", "value": "num?", "unit": "str", "confidence": "conf",
             "status": ("enum", STATUSES), "message": "text"}
LAMP_SPEC = {"lamp_id": "str", "state": ("enum", ("on", "off")),
             "color": ("enum", ("red", "orange", "green", None)),
             "confidence": "conf", "status": ("enum", STATUSES)}
SIREN_SPEC = {"detected": "bool", "confidence": "conf"}
ALERT_SPEC = {"alert_id": "str", "level": ("enum", LEVELS), "created_at": "iso", "trigger": "str",
              "recipients": "list", "responses": "list", "outcome": "null"}
RECIPIENT_SPEC = {"name": "str", "role": ("enum", ROLES), "notified_at": "iso"}
RESPONSE_SPEC = {"role": ("enum", tuple(REPLIES)), "reply": "text", "replied_at": "iso"}

# mirrors simulator.py's DIALS, so the Settings screen shows thresholds out of the box
SEED_INSTRUMENTS = [
    ("oil_temp", "dial", "Oil temperature", "°C", 85, 100, 2.0),
    ("winding_temp", "dial", "Winding temperature", "°C", 105, 120, 2.0),
    ("pressure", "dial", "Pressure", "bar", 2.0, 2.5, None),
    ("alarm_red", "lamp", "Red alarm lamp", None, None, None, None),
    ("alarm_orange", "lamp", "Orange alarm lamp", None, None, None, None),
    ("run_green", "lamp", "Green run lamp", None, None, None, None),
]

app = Flask(__name__)


# ── database ─────────────────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect(DB_FILE)
    try:
        db.executescript(open(SCHEMA_FILE).read())            # idempotent (IF NOT EXISTS)
        db.executemany("INSERT OR IGNORE INTO instruments VALUES (?,?,?,?,?,?,?)", SEED_INSTRUMENTS)
        db.commit()
    finally:
        db.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ── validation ("follow the format to the letter") ───────────────────────
def is_iso(v):
    if not isinstance(v, str) or not v.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def bad(obj, spec, path):
    """Error string when obj doesn't match spec exactly, else None. Rules:
    'iso' timestamp · 'conf' 0..1 · 'num'/'num?' number · 'bool' · 'str' non-empty ·
    'text' any string · 'list' · 'obj' · 'null' · ('enum', allowed values)."""
    if not isinstance(obj, dict):
        return f"{path} must be an object"
    if set(obj) != set(spec):
        return (f"{path}: missing {sorted(set(spec) - set(obj))} / "
                f"unexpected {sorted(set(obj) - set(spec))}")
    for k, rule in spec.items():
        v = obj[k]
        if rule == "iso":
            ok = is_iso(v)
        elif rule == "conf":
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
        elif rule == "num":
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        elif rule == "num?":
            ok = v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
        elif rule == "bool":
            ok = isinstance(v, bool)
        elif rule == "str":
            ok = isinstance(v, str) and bool(v)
        elif rule == "text":
            ok = isinstance(v, str)
        elif rule == "list":
            ok = isinstance(v, list)
        elif rule == "obj":
            ok = isinstance(v, dict)
        elif rule == "null":
            ok = v is None
        else:                                          # ('enum', allowed values)
            ok = v in rule[1]
        if not ok:
            return f"{path}.{k}: invalid value (rule: {rule})"
    return None


def reading_problems(r):
    e = bad(r, READING_SPEC, "reading")
    if e:
        return [e]
    problems = []
    for i, d in enumerate(r["dials"]):
        e = bad(d, DIAL_SPEC, f"dials[{i}]")
        if e:
            problems.append(e)
        elif d["status"] == "REJECTED" and (d["value"] is not None or d["message"] != "check needed"):
            problems.append(f"dials[{i}]: a REJECTED dial must carry value null and message "
                            f"'check needed' — never an invented value")
        elif d["status"] != "REJECTED" and d["value"] is None:
            problems.append(f"dials[{i}]: value must be a number unless the dial is REJECTED")
    for i, lamp in enumerate(r["lamps"]):
        e = bad(lamp, LAMP_SPEC, f"lamps[{i}]")
        if e:
            problems.append(e)
        elif (lamp["color"] is None) != (lamp["state"] == "off"):
            problems.append(f"lamps[{i}]: color must be null exactly when the lamp is off")
    e = bad(r["siren"], SIREN_SPEC, "siren")
    if e:
        problems.append(e)
    return problems


def response_problems(x, path="response"):
    e = bad(x, RESPONSE_SPEC, path)
    if e:
        return [e]
    if x["reply"] not in REPLIES[x["role"]]:
        return [f"{path}.reply: role '{x['role']}' must reply one of {REPLIES[x['role']]}"]
    return []


def alert_problems(a):
    e = bad(a, ALERT_SPEC, "alert")
    if e:
        return [e]
    problems = []
    for i, rec in enumerate(a["recipients"]):
        e = bad(rec, RECIPIENT_SPEC, f"recipients[{i}]")
        if e:
            problems.append(e)
    for i, x in enumerate(a["responses"]):
        problems.extend(response_problems(x, f"responses[{i}]"))
    return problems


# ── small helpers ────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(status, msg):
    return jsonify(error=msg), status


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def setting(db, key, default):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def to_reading(row, children):
    """Rebuild the exact section-5 JSON from normalized rows."""
    dials, lamps = [], []
    for c in children:
        if c["state"] is not None:                     # lamp row
            lamps.append({"lamp_id": c["instrument_id"], "state": c["state"], "color": c["color"],
                          "confidence": c["confidence"], "status": c["status"]})
        else:                                          # dial row
            dials.append({"dial_id": c["instrument_id"], "value": c["value"], "unit": c["unit"],
                          "confidence": c["confidence"], "status": c["status"], "message": c["message"]})
    return {"timestamp": row["ts"], "site_id": row["site_id"], "dials": dials, "lamps": lamps,
            "siren": {"detected": bool(row["siren_detected"]), "confidence": row["siren_confidence"]},
            "system_state": row["system_state"]}


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"  # the dashboard calls us from a browser
    return resp


# ── readings ─────────────────────────────────────────────────────────────
@app.post("/api/readings")
@app.post("/readings")
def receive_reading():
    r = body()
    if r is None:
        return fail(400, "expected a JSON body")
    problems = reading_problems(r)
    if problems:
        return jsonify(errors=problems), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO readings (site_id, ts, received_at, system_state, siren_detected, siren_confidence) "
        "VALUES (?,?,?,?,?,?)",
        (r["site_id"], r["timestamp"], now_iso(), r["system_state"],
         int(r["siren"]["detected"]), r["siren"]["confidence"]))
    reading_id = cur.lastrowid
    for d in r["dials"]:
        db.execute("INSERT INTO instrument_readings (reading_id, instrument_id, status, confidence, "
                   "value, unit, message) VALUES (?,?,?,?,?,?,?)",
                   (reading_id, d["dial_id"], d["status"], d["confidence"],
                    d["value"], d["unit"], d["message"]))
    for lamp in r["lamps"]:
        db.execute("INSERT INTO instrument_readings (reading_id, instrument_id, status, confidence, "
                   "value, unit, message, state, color) VALUES (?,?,?,?,?,?,?,?,?)",
                   (reading_id, lamp["lamp_id"], lamp["status"], lamp["confidence"],
                    None, None, "", lamp["state"], lamp["color"]))
    prev = db.execute("SELECT system_state FROM readings WHERE reading_id < ? ORDER BY reading_id DESC LIMIT 1",
                      (reading_id,)).fetchone()
    if prev is None or prev["system_state"] != r["system_state"]:   # record every state change
        db.execute("INSERT INTO state_changes (site_id, changed_at, old_state, new_state) VALUES (?,?,?,?)",
                   (r["site_id"], now_iso(), prev["system_state"] if prev else None, r["system_state"]))
    db.commit()
    return jsonify(ok=True, reading_id=reading_id), 201


@app.get("/api/readings")
@app.get("/readings")
def list_readings():
    limit = clamp(request.args.get("limit", 20, type=int) or 20, 1, 500)
    db = get_db()
    dial, status = request.args.get("dial"), request.args.get("status")
    if dial:                                          # curve for the instrument-detail screen
        pts = db.execute("SELECT r.ts, i.value, i.status, i.confidence, i.state, i.color "
                         "FROM instrument_readings i JOIN readings r USING (reading_id) "
                         "WHERE i.instrument_id = ? ORDER BY r.reading_id DESC LIMIT ?",
                         (dial, limit)).fetchall()
        points = [{"ts": p["ts"], "value": p["value"], "status": p["status"],
                   "confidence": p["confidence"], "state": p["state"], "color": p["color"]}
                  for p in reversed(pts)]
        return jsonify(dial=dial, points=points)
    if status:                                        # e.g. REJECTED → "latest unreadable readings"
        rows = db.execute("SELECT r.ts, i.instrument_id, i.status, i.message "
                          "FROM instrument_readings i JOIN readings r USING (reading_id) "
                          "WHERE i.status = ? ORDER BY r.reading_id DESC LIMIT ?",
                          (status, limit)).fetchall()
        return jsonify(unreadable=[dict(x) for x in rows])
    latest = db.execute("SELECT * FROM readings ORDER BY reading_id DESC LIMIT ?", (limit,)).fetchall()
    ids = [row["reading_id"] for row in latest]
    children = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for c in db.execute(f"SELECT * FROM instrument_readings WHERE reading_id IN ({marks}) ORDER BY id", ids):
            children.setdefault(c["reading_id"], []).append(c)
    return jsonify(readings=[to_reading(row, children.get(row["reading_id"], [])) for row in latest])


@app.get("/api/state")
@app.get("/state")
def overall_state():
    """Last reported state, or SYSTEM_OFFLINE when no signal arrived within the timeout."""
    db = get_db()
    row = db.execute("SELECT site_id, received_at, system_state FROM readings "
                     "ORDER BY reading_id DESC LIMIT 1").fetchone()
    timeout = int(setting(db, "offline_timeout_s", "30"))
    if row is None:
        return jsonify(system_state=None, last_reported_state=None, last_signal_at=None,
                       offline_timeout_s=timeout)
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
    offline = age.total_seconds() > timeout
    return jsonify(site_id=row["site_id"], last_reported_state=row["system_state"],
                   system_state="SYSTEM_OFFLINE" if offline else row["system_state"],
                   last_signal_at=row["received_at"], offline_timeout_s=timeout,
                   seconds_since_signal=round(age.total_seconds(), 1))


# ── alerts ───────────────────────────────────────────────────────────────
@app.post("/api/alerts")
def create_alert():
    a = body()
    if a is None:
        return fail(400, "expected a JSON body")
    problems = alert_problems(a)
    if problems:
        return jsonify(errors=problems), 400
    db = get_db()
    if db.execute("SELECT 1 FROM alerts WHERE alert_id = ?", (a["alert_id"],)).fetchone():
        return fail(409, f"alert {a['alert_id']} already exists")
    last = db.execute("SELECT site_id, reading_id AS id FROM readings ORDER BY reading_id DESC LIMIT 1").fetchone()
    site_id = last["site_id"] if last else "site-01"
    snapshot = last["id"] if last else None             # values/lamps/siren at alert time
    db.execute("INSERT INTO alerts (alert_id, site_id, level, created_at, trigger, outcome, snapshot_reading_id) "
               "VALUES (?,?,?,?,?,NULL,?)",
               (a["alert_id"], site_id, a["level"], a["created_at"], a["trigger"], snapshot))
    for rec in a["recipients"]:
        db.execute("INSERT INTO alert_recipients (alert_id, name, role, notified_at) VALUES (?,?,?,?)",
                   (a["alert_id"], rec["name"], rec["role"], rec["notified_at"]))
    for x in a["responses"]:                           # only used when replaying an alert
        db.execute("INSERT INTO alert_responses (alert_id, role, reply, replied_at) VALUES (?,?,?,?)",
                   (a["alert_id"], x["role"], x["reply"], x["replied_at"]))
    db.commit()
    return jsonify(ok=True, snapshot_reading_id=snapshot), 201


@app.get("/api/alerts")
def list_alerts():
    db = get_db()
    where, args = [], []
    for param, op in (("level", "="), ("since", ">="), ("until", "<=")):
        value = request.args.get(param)
        if not value:
            continue
        if param == "level" and value not in LEVELS:
            return fail(400, f"level must be one of {LEVELS}")
        if param != "level" and not is_iso(value):
            return fail(400, f"{param} must be ISO-8601 UTC (…Z)")
        column = "a.level" if param == "level" else "a.created_at"
        where.append(f"{column} {op} ?")
        args.append(value)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    limit = clamp(request.args.get("limit", 100, type=int) or 100, 1, 500)
    rows = db.execute(
        "SELECT a.*, "
        "(SELECT role FROM alert_responses r WHERE r.alert_id = a.alert_id ORDER BY replied_at LIMIT 1) AS first_responder, "
        "(SELECT MIN(strftime('%s', r.replied_at)) - strftime('%s', a.created_at) FROM alert_responses r "
        " WHERE r.alert_id = a.alert_id) AS first_response_s, "
        "(SELECT COUNT(*) FROM alert_responses r WHERE r.alert_id = a.alert_id) AS reply_count "
        f"FROM alerts a{cond} ORDER BY a.created_at DESC LIMIT ?", (*args, limit)).fetchall()
    return jsonify(alerts=[dict(x) for x in rows])


@app.get("/api/alerts/<alert_id>")
def alert_detail(alert_id):
    db = get_db()
    a = db.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    if a is None:
        return fail(404, f"alert {alert_id} not found")
    recipients = [dict(x) for x in db.execute("SELECT name, role, notified_at FROM alert_recipients "
                                              "WHERE alert_id = ? ORDER BY notified_at", (alert_id,))]
    responses = [dict(x) for x in db.execute("SELECT role, reply, replied_at FROM alert_responses "
                                             "WHERE alert_id = ? ORDER BY replied_at", (alert_id,))]
    snapshot = None
    if a["snapshot_reading_id"]:
        srow = db.execute("SELECT * FROM readings WHERE reading_id = ?", (a["snapshot_reading_id"],)).fetchone()
        if srow:
            children = db.execute("SELECT * FROM instrument_readings WHERE reading_id = ? ORDER BY id",
                                  (a["snapshot_reading_id"],)).fetchall()
            snapshot = to_reading(srow, children)
    first = db.execute("SELECT MIN(strftime('%s', replied_at)) - strftime('%s', ?) FROM alert_responses "
                       "WHERE alert_id = ?", (a["created_at"], alert_id)).fetchone()[0]
    return jsonify(**dict(a), recipients=recipients, responses=responses,
                   snapshot=snapshot, first_response_s=first)


@app.post("/api/alerts/<alert_id>/responses")
def add_response(alert_id):
    x = body()
    if x is None:
        return fail(400, "expected a JSON body")
    problems = response_problems(x)
    if problems:
        return jsonify(errors=problems), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone():
        return fail(404, f"alert {alert_id} not found")
    db.execute("INSERT INTO alert_responses (alert_id, role, reply, replied_at) VALUES (?,?,?,?)",
               (alert_id, x["role"], x["reply"], x["replied_at"]))
    db.commit()
    return jsonify(ok=True), 201


@app.patch("/api/alerts/<alert_id>")
def set_outcome(alert_id):
    x = body()
    if x is None or not isinstance(x.get("outcome"), str):
        return fail(400, 'expected {"outcome": "…"}')
    db = get_db()
    cur = db.execute("UPDATE alerts SET outcome = ? WHERE alert_id = ?", (x["outcome"], alert_id))
    db.commit()
    if cur.rowcount == 0:
        return fail(404, f"alert {alert_id} not found")
    return jsonify(ok=True)


@app.get("/api/review")
def morning_review():
    db = get_db()
    since = request.args.get("since")
    if since and not is_iso(since):
        return fail(400, "since must be ISO-8601 UTC (…Z)")
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_level = {row["level"]: row["n"] for row in db.execute(
        "SELECT level, COUNT(*) AS n FROM alerts WHERE created_at >= ? GROUP BY level", (since,))}
    avg = db.execute("SELECT AVG(t) FROM (SELECT MIN(strftime('%s', r.replied_at)) - strftime('%s', a.created_at) AS t "
                     "FROM alerts a JOIN alert_responses r ON r.alert_id = a.alert_id "
                     "WHERE a.created_at >= ? GROUP BY a.alert_id)", (since,)).fetchone()[0]
    false_alarms = db.execute("SELECT COUNT(*) FROM alert_responses WHERE reply = 'FALSE ALARM' AND replied_at >= ?",
                              (since,)).fetchone()[0]
    return jsonify(since=since, alerts_total=sum(by_level.values()), by_level=by_level,
                   avg_first_response_s=round(avg, 1) if avg is not None else None,
                   false_alarms=false_alarms)


# ── configuration (Settings screen) ──────────────────────────────────────
@app.get("/api/config")
def get_config():
    db = get_db()
    return jsonify(
        instruments=[dict(x) for x in db.execute("SELECT * FROM instruments ORDER BY kind, instrument_id")],
        contacts=[dict(x) for x in db.execute("SELECT * FROM contacts ORDER BY escalation_order")],
        settings={row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")})


if __name__ == "__main__":
    print(f"Nadhir API · http://localhost:{PORT} · db: {DB_FILE}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)