# Nadhir prototype — README

Two programs and one database. `simulator.py` plays the fake on-site box
(Tiers 1–4) and pushes section-5 readings/alerts over HTTP; `api/app.py` is the
dashboard API (Tier 5) that validates those payloads "to the letter", stores
them in the normalized 9-table SQLite schema and serves every screen of the
requirements. Both speak the exact section-5 JSON format from the spec.

## Project structure

```
root/
├── api/
│   ├── app.py           # dashboard API          → http://localhost:8080
│   ├── schema.sql       # 9-table schema (must sit next to app.py)
│   └── nadhir.db        # SQLite database (created in Step 1, lives next to app.py)
├── templates/
│   └── simulator.html   # control page UI (loaded by simulator.py — keep the folder name exactly `templates/`)
└── simulator.py         # fake on-site box       → http://localhost:5001
```

- `app.py` is the `api.py` from the shared deliverables, renamed. Same file.
- Matched pairs — always replace together, never mix generations:
  `app.py` + `schema.sql`, and `simulator.py` + `templates/simulator.html`.
- Flask finds `templates/simulator.html` by convention, so the folder must be
  called `templates` and sit next to `simulator.py`.

## Ports

| What | Port | Env var to change it | URL |
|---|---|---|---|
| `api/app.py` — dashboard API | **8080** | `API_PORT` | http://localhost:8080 |
| `simulator.py` — control page | **5001** | `SIM_PORT` | http://localhost:5001 |
| simulator → API target | — | `API_URL` | default `http://localhost:8080` |

The defaults already match each other (simulator sends to 8080, API listens on
8080). If you move the API to another port, set **both** sides, e.g.:

```bash
API_PORT=8081 python app.py                                  # in api/
API_URL=http://localhost:8081 python simulator.py            # in root/
```

## Requirements

```bash
pip install flask requests        # app.py needs flask, simulator.py needs flask + requests
```

`sqlite3` command-line tool must be on the PATH for Step 1 (macOS ships it;
Linux `sudo apt install sqlite3`; Windows bundled with SQLite tools).

**Launch the scripts as plain `python <file>` — never through an IDE
"Flask server" run configuration.** That kind of run config skips
`if __name__ == "__main__"`, so the simulator's reading loop never starts
(yellow banner on the control page: no reading for 15 s).

---

## Step 1 — reset the database (required before running app.py)

Run **inside `api/`** — the same folder `app.py` will be started from:

```bash
cd api
rm nadhir.db                      # Windows: del nadhir.db
sqlite3 nadhir.db < schema.sql
```

Rules, in plain words:

1. **Stop `app.py` first** (Ctrl+C in its terminal), then delete `nadhir.db`.
2. **Create the fresh DB in the same folder you will launch `python app.py`
   from.** `app.py` opens `nadhir.db` in whatever folder it is *started from* —
   if you initialize it in `api/` but launch from root, you end up with two
   different database files and your SQL browser shows the empty one. Never
   launch it as `python api/app.py` from root; `cd api` and run
   `python app.py` there.
3. **Why the delete is mandatory:** `schema.sql` uses `CREATE TABLE IF NOT
   EXISTS`, so it never repairs or removes leftover tables. A `nadhir.db` from
   the previous schema generation (the old `dial_readings`/`lamp_readings`
   tables) or from earlier tests silently poisons the demo. Deleting is the
   only clean reset. Skip the delete only if you deliberately want to keep the
   old readings and alerts.
4. **Verify you are on the right file:** `app.py` prints the absolute path at
   startup (`Nadhir API · http://localhost:8080 · db: …`) and
   `curl http://localhost:8080/api/state` returns `db_path`. Open *that* file
   in your SQL tool.

## Step 2 — start the simulator (terminal 1, project root)

```bash
python simulator.py               # from root, NOT from inside api/
```

- Control page: **http://localhost:5001** — "Nadhir · Simulator control".
- It starts **standalone**: nothing leaves the box until you tick
  **Send to API** on the page (it turns on automatically if you start it with
  `API_URL` set).
- One reading every 2–5 s in the exact section-5 format; alerts fire on
  threshold crossings; guard and electrician replies are simulated.
- Scenarios (buttons on the page): `normal` · `drift` · `limit` ·
  `unreadable` · `silent`. **▶ Play full demo** runs the whole walkthrough by
  itself; from the CLI: `python simulator.py demo`, or
  `python simulator.py drift` to open directly in one scenario.

## Step 3 — start the dashboard API (terminal 2, inside `api/`)

```bash
cd api
python app.py
```

- Prints `Nadhir API · http://localhost:8080 · db: …` and serves every
  endpoint below. This is the **one and only** dashboard API — see
  Troubleshooting before starting anything else on 8080.
- Firing order is: **first `python simulator.py`, then `python app.py`**.
  The simulator tolerates the API being late: it keeps retrying every few
  seconds, the pill flips to `connected` and the counters start climbing as
  soon as `app.py` is up. Nothing is lost in between.

## Check that it works (30 seconds)

1. Control-page pill (top right): `API: connected · http://localhost:8080 · N
   readings · M alerts sent · 0 failed` — **`failed` must stay 0**; any
   rejected call raises a red banner with the HTTP status and the API's answer.
2. `curl http://localhost:8080/api/state` → shows `system_state`,
   `last_signal_at` and the absolute `db_path`.
