"""rules.py — decides the substation state. No display, no database, no network.

evaluate(...) -> Result(state, trigger)

States (priority order, requirements §4):
    SYSTEM_OFFLINE  no signal received for more than `offline_timeout_s`
    INTERVENE_NOW   a dial reached its limit, OR two independent signals agree
    WATCH           one signal is abnormal (dial above WATCH threshold / rising too fast,
                    abnormal lamp, siren)
    NORMAL          otherwise

Signal families: "dial", "lamp", "siren". Two different families active = signals agree.
(The brief says dial+lamp or dial+siren; lamp+siren is also counted here, because it
 is exactly what we'd see if the dial were unreadable during a real incident.)

A REJECTED dial or lamp is ignored: we never use a value we don't have.
An UNCERTAIN dial keeps its measured value and is judged normally — only REJECTED
dials (value null + "check needed") and null values are discarded.

Timestamps are ISO-8601 UTC ("2026-11-20T02:14:05Z"), like everywhere in the project.
`last_signal_ts` must be the SERVER-side receive time of the last reading (the
`received_at` column in app.py), not the device timestamp: a drifting device clock
or a delayed delivery must not fake an outage. Same rule the dashboard already
applies: MAX(received_at) vs offline_timeout_s from the settings table (seed 30 s).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

OFFLINE_TIMEOUT_S = 30                      # settings table: offline_timeout_s (seed 30)
ALARM_LAMP_COLORS = {"red", "orange"}       # a green lamp "on" is just "running"
RATE_WINDOW_S = 120                         # look-back window for the rise-rate rule

# Default thresholds — in the real app these come from the `instruments` table
# (Settings screen, GET /api/config). They mirror the seed rows in app.py
# (SEED_INSTRUMENTS), so out-of-the-box decisions match what the screen shows:
# oil_temp and winding_temp rise-rate 2.0/min, pressure has none (NULL = rule off).
DEFAULT_THRESHOLDS = {
    "oil_temp":     {"watch": 85,  "limit": 100, "max_rise_per_min": 2.0},
    "winding_temp": {"watch": 105, "limit": 120, "max_rise_per_min": 2.0},
    "pressure":     {"watch": 2.0, "limit": 2.5, "max_rise_per_min": None},
}


@dataclass
class Result:
    state: str          # NORMAL | WATCH | INTERVENE_NOW | SYSTEM_OFFLINE
    trigger: str = ""   # human-readable, ready for alerts.trigger / state_changes.reason


def parse_ts(ts: str) -> datetime:
    """'2026-11-20T02:14:05Z' -> aware datetime (UTC). Naive input is read as UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rising_too_fast(dial_id, value, now, history, max_rate):
    """True if the dial rose faster than max_rate (units/min) inside the window.

    history: {dial_id: [(datetime, value), ...]} — past readings, any order.
    """
    if not max_rate:
        return False
    points = [(t, v) for t, v in history.get(dial_id, [])
              if v is not None and 0 < (now - t).total_seconds() <= RATE_WINDOW_S]
    if not points:
        return False
    t0, v0 = min(points, key=lambda p: p[0])                 # oldest point in the window
    minutes = (now - t0).total_seconds() / 60
    # Need at least 1 min of span: over a few seconds, ordinary sensor noise looks
    # like a fast rise (verified against the simulator's noise — a 20 s guard was
    # not enough and produced spurious WATCH flickers in the NORMAL scenario).
    return minutes >= 1 and (value - v0) / minutes > max_rate


