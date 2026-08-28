---
name: pna
description: >
  Controls the Rohde & Schwarz Phase Noise Analyzer via REST API. Use this
  skill when the user wants to measure phase noise, start PNA sweeps, check
  instrument health, or read frequency-domain data. Triggers include: "PNA",
  "phase noise", "sweep", "measure noise", "start measurement", "/measure/",
  "/health/". Also use when user mentions "Rohde & Schwarz", "spectrum",
  "PNR", or asks about "carrier suppression", "SSB phase noise".
  Do NOT use for general network requests unrelated to PNA instruments.
---
# PNA Service Skill

Use this skill when working with PNA (Phase Noise Analyzer) measurements.

## Overview

The PNA service controls a Rohde & Schwarz Phase Noise Analyzer. It maintains a **persistent long-lived connection** to the instrument — connected at service startup and held until shutdown.

## Prerequisites

**The PNA service must be running before starting measurements.**

If the service is not running, Agent must start it first using `bash(background=True)`:

```
bash(command="python -m instrument.pna.main", background=True)
```

The service runs on port 8002.

## Starting the Service

**CRITICAL**: Always use `background=True` when starting the PNA service. Without it, the command will block the agent indefinitely.

```
bash(command="python -m instrument.pna.main", background=True)
```

## Capabilities

- Start asynchronous PNA measurements
- Query measurement status and results
- Cancel running measurements
- Receive real-time notifications via NotificationHub (port 8000)
- Persistent PNA connection verified via `/health`

## Service Address

**Base URL**: `http://127.0.0.1:8002`

## API Endpoints


| Method | Endpoint                    | Description                                                                                                       |
| -------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| POST   | `/measure`                  | Start a new measurement                                                                                           |
| GET    | `/measure/{task_id}`        | Get task status                                                                                                   |
| POST   | `/measure/{task_id}/cancel` | Cancel a running measurement                                                                                      |
| GET    | `/health`                   | Health check —`pna_connected` reflects actual PNA connection                                                     |
| POST   | `/read_pna`                 | Extract key frequency points from a completed measurement CSV. Body:`{csv_path, target_freqs, tolerance_factor?}` |

## Naming Convention

- **Default**: omit `csv_filename`. The server stores files as
  `trace_{YYYYMMDD_HHMMSS}.csv`, unique per second.
- **User-named**: only include `csv_filename` when the user explicitly says
  "save as ..." (e.g., "save as run_42").
- **NEVER invent** names like `"phase_noise.csv"` or `"my_trace.csv"` — this
  caused file overwrite when the same invented name was reused. Examples in
  this skill that omit `csv_filename` show the **default**; examples that
  include it show the **user-named exception**.

## Connection Behavior

- **Startup**: Service connects to PNA instrument in `lifespan` (before first request is handled)
- **During operation**: Connection is held persistently — no reconnect per measurement
- **Shutdown**: Connection is closed cleanly
- **Health check**: `GET /health` returns `pna_connected: true/false` based on actual instrument state

If `pna_connected` is `false`, the PNA instrument could not be reached at service startup. Report this to the user.

## Usage Examples

### Check health

```
GET http://127.0.0.1:8002/health
```

Response: `{"status": "ok", "pna_connected": true}`

### Start a measurement

**Default — omit `csv_filename`** (server picks a timestamp-based name; do not invent names):

```
POST http://127.0.0.1:8002/measure
{
  "start_freq": 1,
  "stop_freq": 1000000
}
```

Response: `{"task_id": "abc123", "status": "pending"}`

Then end immediately. The result arrives as a NotificationHub push.

**User-specified name** (only when user explicitly says "save as xxx"):

```
POST http://127.0.0.1:8002/measure
{
  "start_freq": 1,
  "stop_freq": 1000000,
  "csv_filename": "the_name_user_gave.csv"
}
```

### Check status (only if user explicitly requests)

```
GET http://127.0.0.1:8002/measure/abc123
```

Response:

```
{
  "task_id": "abc123",
  "status": "completed",
  "csv_path": "D:\\PDHlocking\\LabPilot\\data\\PNA_data\\my_trace.csv",
  "result": {"trace_points": 801}
}
```

## Important Constraints

- **Do NOT start a new measurement while another is in progress** — the service will reject the request with a 400 error
- Results are automatically pushed to the NotificationHub (port 8000) when complete — **do NOT poll for results**

## Prohibited Actions

