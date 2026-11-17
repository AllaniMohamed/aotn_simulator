"""simulator.py — fake Nadhir box + control panel.

Runs a small Flask server (control page on http://localhost:5001) and a background
thread that POSTs a reading to the dashboard's API every 2–5 s, in the exact
format of section 5 of the requirements.

    pip install flask requests
    python simulator.py              # start in the "normal" scenario
    python simulator.py demo         # auto-play the full demo (see run_demo)
    python simulator.py drift        # start directly in a scenario

STANDALONE BY DEFAULT: nothing is sent anywhere. Readings, alerts and replies stay in
memory and are visible on the control page. To forward them to the dashboard's API,
set API_URL (or tick "Send to API" on the control page).

Env vars:  API_URL (enables sending; e.g. http://localhost:8000)   SIM_PORT (5001)   ALERT_START (1)

The API routes it calls when sending is on (rename them below if yours differ):
    POST {API_URL}/api/readings
    POST {API_URL}/api/alerts
    POST {API_URL}/api/alerts/<alert_id>/responses
"""

import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template, request

API_URL = os.getenv("API_URL", "http://localhost:8000")
SEND_BY_DEFAULT = bool(os.getenv("API_URL"))      # no API_URL -> standalone
PORT = int(os.getenv("SIM_PORT", 5001))
SITE_ID = "site-01"
INTERVAL = (2, 5)                           # seconds between readings

# Dials: base value, noise (std-dev), WATCH and limit thresholds
DIALS = {
    "oil_temp":     {"unit": "°C",  "base": 70.0, "noise": 0.6,  "watch": 85,  "limit": 100},
    "winding_temp": {"unit": "°C",  "base": 82.0, "noise": 0.8,  "watch": 105, "limit": 120},
    "pressure":     {"unit": "bar", "base": 1.2,  "noise": 0.03, "watch": 2.0, "limit": 2.5},
}
SCENARIOS = ["normal", "drift", "limit", "unreadable", "silent"]
ROLES_BY_LEVEL = {
    "WATCH": ["electrician"],
    "INTERVENE_NOW": ["electrician", "guard"],
    "SYSTEM_OFFLINE": ["electrician"],
}
NAMES = {"electrician": "Electrician", "guard": "Guard"}
GUARD_REPLIES = {"1", "2"}
ELEC_REPLIES = {"COMING", "WATCH", "FALSE ALARM"}

app = Flask(__name__)
lock = threading.RLock()
stop_demo = threading.Event()
state = {
    "scenario": "normal",
    "demo": False,
    "send_to_api": SEND_BY_DEFAULT,         # False = standalone, nothing leaves this process
    "api_ok": None,                         # None = no call yet
    "alerts": [],                           # local copy of alerts + replies (newest last)
    "last_reading": None,
    "last_alert_id": None,
    "alerted": False,                       # alert already sent for the current scenario?
    "oil_drift": DIALS["oil_temp"]["base"],
    "alert_seq": int(os.getenv("ALERT_START", 1)),
    "events": [],
}


# ───────────── helpers ─────────────
def now_iso(offset_s=0):
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    with lock:
        state["events"].append({"t": now_iso(), "msg": msg})
        del state["events"][:-100]
    print(f"[{now_iso()}] {msg}")


def post(path, payload):
    """POST to the API (only if enabled); log only when the connection status changes."""
    if not state["send_to_api"]:
        return True                                  # standalone: pretend it went fine
    try:
        r = requests.post(API_URL + path, json=payload, timeout=3)
        ok = r.status_code < 300
        if not ok:
            log(f"API {path} -> HTTP {r.status_code}: {r.text[:120]}")
    except requests.RequestException as e:
        ok = False
        if state["api_ok"] is not False:
            log(f"API unreachable ({e.__class__.__name__})")
    with lock:
        if ok and state["api_ok"] is False:
            log("API reachable again")
        state["api_ok"] = ok
    return ok


# ───────────── readings ─────────────
def noisy(d, value=None):
    return round((d["base"] if value is None else value) + random.gauss(0, d["noise"]), 2)