def evaluate(dials, lamps, siren, last_signal_ts, now=None,
             thresholds=None, history=None, offline_timeout_s=OFFLINE_TIMEOUT_S) -> Result:
    """
    dials / lamps : lists from the latest reading (format of section 5)
    siren         : {"detected": bool, "confidence": float}
    last_signal_ts: ISO string (or datetime) of the last signal RECEIVED by the
                    server (received_at); None = never
    now           : defaults to current UTC time (inject it in tests)
    thresholds    : {dial_id: {"watch", "limit", "max_rise_per_min"}}
    history       : {dial_id: [(datetime, value), ...]} for the rise-rate rule (optional)
    """
    now = now or datetime.now(timezone.utc)
    thresholds = thresholds or DEFAULT_THRESHOLDS
    history = history or {}

    # 1. Silence beats everything: stale data can't tell us anything.
    if last_signal_ts is None:
        return Result("SYSTEM_OFFLINE", "no signal received yet")
    last = parse_ts(last_signal_ts) if isinstance(last_signal_ts, str) else last_signal_ts
    silence = (now - last).total_seconds()
    if silence > offline_timeout_s:
        return Result("SYSTEM_OFFLINE", f"no signal for {int(silence)} s")

    # 2. Dials
    limit_hits, dial_warnings = [], []
    for d in dials or []:
        if d.get("status") == "REJECTED" or d.get("value") is None:
            continue                                         # never use a fabricated value
        th = thresholds.get(d["dial_id"])
        if not th:
            continue
        v = d["value"]
        if v >= th["limit"]:
            limit_hits.append(f"{d['dial_id']} above limit")
        elif v >= th["watch"]:
            dial_warnings.append(f"{d['dial_id']} above watch threshold")
        elif _rising_too_fast(d["dial_id"], v, now, history, th.get("max_rise_per_min")):
            dial_warnings.append(f"{d['dial_id']} rising too fast")

    # 3. Lamps and siren
    lamp_alarms = [f"{l['lamp_id']} lit" for l in lamps or []
                   if l.get("status") != "REJECTED" and l.get("state") == "on"
                   and l.get("color") in ALARM_LAMP_COLORS]
    siren_on = bool(siren and siren.get("detected"))

    # 4. Decide
    dial_signal = limit_hits + dial_warnings
    families = [bool(dial_signal), bool(lamp_alarms), siren_on]
    details = dial_signal + lamp_alarms + (["siren detected"] if siren_on else [])

    if limit_hits or sum(families) >= 2:
        return Result("INTERVENE_NOW", " + ".join(details))
    if any(families):
        return Result("WATCH", " + ".join(details))
    return Result("NORMAL", "")


# ───────────── Quick self-test:  python rules.py ─────────────
if __name__ == "__main__":
    NOW = datetime(2026, 11, 20, 2, 0, 0, tzinfo=timezone.utc)
    FRESH = "2026-11-20T01:59:58Z"
    dial = lambda v, s="OK": {"dial_id": "oil_temp", "value": v, "status": s}
    red = lambda on: {"lamp_id": "alarm_red", "state": "on" if on else "off",
                      "color": "red" if on else None, "status": "OK"}
    orange = lambda on: {"lamp_id": "alarm_orange", "state": "on" if on else "off",
                         "color": "orange" if on else None, "status": "OK"}
    quiet, loud = {"detected": False}, {"detected": True}
    # one past point: oil_temp was 70.0 one minute ago -> rise rate in units/min
    HIST = {"oil_temp": [(NOW - timedelta(seconds=60), 70.0)]}

    cases = [
        ("normal",                     [dial(70)],            [red(0)], quiet, FRESH, "NORMAL"),
        ("above watch",                [dial(88)],            [red(0)], quiet, FRESH, "WATCH"),
        ("above limit",                [dial(105)],           [red(0)], quiet, FRESH, "INTERVENE_NOW"),
        ("watch + red lamp",           [dial(88)],            [red(1)], quiet, FRESH, "INTERVENE_NOW"),
        ("watch + siren",              [dial(88)],            [red(0)], loud,  FRESH, "INTERVENE_NOW"),
        ("rejected dial is ignored",   [dial(None, "REJECTED")], [red(0)], quiet, FRESH, "NORMAL"),
        ("silent for 60 s",            [dial(70)],            [red(0)], quiet, "2026-11-20T01:59:00Z", "SYSTEM_OFFLINE"),
        ("never received",             [],                    [],       quiet, None,  "SYSTEM_OFFLINE"),
        # ── added with the compliance pass ──
        ("orange lamp alone",          [dial(70)],            [orange(1)], quiet, FRESH, "WATCH"),
        ("rising too fast",            [dial(78)],            [red(0)], quiet, FRESH, "WATCH"),
        ("slow rise is fine",          [dial(71.5)],          [red(0)], quiet, FRESH, "NORMAL"),
        ("rejected dial, lamp+siren",  [dial(None, "REJECTED")], [red(1)], loud,  FRESH, "INTERVENE_NOW"),
    ]
    for name, d, l, s, ts, expected in cases:
        got = evaluate(d, l, s, ts, now=NOW, history=HIST).state
        print(f"{'OK  ' if got == expected else 'FAIL'} {name:28} -> {got}")