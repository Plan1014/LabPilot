---
name: pdh-locking
description: >
  Use this skill whenever interacting with the PDH (Pound-Drever-Hall) optical
  cavity locking FastAPI service (Linien LLM Control API) running at
  http://127.0.0.1:8000. Triggers include: any request to calculate PI, query
  task results, check or control lock state, set PID/modulation parameters,
  export waveforms, or monitor power. Also use when user mentions "PDH",
  "Pound-Drever-Hall", "lock", "锁定", "PI calculation", "task ID",
  "/pi/", "/lock/", "/pid/", "/modulation/", "/plot/", "/power/" endpoints.
  Do NOT use for general FastAPI services unrelated to PDH locking.
---

# PDH Locking System — FastAPI Client Skill

## Overview

Controls a PDH (Pound-Drever-Hall) optical cavity locking system via FastAPI at
`http://127.0.0.1:8000`. The service wraps a PyQt5 GUI control panel and
executes operations in the main GUI thread via a thread-safe command queue.

## Base URL

```
http://127.0.0.1:8000
```

All commands use `curl.exe` on Windows (PowerShell/cmd compatible).

---

## Endpoint Summary

| Operation | Method | Endpoint | Notes |
|-----------|--------|----------|-------|
| PI 计算 | POST | `/pi/calculate` | Returns `task_id`, long-running async |
| 查询结果 | GET | `/task/{task_id}` | Poll for completion |
| 锁定状态 | GET | `/lock/status` | Returns `{"locked": bool}` |
| 执行锁定 | POST | `/lock/manual` | Triggers lock — verify with power check |
| 执行解锁 | POST | `/lock/stop` | Returns `{"status": "stopped"}` |
| 设置 PID | POST | `/pid/set` | `{"kp":0-8191,"ki":0-8191,"kd":0-8191}` |
| 设置调制 | POST | `/modulation/set` | `{"frequency_mhz":0-99,"amplitude_vpp":0-2}` |
| 导出波形 | POST | `/plot/export` | `{"save_path":"*.png"}` |
| 读取功率 | GET | `/power/monitor` | Returns power snapshot |

---

## Workflows

### 1. PI Calculation (Async Task)

**⚠️ Important constraints:**
- PI calculation is **long-running** and can interfere with subsequent
  operations — do NOT主动执行 (do not proactively execute) unless the user
  explicitly requests it
- PI calculation **must be performed in unlocked state** — if locked,
  report that it cannot run while locked

PI calculation runs a `PIComputeThread` in the GUI.

**Step 1: Start calculation (only if explicitly requested and system is unlocked)**
```
curl.exe -X POST http://127.0.0.1:8000/pi/calculate
```

Response (immediate):
```json
{"task_id": "uuid-string", "status": "pending"}
```

**Step 2: Poll for result**
```
curl.exe http://127.0.0.1:8000/task/{task_id}
```

While `status` is `"processing"`, the task is still running. When `status`
becomes `"completed"`, the `result` field contains the output:

```json
{"task_id": "uuid-string", "status": "completed", "result": {...}}
```

If the task is not found or expired, returns HTTP 404.

**User-facing presentation:** Start the calculation, tell the user the
`task_id`, poll in the background, and report the result when ready.

---

### 2. Lock Control

**⚠️ Critical: Lock state vs actual lock verification**

The `/lock/status` endpoint returns `{"locked": bool}` which reflects the
**software lock flag** (`panel.parameters.lock.value`). A successful HTTP
response from `/lock/manual` does NOT guarantee the physical lock succeeded.

**Always verify lock success with a power comparison:**

1. Before calling `/lock/manual`, call `/power/monitor` and record `power_before`
2. Call `/lock/manual`
3. After a short delay (~2–3s for lock to settle), call `/power/monitor` → `power_after`
4. If `power_after < power_before` (power dropped), the lock is **successful**
5. If `power_after >= power_before`, the lock may have **failed** — report this

**Check lock status:**
```
curl.exe http://127.0.0.1:8000/lock/status
```

**Execute lock:**
```
curl.exe -X POST http://127.0.0.1:8000/lock/manual
```

**Execute unlock (stop lock):**
```
curl.exe -X POST http://127.0.0.1:8000/lock/stop
```
Returns: `{"status": "stopped", "message": "Lock successfully stopped"}`

---

### 3. PID Parameter Configuration

PID values are integers in range 0–8191 (inclusive).

```
curl.exe -X POST http://127.0.0.1:8000/pid/set ^
  -H "Content-Type: application/json" ^
  -d "{\"kp\":100,\"ki\":50,\"kd\":10}"
```

Response: `{"status": "updated", "kp": 100, "ki": 50, "kd": 10}`

Parameters:
- `kp` — proportional gain (0–8191)
- `ki` — integral gain (0–8191)
- `kd` — derivative gain (0–8191)

---

### 4. Modulation Configuration

```
curl.exe -X POST http://127.0.0.1:8000/modulation/set ^
  -H "Content-Type: application/json" ^
  -d "{\"frequency_mhz\":10.5,\"amplitude_vpp\":1.2}"
```

Response: `{"status": "updated", "freq_mhz": 10.5, "amp_vpp": 1.2}`

Parameters:
- `frequency_mhz` — float, range 0–99 MHz
- `amplitude_vpp` — float, range 0–2 Vpp

**⚠️ Frequency warnings (inform the user):**
- `> 10 MHz`: Signal begins to distort — warn the user
- `> 31.25 MHz`: Signal is completely unusable — strongly warn the user
- Always keep frequency to **2 decimal places** when presenting or setting
- **Recommended amplitude is 2 Vpp** (maximum value) unless user specifies otherwise

---

### 5. Waveform Export

```
curl.exe -X POST http://127.0.0.1:8000/plot/export ^
  -H "Content-Type: application/json" ^
  -d "{\"save_path\":\"spectrum.png\"}"
```

Default path is `spectrum.png` if `save_path` is omitted.

---

### 6. Power Monitoring

```
curl.exe http://127.0.0.1:8000/power/monitor
```

Returns the current power reading snapshot. Use this for lock verification
as described in the Lock Control section.

---

## Response Handling

| Scenario | HTTP Status | Response |
|----------|-------------|----------|
| Success | 200 | JSON with result data |
| Not found | 404 | `{"detail": "Task not found or expired"}` |
| Validation error | 422 | `{"detail": [...]}`

For async tasks (`/pi/calculate`):
- Pending: `{"task_id": "...", "status": "pending"}`
- Processing: `{"task_id": "...", "status": "processing"}`
- Completed: `{"task_id": "...", "status": "completed", "result": {...}}`

---

## Usage Examples

**"Calculate PI and give me the result"**
→ Verify system is unlocked (if locked, report cannot run) → POST /pi/calculate → get task_id → poll /task/{id} until completed → report result

**"Is the PDH system locked?"**
→ GET /lock/status → report locked state

**"Lock the system"**
→ GET /power/monitor → store power_before → POST /lock/manual →
→ GET /power/monitor → compare → report success/failure

**"Set kp=200, ki=75, kd=15"**
→ POST /pid/set with those values → confirm update

**"Set modulation to 15 MHz"**
→ Warn about distortion above 10 MHz → POST /modulation/set → confirm

**"Set modulation to 35 MHz"**
→ Warn that >31.25 MHz is completely unusable → ask user to confirm

**"Export the waveform to wave.png"**
→ POST /plot/export with save_path="wave.png" → confirm file saved

**"What's the current power?"**
→ GET /power/monitor → report value