def build_reading():
    """Build one reading for the current scenario."""
    sc = state["scenario"]
    values = {k: noisy(d) for k, d in DIALS.items()}
    red = False
    siren = False
    sys_state = "NORMAL"

    if sc == "drift":
        watch = DIALS["oil_temp"]["watch"]
        state["oil_drift"] = min(state["oil_drift"] + random.uniform(1.0, 2.0), watch + 1.5)
        values["oil_temp"] = round(state["oil_drift"] + random.gauss(0, 0.15), 2)
        if values["oil_temp"] >= watch:
            sys_state = "WATCH"
    elif sc == "limit":
        values["oil_temp"] = noisy(DIALS["oil_temp"], DIALS["oil_temp"]["limit"] + 5)
        red, siren, sys_state = True, True, "INTERVENE_NOW"

    dials = []
    for dial_id, v in values.items():
        d = DIALS[dial_id]
        item = {"dial_id": dial_id, "value": v, "unit": d["unit"],
                "confidence": round(random.uniform(0.80, 0.97), 2), "status": "OK", "message": ""}
        if sc == "unreadable" and dial_id == "winding_temp":
            item.update(value=None, confidence=round(random.uniform(0.05, 0.25), 2),
                        status="REJECTED", message="check needed")
        elif sc == "normal" and random.random() < 0.03:     # rare low-confidence read
            item.update(confidence=round(random.uniform(0.45, 0.6), 2), status="UNCERTAIN")
        dials.append(item)

    def lamp(lamp_id, color, on):
        return {"lamp_id": lamp_id, "state": "on" if on else "off",
                "color": color if on else None,
                "confidence": round(random.uniform(0.85, 0.98), 2), "status": "OK"}

    return {
        "timestamp": now_iso(),
        "site_id": SITE_ID,
        "dials": dials,
        "lamps": [lamp("alarm_red", "red", red),
                  lamp("alarm_orange", "orange", False),
                  lamp("run_green", "green", not red)],
        "siren": {"detected": siren, "confidence": round(random.uniform(0.90, 0.99), 2)},
        "system_state": sys_state,
    }


# ───────────── alerts and replies ─────────────
def create_alert(level, trigger):
    with lock:
        alert_id = f"A-{state['alert_seq']:04d}"
        state["alert_seq"] += 1
    payload = {
        "alert_id": alert_id,
        "level": level,
        "created_at": now_iso(),
        "trigger": trigger,
        "recipients": [{"name": NAMES[r], "role": r, "notified_at": now_iso(1)}
                       for r in ROLES_BY_LEVEL[level]],
        "responses": [],
        "outcome": None,
    }
    post("/api/alerts", payload)
    with lock:
        state["last_alert_id"] = alert_id
        state["alerts"].append(payload)
        del state["alerts"][:-30]
    log(f"Alert {alert_id} created ({level}): {trigger}")
    return alert_id


def send_replies(guard, electrician, guard_delay, elec_delay):
    """Send replies to the last alert, each after its own delay (seconds)."""
    alert_id = state["last_alert_id"]
    if not alert_id:
        return False
    plan = sorted([("guard", guard, guard_delay), ("electrician", electrician, elec_delay)],
                  key=lambda x: x[2])

    def run():
        t0 = time.time()
        for role, reply, delay in plan:
            if not reply:
                continue
            time.sleep(max(0, delay - (time.time() - t0)))
            resp = {"role": role, "reply": reply, "replied_at": now_iso()}
            post(f"/api/alerts/{alert_id}/responses", resp)
            with lock:
                for a in state["alerts"]:
                    if a["alert_id"] == alert_id:
                        a["responses"].append(resp)
            log(f"{alert_id}: {role} replied '{reply}'")

    threading.Thread(target=run, daemon=True).start()
    return True


# ───────────── scenarios and demo ─────────────
def set_scenario(name):
    with lock:
        state["scenario"] = name
        state["alerted"] = False
        if name != "drift":
            state["oil_drift"] = DIALS["oil_temp"]["base"]
    log(f"Scenario -> {name}")