- **NEVER** start the PNA service with `background=False` — it will block the agent indefinitely
- **ALWAYS** set `background=True` when calling `bash` to start the PNA service

## Reading Measurement Data

Use the dedicated reader endpoint after the NotificationHub push delivers
the result. Do not parse CSVs by hand — call `POST /read_pna`.

### Locating the saved file

The WebSocket push payload includes `result.csv_path` (absolute path). Read
it directly:

```
[WebSocket] [2026-08-28T14:32:00Z] [pna] task_completed:
  result={"status": "success",
          "csv_path": "D:\\PDHlocking\\LabPilot\\data\\PNA_data\\trace_20260828_143200.csv",
          ...}
```

- ✅ Use `result.csv_path` as the file location.
- ❌ Do NOT call `GET /measure/{task_id}` to look up the file — the push
  already has the path; that call is wasteful.
- ❌ Do NOT reconstruct the path from a timestamp guess.
- ❌ If `result.csv_path` is missing — the measurement failed; report the
  error to the user instead of trying to read data.

### Reading key points

```
POST http://127.0.0.1:8002/read_pna
{
  "csv_path": "D:\\PDHlocking\\LabPilot\\data\\PNA_data\\trace_20260828_143200.csv",
  "target_freqs": [1, 10, 100, 1000, 10000, 100000],
  "tolerance_factor": 0.05
}
```

Defaults: `target_freqs` defaults to per-decade points
`[1, 10, 100, 1000, 10000, 100000, 1000000]` Hz; `tolerance_factor` defaults
to `0.05` (5%, matching the legacy reader heuristic).

Response `200`:

```json
{
  "csv_path": "D:\\PDHlocking\\LabPilot\\data\\PNA_data\\trace_20260828_143200.csv",
  "points": [
    {"frequency_hz": 1.0, "power_dbm": -45.32},
    {"frequency_hz": 10.0, "power_dbm": -55.18}
  ],
  "missing": []
}
```

If `missing` is non-empty, the CSV did not cover those frequencies (e.g.,
the measurement only ran 1 Hz–100 kHz and you asked for 1 MHz). Report which
frequencies are missing to the user.

`csv_path` may also be relative — it is resolved against `PNA_DATA_DIR`
(`data/PNA_data/`) automatically.

## Notification Format

When measurement completes, result is pushed to NotificationHub (port 8000):

```
[WebSocket] task_completed: csv_path=..., trace_points=...
```

Failed measurement:

```
[WebSocket] task_failed: error=...
```

## Configuration


| Env Variable           | Default                          | Description             |
| ------------------------ | ---------------------------------- | ------------------------- |
| PNA_RESOURCE           | USB::0xAAD::0x290::101334::INSTR | VISA resource string    |
| PNA_VISA_TIMEOUT       | 1500000                          | VISA timeout in ms      |
| PNA_OPC_TIMEOUT        | 800000                           | OPC timeout in ms       |
| PNA_DATA_DIR           | data/PNA_data                    | Output directory        |
| PNA_PORT               | 8002                             | Service port            |
| PNA_DEFAULT_START_FREQ | 1                                | Default start freq (Hz) |
| PNA_DEFAULT_STOP_FREQ  | 1000000                          | Default stop freq (Hz)  |

## Implementation Notes

- **Connection**: Persistent at startup, released at shutdown — not per-measurement
- **Data directory**: `data/PNA_data/`
- **Output format**: CSV with columns `Frequency_Hz`, `Power_dBm`
- **Notifications**: Results are POSTed to `http://127.0.0.1:8000/notify`

## Error Handling

- **PNA not connected at startup**: `pna_connected: false` in `/health` → Report "PNA instrument connection failed. Check USB connection and instrument power."
- **Measurement in progress**: Report "A measurement is already in progress. Please wait."
- **Measurement failed**: Report the error message from notification

## Workflow

1. **Check if service is running**: `bash(command="curl http://127.0.0.1:8002/health")`
2. **If service not running**: Start it with `bash(command="python -m instrument.pna.main", background=True)`
3. **Verify PNA connection**: Check `pna_connected` in health response — if `false`, report connection issue
4. User requests measurement → Agent calls `POST /measure`
5. Get `task_id`, tell user "measurement started"
6. **End immediately** — do NOT poll or wait
7. When NotificationHub pushes result → Agent receives via WebSocket on port 8000 → report to user
8. User asks to read data → take `result.csv_path` from the WebSocket push and call `POST /read_pna` with the requested target frequencies (see *Reading Measurement Data*)
