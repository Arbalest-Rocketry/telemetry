# Source contract (v1, draft)

Sources save raw data locally. Live reporting is optional and must not interrupt recording.

## Run files

Upload to `bronze/<run_id>/<source>/`, preserving original logs:

- `raw.csv`: `t_s` (monotonic seconds from `start_utc`), then raw channel readings.
- `meta.json`: `contract_version: 1`, `source`, `run_id`, `start_utc`, actual `sample_rate_hz`, and `channels`.

Proposed channel metadata: `sensor`, `raw_unit`, `unit`, `calibration_id`, and optional
`calibration: {v_zero, v_span, full_scale}`.

The proposed silver layer would apply `(raw - v_zero) / v_span * full_scale`;
`v_span` must be nonzero.
Without calibration, use `calibration_id: null` and `unit = raw_unit`.

## Live batches

`POST /ingest` with JSON, for example:

```json
{
  "contract_version": 1,
  "source": "daq",
  "run_id": "example-run",
  "channels": {
    "pressure": {"unit": "PSI", "calibration_id": "example-v1"}
  },
  "samples": [
    {"t": "2026-11-09T21:15:49Z", "channel": "pressure", "value": 812.4}
  ]
}
```

## Rules

- Stable lowercase source/channel names; share a run ID across participating sources.
- Use UTC acquisition timestamps; verify clock alignment when comparing sources.
- Live units/calibration match the run metadata and stay fixed within a source/run.
- Missing readings: empty CSV cell or JSON `null`, never fabricated zeros. Preserve measured raw values.
- Live sending uses a separate bounded queue and short timeouts. Drop failed/excess batches.
- Live rate cannot exceed acquisition rate. Apply the same calibration in live and silver.

Run ID assignment and integration with the existing DAQ are pending.
