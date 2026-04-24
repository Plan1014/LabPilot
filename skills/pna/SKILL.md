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

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/measure` | Start a new measurement |
| GET | `/measure/{task_id}` | Get task status |
| POST | `/measure/{task_id}/cancel` | Cancel a running measurement |
| GET | `/health` | Health check — `pna_connected` reflects actual PNA connection |

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

```
POST http://127.0.0.1:8002/measure
{
  "start_freq": 1,
  "stop_freq": 100000,
  "csv_filename": "my_trace.csv"
}
```

Response: `{"task_id": "abc123", "status": "pending"}`

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

## Data Reading

Measurement data is saved as CSV to `data/PNA_data/`. To read specific frequency points:

```python
import csv

csv_path = "D:\\PDHlocking\\LabPilot\\data\\PNA_data\\my_trace.csv"
target_freqs = [1, 10, 100, 1000]  # Hz — read these points

with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        if float(row["Frequency_Hz"]) in target_freqs:
            print(f"{row['Frequency_Hz']} Hz: {row['Power_dBm']} dBm")
```

Output format: CSV with columns `Frequency_Hz`, `Power_dBm`. Frequencies are in Hz, power in dBm.

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

| Env Variable | Default | Description |
|--------------|---------|-------------|
| PNA_RESOURCE | USB::0xAAD::0x290::101334::INSTR | VISA resource string |
| PNA_VISA_TIMEOUT | 1500000 | VISA timeout in ms |
| PNA_OPC_TIMEOUT | 800000 | OPC timeout in ms |
| PNA_DATA_DIR | data/PNA_data | Output directory |
| PNA_PORT | 8002 | Service port |
| PNA_DEFAULT_START_FREQ | 1 | Default start freq (Hz) |
| PNA_DEFAULT_STOP_FREQ | 100000 | Default stop freq (Hz) |

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
8. User asks to read data → read CSV at requested frequency points (e.g., 1Hz, 10Hz, 100Hz)
