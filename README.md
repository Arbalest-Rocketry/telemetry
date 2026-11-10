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

For a local mock recording, I run:

```sh
./.venv/bin/python -m daq.record --mock --seconds 2 --out data/runs
```

It prints the run folder with `raw.csv` and `meta.json`. Each CSV row has `t_s`,
`timestamp_utc`, and raw volts for all 11 channels at 1 kHz. Ctrl+C flushes the
remaining samples before closing the log.

For live monitoring, I add `--ingest-url http://localhost:8000 --live-rate-hz 25`.
It posts averaged scans to `/ingest` in batches of five. Whole scan windows keep
the live rate at or below the requested rate; timestamps mark the window midpoint.
A missing reading makes that channel's window `null`. Incomplete windows are
discarded at the end. Pressure is sent in PSI; uncalibrated channels stay in volts.
The sender drops batches if its queue fills or a request fails, and gives pending
batches 0.6 s to finish on shutdown. The full-rate disk log keeps going either way.
