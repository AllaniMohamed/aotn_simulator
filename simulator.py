"""simulator.py — fake Nadhir box + control panel (simplified).

Runs a Flask control page (http://localhost:5001) plus a background thread that
builds one section-5 reading every 2–5 s. Standalone by default: readings, alerts
and replies stay in memory and are visible on the control page. Tick "Send to API"
(or set API_URL) to forward everything to the dashboard API:

    POST {API_URL}/api/readings
    POST {API_URL}/api/alerts
    POST {API_URL}/api/alerts/<alert_id>/responses

    pip install flask requests
    python simulator.py              # normal scenario, standalone
    python simulator.py demo         # auto-play the full demo
    python simulator.py drift        # start directly in a scenario

Env vars: API_URL (e.g. http://localhost:8000) · SIM_PORT (5001) · ALERT_START (1)

No locks on purpose: every shared value is a single dict/list operation (atomic
under CPython's GIL), which is fine for a test tool.
"""

import itertools
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template, request

API_URL = os.getenv("API_URL", "http://localhost:8000")
PORT = int(os.getenv("SIM_PORT", 5001))
SITE_ID = "site-01"
INTERVAL = (2, 5)                    # seconds between readings

# base value, noise (std-dev), WATCH and limit thresholds
DIALS = {
    "oil_temp":     {"unit": "°C",  "base": 70.0, "noise": 0.6,  "watch": 85,  "limit": 100},
    "winding_temp": {"unit": "°C",  "base": 82.0, "noise": 0.8,  "watch": 105, "limit": 120},
    "pressure":     {"unit": "bar", "base": 1.2,  "noise": 0.03, "watch": 2.0, "limit": 2.5},
}
SCENARIOS = ["normal", "drift", "limit", "unreadable", "silent"]
ROLES_BY_LEVEL = {"WATCH": ["electrician"],
                  "INTERVENE_NOW": ["electrician", "guard"],
                  "SYSTEM_OFFLINE": ["electrician"]}
GUARD_REPLIES = ("1", "2")
ELEC_REPLIES = ("COMING", "WATCH", "FALSE ALARM")

app = Flask(__name__)
stop_demo = threading.Event()
ALERT_SEQ = itertools.count(int(os.getenv("ALERT_START", 1)))   # thread-safe IDs
state = {
    "scenario": "normal",
    "demo": False,
    "send_to_api": bool(os.getenv("API_URL")),   # False = standalone, nothing leaves here
    "api_ok": None,                              # None = no call yet
    "alerts": [],                                # local copies, newest last
    "last_reading": None,
    "last_alert_id": None,
    "alerted": False,                            # alert already sent for this scenario?
    "drift_value": DIALS["oil_temp"]["base"],
    "events": [],
}


# ── helpers ──────────────────────────────────────────────────────────────
def now_iso(offset_s=0):
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    state["events"].append({"t": now_iso(), "msg": msg})
    del state["events"][:-100]
    print(f"[{now_iso()}] {msg}", flush=True)


