# telemetry
unified telem pipeline for collecting, storing and viewing sensor data. starting w propulsion DAQ.


## Architecture

```
sources (DAQ)
   │
   ├─ live copy, reduced rate ──> ingest service ──> TimescaleDB ──> Grafana (live)
   │                              └────────── site laptop, no internet needed ──────┘
   │
   └─ full-rate log on the source's own disk
          └─ after the test ──> S3 bronze (raw)
                                   └─> S3 silver (calibrated Parquet)
                                          └─> gold (per-run summaries) ──> Grafana (across tests)
```

Each source lives in its own repo and plugs in by following
[CONTRACT.md](CONTRACT.md); this repo owns the contract and everything after it.
Storage loosely follows the
[medallion architecture](https://www.databricks.com/blog/what-is-medallion-architecture)
(bronze / silver / gold).

- **Sources:** each system reads its own hardware and writes every raw sample
  to its own disk first, so nothing is lost if the network drops. The first
  source is the propulsion DAQ (LabJack T7).
- **Ingest service:** runs on the site laptop, receives the live copy from
  every source and writes it to TimescaleDB.
- **Grafana (live):** dashboards during a test, served from the laptop so they
  work without internet.
- **Bronze:** each source's raw log uploaded to S3 after the test, never
  modified.
- **Silver:** raw values converted to needed eng units (PSI, N, K) and
  stored as Parquet on S3.
- **Gold:** one aggregated summary row per test (peak pressure, burn time,
  etc.), for comparing past tests.

was considering to add msg broker, but at this scale one ingest service and plain Parquet files
are enough. The message format doesn't depend on the transport, so a kafka/iceberg
can be added later.

## Repo layout

Scaffolding only

```
CONTRACT.md          format every source follows
ingest/              live path: service on the site laptop (sources -> TimescaleDB)
pipeline/            after-the-test path: upload (bronze), silver, gold
db/                  TimescaleDB table definitions
grafana/             dashboards kept in git: live/ for test day, history/ across tests
tools/fake_source.py simulated source for development and dry runs
docker-compose.yml   starts the laptop stack
tests/
```

## DAQ

For local ingest, I run `./.venv/bin/python -m ingest.app` on port 8000.
`POST /ingest` and `/ingest/ws` take the same v1 live batches and reply with
`{"accepted": <sample count>}`. Bad HTTP batches get 422; bad WebSocket batches
get `{"error": "..."}` and I can keep sending on the same connection.
Storage is in memory for now, so restarting clears it. TimescaleDB is still TODO.

For a local mock recording, I run:

```sh
./.venv/bin/python -m daq.record --mock --seconds 2 --out data/runs
```

It prints the run folder with `raw.csv` and `meta.json`. Each CSV row has `t_s`
and raw volts for all 11 channels at 1 kHz. I get UTC by adding `t_s` seconds to
`start_utc` in `meta.json`. Ctrl+C flushes the remaining samples before closing the log.

For live monitoring, I add `--ingest-url http://localhost:8000 --live-rate-hz 25`.
It sends averaged scans over WebSocket to `/ingest/ws` in batches of five by default.
I add `--live-transport http` for POST `/ingest` instead. Whole scan windows keep
the live rate at or below the requested rate; timestamps mark the window midpoint.
A missing reading makes that channel's window `null`. Incomplete windows are
discarded at the end. Pressure is sent in PSI; uncalibrated channels stay in volts.
The sender drops batches if its queue fills or a request fails, and gives pending
batches 0.6 s to finish on shutdown. The full-rate disk log keeps going either way.

For the GUI, I run `./.venv/bin/python -m daq.gui --mock`, then Connect and Start Recording.
Runs go under `data/runs`; I can add an ingest URL in the sidebar. Load cells stay in volts until calibrated.
I can pick `ws` (default) or `http` beside the ingest URL before connecting.
For a screenshot, I run `QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m daq.gui --mock --screenshot /tmp/daq.png --after 2`.

### Stand control

The GUI has a control column: Emergency Abort, Reset Abort, the seven test states,
and a toggle per valve. Each valve shows what I commanded next to what the PLC
read back. With `--mock` the window starts its own fake PLC (Modbus TCP on
localhost), so nothing leaves this machine. Writing to the real PLC isn't wired up yet.

Valves are coils 0 to 6 on unit 1: LOX Dump, Fuel Dump, LOX Fill, LOX Run,
Fuel Run, LOX Vent, Fuel Vent. Every command is written, then read back.
If the PLC is down, the latest mask waits and goes out on reconnect, and the
status line under the valves says so.

| State | Open |
| --- | --- |
| IDLE, PRESSURIZATION, READY | none |
| FILLING | LOX Fill, LOX Vent, Fuel Vent |
| TEST | LOX Run, Fuel Run |
| SAFE, ABORT | both dumps, both vents |

Rules:

- LOX Run and LOX Fill never open together. Fill closes.
- ABORT latches. The only way out is SAFE, and only once every watched PT is
  at or below 50 PSI. An unknown pressure also blocks the reset.
- In ABORT, Run and Fill toggles are locked. Dumps and vents still toggle.
- Auto-abort trips when a watched PT goes above 4500 PSI, or when its reads are
  missing, stale, or outside 0.1 to 4.9 V for more than 0.25 s. It runs on its
  own thread off the acquisition data, and the alert is a label, not a dialog,
  so logging and plots keep going.

The thresholds, watched PTs (all six for now), and PLC address live in
`daq/control/config.py`. The 50 PSI reset limit and 0.25 s window are
placeholders until the team picks real ones.