3. In the folder where `app.py` is running:
   `sqlite3 nadhir.db "SELECT COUNT(*) FROM readings;"` — the count climbs by
   one every 2–5 s once **Send to API** is ticked.

## What each file does

**`api/app.py`** — receives readings, alerts and replies, validates every
field against the section-5 rules (a REJECTED dial must carry `value: null` +
message `check needed`; a lamp that is off must carry `color: null`; nothing
invented, nothing missing), normalizes them into the 9-table schema
(`readings` + `instrument_readings` per dial/lamp, `state_changes`,
`alerts` + `alert_recipients` + `alert_responses`), auto-saves the latest
reading as the snapshot of each new alert, and serves the screens: overview,
instrument detail (curve points), alert log with filters, alert detail with
recipients/replies/snapshot, morning review and configuration. This
implementation uses the `/api` namespace only; the legacy `/readings` and
`/state` aliases are not included in this repo.
Seeds the 6 instruments on first start (the two settings come from
`schema.sql`); the `contacts` table starts empty until you add yours.

**`simulator.py`** — the fake box: builds one reading per 2–5 s for the
current scenario, drifts oil temperature in `drift`, hits the limit with red
lamp + siren in `limit`, sends `value: null` / `check needed` in `unreadable`,
and stops talking entirely in `silent` (the API must then report
SYSTEM_OFFLINE after `offline_timeout_s`). Its control page tells the truth
about delivery: counters of readings/alerts actually accepted by the API, a
failed counter, a red banner with the last API error, and alerts marked
`NOT STORED BY API` when they were rejected.

**`api/schema.sql`** — the 9-table SQLite schema, loaded by `app.py`
automatically (and by Step 1 manually). Timestamps are ISO-8601 UTC text;
run `PRAGMA foreign_keys = ON` on every connection if you poke at the DB by
hand (the schema file already does it for the CLI session).

## API cheat sheet

| Method & path | Purpose |
|---|---|
| `POST /api/readings` | receive a reading (validated, normalized) |
| `GET /api/readings?limit=20` | list readings, newest first, exact section-5 shape |
| `GET /api/readings?dial=oil_temp&limit=200` | curve points for one instrument |
| `GET /api/readings?status=REJECTED` | latest unreadable readings ("check needed") |
| `GET /api/state` | overall state; SYSTEM_OFFLINE after `offline_timeout_s` of silence; returns `db_path` |
| `POST /api/alerts` | create an alert + recipients (snapshot auto-saved) |
| `GET /api/alerts?level=&since=&until=` | alert log with filters |
| `GET /api/alerts/<alert_id>` | alert detail: recipients, replies, snapshot |
| `POST /api/alerts/<alert_id>/responses` | add a reply (guard `1`/`2`, electrician `COMING`/`WATCH`/`FALSE ALARM`) |
| `PATCH /api/alerts/<alert_id>` | fill in the outcome the next morning |
| `GET /api/review?since=…` | morning review: totals, avg first response, false alarms |
| `GET /api/config` | instruments, contacts, settings |

## Troubleshooting

**The readings table is empty but the control page looks fine.** This exact
bug was debugged on 2026-11-17 — work through it in this order:

1. **Exactly one dashboard API, and the right one.** The old FastAPI draft
   (`tsy.py`, started with uvicorn) must NOT be running anywhere: the
   simulator posts to `/api/readings` and `/api/alerts`, tsy.py only serves
   `/readings` (→ HTTP 404) and its Pydantic models reject every real reading
   anyway (`color: null` → HTTP 422), so `readings` stays empty while the page
   looks alive. Close any uvicorn process, then start `python app.py`.
2. **Let the page tell you where it breaks** — press **Send one test reading
   now** on the control page (force-sends one reading, tick or no tick):
   - red banner `HTTP 404` → the wrong server answers on that port (tsy.py or
     a leftover process);
   - red banner `HTTP 422` / `HTTP 400` → the payload was rejected, the banner
     names the exact field;
   - red banner `unreachable` → nothing is listening there: `app.py` not
     started yet, or `API_URL` doesn't match `API_PORT`;
   - `stored · db: <absolute path>` → the chain works; if the pill's counters
     still don't climb every 2–5 s, the reading loop is dead (IDE "Flask
     server" launch — restart with plain `python simulator.py`) or the
     scenario is `silent`.
3. **Wrong database file.** Counters climb but your SQL count doesn't → you
   opened a different `nadhir.db` than the one `app.py` writes. Check
   `db_path` from `/api/state` (or the startup line) and open that exact file.
   This is the two-folders trap from Step 1, rule 2.
4. **Last resort:** stop both programs, delete `nadhir.db`, redo Step 1,
   restart both. A fresh DB plus two plain `python …` launches fixes every
   failure seen so far.

**`Address already in use` on 8080/5001** — another process holds the port
(leftover `app.py`, uvicorn, an old simulator). Close it, or move both sides:
`API_PORT=8081 python app.py` and `API_URL=http://localhost:8081 python
simulator.py`.

**Pill says `Standalone (not sending)`** — tick **Send to API** on the control
page, or start the simulator with `API_URL=http://localhost:8080`.

**Yellow banner: no reading for 15 s** — the reading loop is not running
(IDE "Flask server" run configuration skips `__main__`) or the scenario is
`silent`. Restart with plain `python simulator.py` or pick another scenario.
