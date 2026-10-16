---
name: pdh-locking
description: >
  Use this skill whenever interacting with the PDH (Pound-Drever-Hall) optical
  cavity locking FastAPI service running at
  http://127.0.0.1:8001. Triggers include: any request to calculate PI, query
  task results, check or control lock state, set PID/modulation parameters,
  export waveforms, or monitor power. Also use when user mentions "PDH",
  "Pound-Drever-Hall", "lock", "锁定", "PI calculation", "task ID",
  "/pi/", "/lock/", "/pid/", "/modulation/", "/plot/", "/power/" endpoints.
  Do NOT use for general FastAPI services unrelated to PDH locking.
---
# PDH Locking System — FastAPI Client Skill

## Overview

Controls a PDH (Pound-Drever-Hall) optical cavity locking system via FastAPI at
`http://127.0.0.1:8001`. The service wraps a PyQt5 GUI control panel and
executes operations in the main GUI thread via a thread-safe command queue.

## Base URL

```
http://127.0.0.1:8001
```

All commands use `curl.exe` on Windows (PowerShell/cmd compatible).

---

## Endpoint Summary

| Operation | Method | Endpoint            | Notes                                          |
| --------- | ------ | ------------------- | ---------------------------------------------- |
| PI 计算   | POST   | `/pi/calculate`   | Returns `task_id`, result pushed via WebSocket |
| 查询结果  | GET    | `/task/{task_id}` | 仅在用户明确要求时查询                              |
| 锁定状态  | GET    | `/lock/status`    | Returns `{"locked": bool}`                   |
| 执行锁定  | POST   | `/lock/manual`    | Triggers lock — verify with power check       |
| 执行解锁  | POST   | `/lock/stop`      | Returns `{"status": "stopped"}`              |
| 设置 PID  | POST   | `/pid/set`        | `{"kp":0-8191,"ki":0-8191,"kd":0-8191}`      |
| 设置调制  | POST   | `/modulation/set` | `{"frequency_mhz":0-99,"amplitude_vpp":0-2}` |
| 导出波形  | POST   | `/plot/export`    | `{"save_path":"*.png"}`                      |
| 读取功率  | GET    | `/power/monitor`  | Returns power snapshot                         |
| 日志查询  | GET    | `/logs/recent`    | `?lines=N&level=ERROR&grep=...`（解析后）       |
| 功率时序  | GET    | `/power/recent`   | `?window_seconds=N`（环形缓冲，不主动推送）       |
| 锁定事件  | GET    | `/lock/recent`    | `?window_minutes=N`（当前滑窗）                  |

---

## WebSocket Push Notifications

When PI calculation completes, the result is automatically pushed to the agent via the **NotificationHub (port 8000)**. **You do NOT need to poll for results.**

The push notification format:
```
[WebSocket] task_completed: P={value}, I={value}
```

For example:
```
[WebSocket] task_completed: P=1800, I=4800
```

**Important**: When you receive such a WebSocket notification, report the result to the user. Do NOT initiate additional PI calculations unless the user explicitly requests it.

---

## Lock Anomaly Digest (auto-notify)

Linien 端在 `linien-gui-v2/linien_gui_v2/ui/notifier.py` 的
`LockEventAggregator` 会监控 `parameters.lock.value`，
**仅在 `parameters.auto_relock_status == "running"` 期间**
（linen-server `fast_relock.py` 写的标识）且 **60 s 内 lock 翻转 ≥5 次**
（默认阈值）时，POST 一条 `type=lock_anomaly` 摘要到 `/notify`
（带 cooldown 60 s，避免噪声）。格式：

```
[WebSocket] lock_anomaly: Lock is flapping. Run /lock/status,
             /power/recent?window_seconds=30, consider relaxing PID
             or widening watch_lock_threshold. (6 events in 47.2s,
             3 acquired, 3 lost; linien 开机后 123.4s)
```

**关键细节**：

- **用户主动操作不会触发** `/lock/manual` + `/lock/stop` 触发的翻转会被源头过滤跳过，
  因为此时 `auto_relock_status` 不是 `"running"`。
  因此 `lock_anomaly` 只表示 fast_relock 自身在抖动。
- `parameters.auto_relock_status` 进入 `failed` / `lost_during_settle` 时
  **立即**（不走阈值）推送一条 `type=relock_failed`：

  ```
  [WebSocket] relock_failed: Auto-relock entered unrecoverable state: failed.
               Human intervention recommended. (linien 开机后 234.5s)
  ```