def send_reading():
    reading = build_reading()
    if post("/api/readings", reading):
        with lock:
            state["last_reading"] = reading
    else:
        with lock:
            state["last_reading"] = reading          # still show it in the control page

    # Alert once per scenario, after the first reading that justifies it
    with lock:
        sc, alerted = state["scenario"], state["alerted"]
    if not alerted and sc in ("drift", "limit"):
        if sc == "limit":
            create_alert("INTERVENE_NOW", "oil_temp above limit + alarm_red lit + siren detected")
            with lock:
                state["alerted"] = True
        elif reading["system_state"] == "WATCH":
            create_alert("WATCH", "oil_temp above watch threshold")
            with lock:
                state["alerted"] = True


def reading_loop():
    while True:
        if state["scenario"] != "silent":            # "silent" = the box stops talking
            send_reading()
        time.sleep(random.uniform(*INTERVAL))


def run_demo():
    """Full walkthrough from the Overview & Demo Guide, no manual action needed."""
    def wait(seconds):                               # True if the demo was stopped
        return stop_demo.wait(seconds)

    with lock:
        state["demo"] = True
    log("Demo started")
    try:
        set_scenario("normal")
        if wait(20): return                          # green banner
        set_scenario("drift")
        if wait(50): return                          # temperature climbs -> WATCH
        set_scenario("limit")                        # red lamp + siren -> INTERVENE_NOW
        while not state["alerted"]:
            if wait(0.5): return
        send_replies("1", "COMING", 6, 18)           # guard "1", then electrician "COMING"
        if wait(30): return
        set_scenario("unreadable")                   # "check needed"
        if wait(20): return
        set_scenario("normal")
        if wait(10): return
        set_scenario("silent")                       # -> SYSTEM_OFFLINE after the timeout
        if wait(45): return
        set_scenario("normal")
        log("Demo finished")
    finally:
        with lock:
            state["demo"] = False
        stop_demo.clear()


def start_demo():
    if state["demo"]:
        return False
    stop_demo.clear()
    threading.Thread(target=run_demo, daemon=True).start()
    return True


# ───────────── control page ─────────────
@app.get("/")
def index():
    return render_template("simulator.html", scenarios=SCENARIOS)


@app.get("/state")
def get_state():
    with lock:
        return jsonify({**state, "events": state["events"][-40:][::-1],
                        "alerts": state["alerts"][::-1],
                        "api_url": API_URL, "thresholds": DIALS})


@app.post("/scenario")
def post_scenario():
    name = (request.get_json(silent=True) or {}).get("name")
    if name not in SCENARIOS:
        return jsonify(error="unknown scenario"), 400
    stop_demo.set()                                  # manual control cancels a running demo
    time.sleep(0.05)
    set_scenario(name)
    return jsonify(ok=True)


@app.post("/mode")
def post_mode():
    on = bool((request.get_json(silent=True) or {}).get("send_to_api"))
    with lock:
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
    data = request.get_json(silent=True) or {}
    level = data.get("level", "INTERVENE_NOW")
    if level not in ROLES_BY_LEVEL:
        return jsonify(error="unknown level"), 400
    trigger = data.get("trigger") or f"manual test ({level})"
    return jsonify(alert_id=create_alert(level, trigger))


@app.post("/replies")
def post_replies():
    d = request.get_json(silent=True) or {}
    guard, elec = d.get("guard") or None, d.get("electrician") or None
    if (guard and guard not in GUARD_REPLIES) or (elec and elec not in ELEC_REPLIES):
        return jsonify(error="invalid reply"), 400
    ok = send_replies(guard, elec, float(d.get("guard_delay", 5)), float(d.get("elec_delay", 15)))
    return (jsonify(ok=True) if ok else (jsonify(error="no alert yet — create one first"), 409))


if __name__ == "__main__":
    threading.Thread(target=reading_loop, daemon=True).start()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "demo":
        start_demo()
    elif arg in SCENARIOS:
        set_scenario(arg)
    # use_reloader=False: the reloader would start the reading thread twice
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)