def post(path, payload):
    """Forward to the API when sending is on; log only on status change."""
    if not state["send_to_api"]:
        return True                              # standalone: pretend it went fine
    try:
        r = requests.post(API_URL + path, json=payload, timeout=3)
        ok = r.status_code < 300
        if not ok:
            log(f"API {path} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        ok = False
        if state["api_ok"] is not False:
            log(f"API unreachable ({e.__class__.__name__})")
    if ok and state["api_ok"] is False:
        log("API reachable again")
    state["api_ok"] = ok
    return ok


# ── readings ─────────────────────────────────────────────────────────────
def noisy(dial, value=None):
    return round((dial["base"] if value is None else value) + random.gauss(0, dial["noise"]), 2)


def build_reading():
    """One reading in the exact section-5 format, for the current scenario."""
    sc = state["scenario"]
    values = {k: noisy(d) for k, d in DIALS.items()}
    red, siren, sys_state = False, False, "NORMAL"

    if sc == "drift":
        state["drift_value"] = min(state["drift_value"] + random.uniform(1.0, 2.0),
                                   DIALS["oil_temp"]["watch"] + 1.5)
        values["oil_temp"] = round(state["drift_value"] + random.gauss(0, 0.15), 2)
        if values["oil_temp"] >= DIALS["oil_temp"]["watch"]:
            sys_state = "WATCH"
    elif sc == "limit":
        values["oil_temp"] = noisy(DIALS["oil_temp"], DIALS["oil_temp"]["limit"] + 5)
        red, siren, sys_state = True, True, "INTERVENE_NOW"

    dials = [{"dial_id": dial_id, "value": v, "unit": DIALS[dial_id]["unit"],
              "confidence": round(random.uniform(0.80, 0.97), 2), "status": "OK", "message": ""}
             for dial_id, v in values.items()]
    if sc == "unreadable":                       # never an invented value
        next(d for d in dials if d["dial_id"] == "winding_temp").update(
            value=None, confidence=round(random.uniform(0.05, 0.25), 2),
            status="REJECTED", message="check needed")
    elif sc == "normal" and random.random() < 0.03:      # rare low-confidence read
        dials[0].update(confidence=round(random.uniform(0.45, 0.60), 2), status="UNCERTAIN")

    def lamp(lamp_id, color, on):
        return {"lamp_id": lamp_id, "state": "on" if on else "off", "color": color if on else None,
                "confidence": round(random.uniform(0.85, 0.98), 2), "status": "OK"}

    return {"timestamp": now_iso(), "site_id": SITE_ID, "dials": dials,
            "lamps": [lamp("alarm_red", "red", red),
                      lamp("alarm_orange", "orange", False),
                      lamp("run_green", "green", not red)],
            "siren": {"detected": siren, "confidence": round(random.uniform(0.90, 0.99), 2)},
            "system_state": sys_state}


def send_reading():
    reading = build_reading()
    post("/api/readings", reading)
    state["last_reading"] = reading              # shown even if the API is unreachable

    if state["alerted"]:                         # one alert per scenario, on the first
        return                                   # reading that justifies it
    if state["scenario"] == "limit":
        create_alert("INTERVENE_NOW", "oil_temp above limit + alarm_red lit + siren detected")
        state["alerted"] = True
    elif state["scenario"] == "drift" and reading["system_state"] == "WATCH":
        create_alert("WATCH", "oil_temp above watch threshold")
        state["alerted"] = True


def reading_loop():
    while True:
        if state["scenario"] != "silent":        # "silent" = the box stops talking
            send_reading()
        time.sleep(random.uniform(*INTERVAL))


# ── alerts and replies ───────────────────────────────────────────────────
def create_alert(level, trigger):
    alert_id = f"A-{next(ALERT_SEQ):04d}"
    alert = {"alert_id": alert_id, "level": level, "created_at": now_iso(), "trigger": trigger,
             "recipients": [{"name": r.capitalize(), "role": r, "notified_at": now_iso(1)}
                            for r in ROLES_BY_LEVEL[level]],
             "responses": [], "outcome": None}
    post("/api/alerts", alert)
    state["last_alert_id"] = alert_id
    state["alerts"].append(alert)
    del state["alerts"][:-30]
    log(f"Alert {alert_id} ({level}): {trigger}")
    return alert_id


def send_replies(guard, electrician, guard_delay, elec_delay):
    """Schedule the guard and electrician replies to the last alert."""
    alert_id = state["last_alert_id"]
    if not alert_id:
        return False
    for role, reply, delay in (("guard", guard, guard_delay),
                               ("electrician", electrician, elec_delay)):
        if not reply:
            continue

        def run(role=role, reply=reply, delay=delay):
            time.sleep(max(0, delay))
            resp = {"role": role, "reply": reply, "replied_at": now_iso()}
            post(f"/api/alerts/{alert_id}/responses", resp)
            for a in state["alerts"]:
                if a["alert_id"] == alert_id:
                    a["responses"].append(resp)
            log(f"{alert_id}: {role} replied '{reply}'")

        threading.Thread(target=run, daemon=True).start()
    return True


# ── scenarios and demo ───────────────────────────────────────────────────
def set_scenario(name):
    state["scenario"] = name
    state["alerted"] = False
    if name != "drift":
        state["drift_value"] = DIALS["oil_temp"]["base"]
    log(f"Scenario -> {name}")


def start_demo():
    if state["demo"]:
        return False
    stop_demo.clear()
    threading.Thread(target=run_demo, daemon=True).start()
    return True


def run_demo():
    """Full walkthrough from the demo guide, no manual action needed."""
    def wait(seconds):                           # True if the demo was stopped
        return stop_demo.wait(seconds)

    state["demo"] = True
    log("Demo started")
    try:
        set_scenario("normal")
        if wait(20): return                      # green banner
        set_scenario("drift")
        if wait(50): return                      # oil temp climbs -> WATCH
        set_scenario("limit")                    # red lamp + siren -> INTERVENE_NOW
        while not state["alerted"]:
            if wait(0.5): return
        send_replies("1", "COMING", 6, 18)       # guard, then electrician
        if wait(30): return
        set_scenario("unreadable")
        if wait(20): return                      # "check needed"
        set_scenario("normal")
        if wait(10): return
        set_scenario("silent")
        if wait(45): return                      # -> SYSTEM_OFFLINE after the timeout
        set_scenario("normal")
        log("Demo finished")
    finally:
        state["demo"] = False
        stop_demo.clear()


# ── control page ─────────────────────────────────────────────────────────
@app.get("/")
def index():
    return render_template("simulator.html", scenarios=SCENARIOS)


@app.get("/state")
def get_state():
    return jsonify(scenario=state["scenario"], demo=state["demo"],
                   send_to_api=state["send_to_api"], api_ok=state["api_ok"], api_url=API_URL,
                   last_reading=state["last_reading"], last_alert_id=state["last_alert_id"],
                   alerts=state["alerts"][::-1], events=state["events"][-40:][::-1])


@app.post("/scenario")
def post_scenario():
    name = (request.get_json(silent=True) or {}).get("name")
    if name not in SCENARIOS:
        return jsonify(error="unknown scenario"), 400
    stop_demo.set()                              # manual control cancels a running demo
    time.sleep(0.05)                             # let the demo thread notice first
    set_scenario(name)
    return jsonify(ok=True)


@app.post("/mode")
def post_mode():
    on = bool((request.get_json(silent=True) or {}).get("send_to_api"))
    state["send_to_api"] = on
    state["api_ok"] = None
    log(f"Sending to API: {'ON -> ' + API_URL if on else 'OFF (standalone)'}")
    return jsonify(ok=True)


@app.post("/demo")
def post_demo():
    action = (request.get_json(silent=True) or {}).get("action")
    if action == "stop":
        stop_demo.set()
        return jsonify(ok=True)
    return jsonify(ok=start_demo())


@app.post("/alert")
def post_alert():
    d = request.get_json(silent=True) or {}
    level = d.get("level", "INTERVENE_NOW")
    if level not in ROLES_BY_LEVEL:
        return jsonify(error="unknown level"), 400
    return jsonify(alert_id=create_alert(level, d.get("trigger") or f"manual test ({level})"))


@app.post("/replies")
def post_replies():
    d = request.get_json(silent=True) or {}
    guard, elec = d.get("guard") or None, d.get("electrician") or None
    if (guard and guard not in GUARD_REPLIES) or (elec and elec not in ELEC_REPLIES):
        return jsonify(error="invalid reply"), 400
    if send_replies(guard, elec, float(d.get("guard_delay", 5)), float(d.get("elec_delay", 15))):
        return jsonify(ok=True)
    return jsonify(error="no alert yet — create one first"), 409


if __name__ == "__main__":
    threading.Thread(target=reading_loop, daemon=True).start()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "demo":
        start_demo()
    elif arg in SCENARIOS:
        set_scenario(arg)
    elif arg:
        sys.exit(f"unknown argument '{arg}' — expected 'demo' or one of {SCENARIOS}")
    mode = f"sending to {API_URL}" if state["send_to_api"] else "standalone (nothing sent)"
    print(f"Nadhir simulator · control page http://localhost:{PORT} · {mode}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)  # reloader would run the loop twice