**反应指引**：

1. **不要立即调用 `/lock/manual` 重锁** — 锁已经在抖动，重锁无意义。
2. 调用 `curl /lock/status` 确认当前态。
3. 调用 `curl /power/recent?window_seconds=30` 看功率漂移。
4. 给出调参建议（PID 减小 / 放宽 `watch_lock_threshold`）。
5. 收到 `relock_failed` 时建议人工介入，**不要自动重试**。

**时间字段**：所有推送的 `result.local_time` 字段是 linien 端本地时间字符串
（带时区，如 `+08:00`），与 LabPilot 系统时间一致，可直接读懂。
`result.since_boot_seconds` 表示距离 linien GUI v2 进程启动多久。

---

## Workflows

### 1. PI Calculation (Async Task)

**⚠️ Important constraints:**

- PI calculation is **long-running** and can interfere with subsequent
  operations — do NOT主动执行 (do not proactively execute) unless the user
  explicitly requests it
- PI calculation **must be performed in unlocked state** — if locked,
  report that it cannot run while locked

**Execution flow:**

1. Start calculation (only if explicitly requested and system is unlocked):

```
curl.exe -X POST http://127.0.0.1:8001/pi/calculate
```

Response (immediate):

```json
{"task_id": "uuid-string", "status": "pending"}
```

2. Tell the user "PI calculation started", then **end immediately**.

3. Do NOT wait for results, do NOT spawn subagents, do NOT poll `/task/{task_id}`.

**Why**: Results are automatically pushed via WebSocket when complete. When you receive a message in this format:

```
[WebSocket] task_completed: P={value}, I={value}
```

It means the result has arrived and this agent will be triggered again to report the result. You do NOT need to do anything else.

**Forbidden behaviors:**
- Do NOT spawn subagents to wait for or poll PI results
- Do NOT use sleep or any waiting mechanism
- Do NOT manually poll `/task/{task_id}`

The system handles result delivery automatically.

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
curl.exe http://127.0.0.1:8001/lock/status
```

**Execute lock:**

```
curl.exe -X POST http://127.0.0.1:8001/lock/manual
```

**Execute unlock (stop lock):**

```
curl.exe -X POST http://127.0.0.1:8001/lock/stop
```

Returns: `{"status": "stopped", "message": "Lock successfully stopped"}`

**Re-lock (idempotent operation):**

When the user asks to lock the system without specifying the current state,
handle the already-locked branch. Calling `/lock/manual` on an already-locked
system can leave stale integrator state and produce a partial lock that reads
as `locked` without fully clamping the cavity.

1. Check current status:

```
curl.exe http://127.0.0.1:8001/lock/status
```

Parse the `locked` field.

2. **If `locked == true`** — unlock first to reset integrator state:

```
curl.exe -X POST http://127.0.0.1:8001/lock/stop
```

Wait ~1–2 s for unlock to settle.

3. Lock (in both branches):

```
curl.exe -X POST http://127.0.0.1:8001/lock/manual
```

4. **Always verify** with the power-comparison procedure described at the top
   of this section — record `power_before` before step 1, capture
   `power_after` 2–3 s after step 3, and confirm `power_after < power_before`.

---

### 3. PID Parameter Configuration

PID values are integers in range 0–8191 (inclusive).

```
curl.exe -X POST http://127.0.0.1:8001/pid/set ^
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
curl.exe -X POST http://127.0.0.1:8001/modulation/set ^
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
curl.exe -X POST http://127.0.0.1:8001/plot/export ^
  -H "Content-Type: application/json" ^
  -d "{\"save_path\":\"spectrum.png\"}"
```

Default path is `spectrum.png` if `save_path` is omitted.

---

### 6. Power Monitoring

```
curl.exe http://127.0.0.1:8001/power/monitor
```

Returns the current power reading snapshot. Use this for lock verification
as described in the Lock Control section.

---

## Response Handling

| Scenario         | HTTP Status | Response                                    |
| ---------------- | ----------- | ------------------------------------------- |
| Success          | 200         | JSON with result data                       |
| Not found        | 404         | `{"detail": "Task not found or expired"}` |
| Validation error | 422         | `{"detail": [...]}`                       |

For async tasks (`/pi/calculate`):

- Pending: `{"task_id": "...", "status": "pending"}`
- **Do NOT poll for results** — they are delivered automatically via WebSocket

---

## Usage Examples

**"Calculate PI and give me the result"**
→ Verify system is unlocked (if locked, unlock it) → POST /pi/calculate → get task_id → Tell the user "PI calculation started", then **end immediately**. If WebSocket push arrives → report result

**"Is the PDH system locked?"**
→ GET /lock/status → report locked state

**"Lock the system"**
→ GET /power/monitor → store power_before → POST /lock/manual →
→ GET /power/monitor → compare → report success/failure

**"Re-lock the system"**
→ GET /lock/status → if `locked == true`: POST /lock/stop + wait 1–2 s →
→ GET /power/monitor (power_before) → POST /lock/manual →
→ wait 2–3 s → GET /power/monitor (power_after) →
→ confirm power_after < power_before

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

---

## 事件推送格式（自动触发，AI 无需轮询）

Linien 端会自动向 LabPilot NotificationHub (`POST /notify`) 推送以下事件。
**AI 无需轮询**，收到推送时直接按指引反应即可。

| 事件 `type` | 触发条件 | 严重度 |
|---|---|---|
| `task_completed` | PI 异步计算完成（旧路径，兼容 v1） | 中 |
| `lock_anomaly`  | fast_relock 期间 60s 内 lock 翻转 ≥5 次 | 中 |
| `relock_failed` | `auto_relock_status` 变为 `failed`/`lost_during_settle` | 高 |
| `log_errors`   | linien.log 新增 ≥ WARNING 级条目（每 5s 批量） | 低~中 |

### 7. Log Inspection

调试或回答"出了什么问题"：

```
curl.exe "http://127.0.0.1:8001/logs/recent?lines=200&level=ERROR"
curl.exe "http://127.0.0.1:8001/logs/recent?lines=200&grep=fast_relock"
```

返回 JSON：`{lines: [{ts, logger, level, message}, ...], count, log_path, filter_hints, boot_anchor, since_boot_seconds}`。

- `ts` 是 linien 端 `asctime` 本地时间字符串（例 `2026-09-15 14:23:01,234`），不需要做时区换算。
- `boot_anchor` 是 linien 进程启动锚点；`since_boot_seconds` 是请求时的相对秒数。
- 不需要每次推送都拉取日志——除非用户在排查问题或请求上下文。

### 8. ERROR 推送反应

Linien 端 `LogWatcher` 每 5s 扫描一次 `linien.log`，把新产生的
≥ WARNING 级条目批量打包成一条 `type=log_errors` 推送到 `/notify`。

推送消息示例：

```
[WebSocket] log_errors: 3 new WARNING/ERROR entries
             [2026-09-15 14:23:01,234] ERROR linien_server.foo: test for pdh push
             [2026-09-15 14:23:15,012] WARNING linien_server.parameters: ...
             [2026-09-15 14:24:02,890] ERROR linien_server.fast_relock: ...
             (linien 开机后 67.8s)
```

**反应**：

1. 不要对每条 ERROR 都重复拉取 `/logs/recent` —— 推送里已经包含了 entries 列表。
2. 仅当用户明确要求"完整日志"或上下文不足时，调用 `/logs/recent?lines=200` 取历史。
3. 如 ERROR 与 `fast_relock` 相关，参考 `lock_anomaly` 反应指引考虑 PID/`watch_lock_threshold` 调整。

### 9. 功率数据访问（不主动推送）

功率本身持续抖动，linien 端**不**主动推送功率变化。
需要时调用：

```
curl.exe "http://127.0.0.1:8001/power/recent?window_seconds=30"
```

返回 JSON 含：

- `status`: `"ok" | "cold_start" | "no_power_meter"`
- `samples`: `[{t_offset, epoch, local_time, power_dbm}, ...]`
- `stats`: `{min, max, avg, count}`（仅 `status="ok"` 时）
- `current_power`: 最近一次功率（dBm 字符串解析或 GUI label）
- `data_manager_anchor_epoch`: `PowerMeterWindow` 启动 epoch
- `rate_limit_hz`: 30（PowerDataManager 限速）

**注意**：`power_dbm` 当前是 ADC counts（非校准 dBm）。如果需要 dBm，
需要在 linien 端做 ADC→dBm 校准转换（目前未实现）。
