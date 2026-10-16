# Linien → LabPilot 事件推送 接口规格

> **版本**: 1.0 (对应 `feat/ui-v2` 分支)
> **模块**: `linien-gui-v2.ui.api_bridge` + `linien_gui_v2.ui.notifier`
> **本侧基础**: `python -m linien_gui_v2.app` (端口 `8001`)
> **LabPilot 接收端**: `POST http://127.0.0.1:8000/notify` (可被 `NOTIFICATION_HUB_URL` 环境变量覆盖)

---

## 1. 概述

Linien GUI v2 (端口 8001) 通过 HTTP 主动推送三类事件给 LabPilot NotificationHub:

| 事件类型 | `type` 字段 | 触发源 | 严重度 |
|---|---|---|---|
| 锁定抖动 | `lock_anomaly` | linien-server `fast_relock` 在 60 s 内让 `parameters.lock` 翻转 ≥ 5 次 (默认阈值) | 中 |
| 自动重锁失败 | `relock_failed` | `parameters.auto_relock_status` 变为 `failed` / `lost_during_settle` | 高 (需人工介入) |
| 日志异常 | `log_errors` | `linien.log` 在 5 s 周期内出现新的 ≥ WARNING 级条目 | 低~中 |

**功率数据不主动推送** (功率本身持续抖动, 噪声大) — 通过 `GET /power/recent` 拉取。

---

## 2. 推送协议 (LabPilot 接收方向)

### 2.1 端点
- **Method**: `POST`
- **URL**: `${NOTIFICATION_HUB_URL}/notify` (默认 `http://127.0.0.1:8000/notify`)
- **Content-Type**: `application/json`
- **超时**: 5 s (Linien 侧 daemon thread, 失败仅 print 不抛)

### 2.2 消息 envelope 格式 (所有事件统一)

```json
{
  "source": "pdh-locking",
  "type": "lock_anomaly | relock_failed | log_errors | task_completed",
  "task_id": "event-<kind>-<epoch_ms>",
  "result": { ... 事件特定字段 ... },
  "timestamp": "2026-09-15T03:58:58.123Z"
}
```

| 字段 | 说明 |
|---|---|
| `source` | 固定 `"pdh-locking"` (Linien 标识) |
| `type` | 见上表; `task_completed` 是兼容旧 PI 计算任务 (来自 v1 路径) |
| `task_id` | 见下表, 三类事件各自有前缀; 旧 PI 任务用 `pi-<uuid>` |
| `result` | 事件特定字段 (见 §3) |
| `timestamp` | UTC ISO 8601, 末尾 `Z` (Linien 端 `datetime.utcnow().isoformat() + "Z"`) |

### 2.3 `task_id` 前缀规则

| `type` | `task_id` 前缀 | 例子 |
|---|---|---|
| `lock_anomaly` | `event-lock-anomaly-` | `event-lock-anomaly-1789444738123` |
| `relock_failed` | `event-relock-{status}-` | `event-relock-failed-1789444738123`<br>`event-relock-lost_during_settle-1789444738123` |
| `log_errors` | `event-log-errors-` | `event-log-errors-1789444738123` |
| `task_completed` (旧 PI) | `pi-` | `pi-<uuid>` |

---

## 3. 推送事件 result 字段详解

### 3.1 `type=lock_anomaly` (滑窗抖动)

**触发条件**:
- `parameters.lock.value` 在 60 s 窗口内翻转 ≥ 5 次 (默认阈值, env `LINIEN_LOCK_THRESHOLD_COUNT`)
- **必须** `parameters.auto_relock_status.value == "running"` 期间 (源头过滤: 用户主动操作不会触发)
- 触发后进入 60 s cooldown (env `LINIEN_LOCK_COOLDOWN_SECONDS`)

```json
{
  "event_type": "lock_anomaly",
  "source": "fast_relock",
  "count": 5,
  "window_seconds": 12.34,
  "lock_acquired": 3,
  "lock_lost": 2,
  "events": [
    {"ts": 1789444738.123, "prev": true,  "new": false, "kind": "lock_lost"},
    {"ts": 1789444738.456, "prev": false, "new": true,  "kind": "lock_acquired"},
    ...
  ],
  "suggestion": "Lock is flapping. Run /lock/status, /power/recent?window_seconds=30, consider relaxing PID or widening watch_lock_threshold.",
  "local_time": "2026-09-15T11:58:58+08:00",
  "epoch": 1789444738.5,
  "since_boot_seconds": 123.45,
  "boot_anchor": "2026-09-15T11:56:55+08:00",
  "boot_anchor_epoch": 1789444615.0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_type` | string | 固定 `"lock_anomaly"` (与 envelope `type` 同步) |
| `source` | string | 固定 `"fast_relock"` (源头标记, 用于过滤/分类) |
| `count` | int | 触发消化的事件总数 (本例 = `lock_acquired + lock_lost`) |
| `window_seconds` | float | 首个事件到末个事件的时间跨度 (秒) |
| `lock_acquired` | int | 翻转中 False→True 的次数 |
| `lock_lost` | int | 翻转中 True→False 的次数 |
| `events[].ts` | float | epoch 秒 (Linien 端 `time.time()`) |
| `events[].prev` | bool | 翻转前的 lock 状态 |
| `events[].new` | bool | 翻转后的 lock 状态 |
| `events[].kind` | string | `"lock_lost"` (True→False) 或 `"lock_acquired"` (False→True) |
| `suggestion` | string | 建议的排查步骤 (硬编码) |

### 3.2 `type=relock_failed` (自动重锁失败, 立即推)

**触发条件**:
- `parameters.auto_relock_status.value` 变为 `failed` 或 `lost_during_settle`
- 不等滑窗阈值, 立即推

```json
{
  "event_type": "relock_failed",
  "status": "failed",
  "message": "Auto-relock entered unrecoverable state: failed. Human intervention recommended.",
  "local_time": "2026-09-15T11:58:58+08:00",
  "epoch": 1789444738.5,
  "since_boot_seconds": 123.45,
  "boot_anchor": "2026-09-15T11:56:55+08:00",
  "boot_anchor_epoch": 1789444615.0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_type` | string | `"relock_failed"` |
| `status` | string | 实际状态值: `"failed"` 或 `"lost_during_settle"` |
| `message` | string | 人类可读 + 行动建议 |

**`auto_relock_status` 实际合法值集合** (来自 `linien-server/fast_relock.py`):

```
idle | running | success | failed | lost_during_settle | settled_no_data | settled | unstable
```

立即推送白名单 = `{failed, lost_during_settle}`. 其他值仅观测, 不推.

### 3.3 `type=log_errors` (日志 WARNING/ERROR 批量)

**触发条件**:
- 后台线程每 5 s (env `LINIEN_LOG_PUSH_INTERVAL_SEC`) 扫描一次 `linien.log`
- 增量解析 + 去重, 仅推送 ≥ `WARNING` 级别 (env `LINIEN_LOG_PUSH_MIN_LEVEL`)
- 单批最多 50 条 (env `LINIEN_LOG_PUSH_BATCH_MAX`), 超出截断并标 `truncated=true`

```json
{
  "event_type": "log_errors",
  "new_count": 3,
  "truncated": false,
  "since_boot_seconds": 123.45,
  "entries": [
    {
      "ts": "2026-09-15 11:58:55,123",
      "logger": "linien_server.foo",
      "level": "ERROR",
      "message": "test for pdh push"
    },
    ...
  ],
  "local_time": "2026-09-15T11:58:58+08:00",
  "epoch": 1789444738.5,
  "boot_anchor": "2026-09-15T11:56:55+08:00",
  "boot_anchor_epoch": 1789444615.0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_type` | string | `"log_errors"` |
| `new_count` | int | 本次 entries 实际条数 (≤ `LINIEN_LOG_PUSH_BATCH_MAX`) |
| `truncated` | bool | 是否因 batch_max 截断 |
| `entries[].ts` | string | **本地时间字符串** `YYYY-MM-DD HH:MM:SS,mmm` (Linien 端 `asctime` 格式, 不带时区, 因为 `RotatingFileHandler` 用了 local time formatter) |
| `entries[].logger` | string | Python logger 名 |
| `entries[].level` | string | `"WARNING"` / `"ERROR"` / `"CRITICAL"` (≥ min_level 的) |
| `entries[].message` | string | 原始消息 (可能含 `-`, 已做 strip) |

---

## 4. 拉取端点 (Linien 8001)

### 4.1 `GET /logs/recent?lines=100&level=ERROR&grep=foo`
Tail `linien.log` (默认 100 行; `level` 大小写不敏感精确匹配; `grep` 在 message 上子串过滤, 大小写不敏感).

```json
{
  "lines": [{"ts": "2026-09-15 11:58:55,123", "logger": "...", "level": "ERROR", "message": "..."}, ...],
  "count": 42,
  "total_bytes_read": 12345,
  "log_path": "C:\\Users\\<user>\\AppData\\Local\\linien\\linien.log",
  "local_time": "2026-09-15T11:58:58+08:00",
  "epoch": 1789444738.5,
  "boot_anchor": "2026-09-15T11:56:55+08:00",
  "boot_anchor_epoch": 1789444615.0,
  "since_boot_seconds": 123.45,
  "filter_hints": "ts 是本地时间字符串 (linien 端 asctime 格式), 不需要做时区换算"
}
```

### 4.2 `GET /power/recent?window_seconds=60`
复用 `PowerMeterWindow.data_manager` (环形缓冲 2000 样本 + 30 Hz 限速 + 临时二进制文件).

```json
{
  "status": "ok | cold_start | no_power_meter",
  "window_seconds": 60,
  "count": 1800,
  "samples": [
    {"t_offset": 12.345, "epoch": 1789444738.5, "local_time": "...", "power_dbm": -12.34},
    ...
  ],
  "stats": {"min": -15.2, "max": -10.1, "avg": -12.4, "count": 1800},
  "current_power": -12.34,
  "display_window_size": 2000,
  "total_points": 12345,
  "data_manager_anchor_epoch": 1789444615.0,
  "rate_limit_hz": 30,
  "local_time": "...",
  "epoch": ...,
  "boot_anchor": "...",
  "boot_anchor_epoch": ...,
  "since_boot_seconds": ...,
  "unit_note": "power_dbm 当前是 ADC counts (非校准 dBm); data_manager_anchor_epoch 是 PowerMeterWindow 启动 epoch"
}
```

**`to_plot` 实际可用的功率键**: 仅 `power_adc_a` (兜底用 `error_signal` 或 `error_signal_1` 均值的绝对值).
**没有** `power_adc_b` / `slow` 等键 — plan B 里假设的键 v2 实际不存在.

### 4.3 `GET /lock/recent?window_minutes=5`
当前锁定事件滑窗 (不含已触发 lock_anomaly 并清空的).

```json
{
  "window_minutes": 5,
  "count": 0,
  "config": {
    "threshold": 5,
    "cooldown_seconds": 60,
    "window_seconds": 60
  },
  "events": [],
  "current_lock_state": false,
  "local_time": "...",
  "epoch": ...,
  "boot_anchor": "...",
  "boot_anchor_epoch": ...,
  "since_boot_seconds": ...
}
```

### 4.4 其它 v1 兼容端点 (沿用 plan B 接口)

| 端点 | 方法 | 说明 |
|---|---|---|
| `/pi/calculate` | POST | 异步 PI 计算, 返回 `task_id` |
| `/task/{task_id}` | GET | 查询 PI 任务结果 |
| `/lock/status` | GET | 当前 lock 状态 |
| `/lock/manual` | POST | 智能锁定 (走 `start_smart_lock`) |
| `/lock/stop` | POST | 停止锁定 (走 `stop_lock`) |
| `/pid/set` | POST | 设 PID 三参数 (kp/ki/kd) |
| `/modulation/set` | POST | 设调制频率 + 幅度 |
| `/plot/export` | POST | 导出当前波形为图片 |
| `/power/monitor` | GET | 当前功率 (从 UI label 解析) |

---

## 5. 时间字段约定

所有时间相关字段语义统一:

| 字段 | 格式 | 用途 |
|---|---|---|
| `local_time` | ISO 8601 + 时区 (例 `2026-09-15T11:58:58+08:00`) | 人类可读, **带时区** |
| `epoch` | float (秒, UTC, `time.time()`) | 机器解析, 跨时区安全 |
| `since_boot_seconds` | float | 本次开机后多久发生 |
| `boot_anchor` | ISO 8601 + 时区 (进程启动时定锚) | LabPilot 用来识别"本次开机"事件边界 |
| `boot_anchor_epoch` | float (进程启动 epoch) | 同上, 机器版 |

**`boot_anchor` 行为**: 进程启动时 `BOOT_ANCHOR_EPOCH = time.time()`, `BOOT_ANCHOR_LOCAL = datetime.now().astimezone().isoformat(timespec="seconds")`. 之后**所有** `/notify` 推送和拉取响应里的 `boot_anchor` / `boot_anchor_epoch` 都引用同一个常量. LabPilot 可用 `epoch >= boot_anchor_epoch` 判定"本次开机后"的事件.

**注意**: `entries[].ts` (在 `log_errors` 推送和 `/logs/recent` 响应里) 是 **Linien 端 `RotatingFileHandler` 写的 asctime, 不带时区** (例 `2026-09-15 11:58:55,123`). LabPilot 需知道这是 Linien 主机本地时间.

---

## 6. 错误与降级

| 场景 | Linien 侧行为 | LabPilot 侧预期 |
|---|---|---|
| `NOTIFICATION_HUB_URL` 不可达 | daemon thread 内 `requests.post` 抛异常 → 5 s 超时 → print 错误 → 继续 | 推送丢失, **不需要**回补 (LabPilot 可走 `/logs/recent` 或 `/lock/recent` 拉取) |
| `LINIEN_NOTIFY_ENABLED=false` | `send_notification_async` 直接 return | 不会有任何推送, LabPilot 应改走轮询 |
| bridge 未 ready (GUI 启动期) | `attach_subscriptions` 跳过, `_lock_aggregator` 为 None | `/lock/recent` 返回 `events: []` (不抛) |
| server 缺 `parameters.to_plot` / `lock` / `auto_relock_status` | `hasattr` 守卫 + console 警告, **不抛** | `/lock/recent` 等端点返回空, 推送不发 |
| Linien 进程崩溃重启 | 新 `BOOT_ANCHOR_*`, 推送里 `boot_anchor` 变化 | LabPilot 收到新 boot_anchor_epoch 即可识别重启 |

---

## 7. 验证步骤 (在 LabPilot 端复现)

**前提**: Linien 端 `python -m linien_gui_v2.app` 启动, 8001 端口可达; LabPilot NotificationHub 监听 8000.

### 7.1 冷启动日志查询
```bash
curl 'http://127.0.0.1:8001/logs/recent?lines=20'
# 期望: 返回解析后的历史日志, 含 local_time / boot_anchor / since_boot_seconds
```

### 7.2 ERROR 日志推送
```bash
# 在运行中的 Linien Python 进程内 (或外部触发):
python -c "import logging; logging.getLogger('linien_server.foo').error('test for pdh push')"
# 5–10 s 后, LabPilot 收到 type=log_errors, entries 含该条
```

### 7.3 滑窗去抖 (lock_anomaly)
让 fast_relock 实际触发 (例如等功率下降到 `threshold_dbm=8.1` 之下):
- 60 s 内 `parameters.lock` 自动翻转 ≥ 5 次
- LabPilot 收到 **恰好 1 条** `type=lock_anomaly`, `source="fast_relock"`, `count=5`

**注意**: 手动交替 `/lock/manual` + `/lock/stop` **不会**触发 lock_anomaly (被源头过滤跳过, 视为用户操作).

### 7.4 立即触发 (relock_failed)
通过 rpyc 把 `parameters.auto_relock_status` 设为 `"failed"`:
```python
c.root.parameters.auto_relock_status.value = "failed"
# 立即 (< 100 ms) 收到 type=relock_failed, status=failed
```

### 7.5 功率被动模式
```bash
# 数据持续累积, 不会触发 /notify
curl 'http://127.0.0.1:8001/power/recent?window_seconds=30'
# 观察 count / samples 增长
```

### 7.6 boot_anchor 一致性
```bash
# 连续两次调用, boot_anchor_epoch 应相同
curl 'http://127.0.0.1:8001/logs/recent?lines=5' | jq .boot_anchor_epoch
curl 'http://127.0.0.1:8001/logs/recent?lines=5' | jq .boot_anchor_epoch
# 与首条 /notify 推送里的 boot_anchor_epoch 一致
```

---

## 8. 实施反馈 (plan B.10 要求汇总)

| 项 | 实际值 |
|---|---|
| **1. 实际字段命名** | 见 §3 各表 (entries/events/lock_acquired/lock_lost/samples/stats 等) |
| **2. 实际推送的 `event_type` 字符串** | `lock_anomaly` / `relock_failed` / `log_errors` / `task_completed` (兼容旧 PI 任务) |
| **3. 实际推送时的 `task_id` 前缀格式** | `event-lock-anomaly-*` / `event-relock-{status}-*` / `event-log-errors-*` / `pi-<uuid>` |
| **4. `auto_relock_status` 实际合法值集合** | `{idle, running, success, failed, lost_during_settle, settled_no_data, settled, unstable}` (8 个; 原 plan B 漏了 `settled_no_data`) |
| **5. `parameters.to_plot` 里实际可用的功率键** | **仅** `power_adc_a` (plan B 假设的 `power_adc_b` / `slow` **不存在**); 兜底用 `error_signal` / `error_signal_1` 均值 |
| **6. `MainWindowAPIBridge.__init__` 里能否访问 `self.locking_panel.parameters`** | **不能** — v2 用 `linien_gui_v2.bridge.Bridge` 单例, 参数通过 `self._bridge.parameters.*` 访问; 订阅必须延后到 `on_connection_established` 之后 (`api_bridge.attach_subscriptions()`) |
| **7. `logging.getLogger("linien_server.foo").error(...)` 是否会真的写入 `linien.log`** | **是** — `linien-client/__init__.py:29` 配置 `RotatingFileHandler(str(LOG_FILE_PATH), maxBytes=1MB, backupCount=10)`, 所有 linien 模块共用同一个 root logger |

---

## 9. 实现位置 (供 LabPilot 维护对照)

| 文件 | 角色 |
|---|---|
| `linien-gui-v2/linien_gui_v2/ui/notifier.py` | 推送 helper + LogTailer / LogWatcher / LockEventAggregator |
| `linien-gui-v2/linien_gui_v2/ui/api_bridge.py` | FastAPI 路由 + `attach_subscriptions` 订阅 |
| `linien-gui-v2/linien_gui_v2/main_window_v2.py` | 启动时 `api_bridge.attach_subscriptions()` 触发点 |
| `linien-common/linien_common/config.py` | `LOG_FILE_PATH = user_data_dir / "linien.log"` |
| `linien-client/linien_client/__init__.py` | `RotatingFileHandler` 注册 (所有 linien 模块日志落地) |
| `linien-gui-v2/linien_gui_v2/app.py` | 单例 `app.power_meter_window = PowerMeterWindow()` (被 `/power/recent` 复用) |
| `tests/test_notifier_v2.py` | 48 个 pytest 测试覆盖所有上述组件 |

---

## 10. 环境变量 (Linien 端可配)

| 变量 | 默认 | 说明 |
|---|---|---|
| `NOTIFICATION_HUB_URL` | `http://127.0.0.1:8000` | LabPilot 端接收 URL |
| `LINIEN_NOTIFY_ENABLED` | `true` | 总开关, 设 `false` 完全不推 |
| `LINIEN_LOCK_WINDOW_SECONDS` | `60` | lock_anomaly 滑窗长度 |
| `LINIEN_LOCK_THRESHOLD_COUNT` | `5` | 触发阈值 |
| `LINIEN_LOCK_COOLDOWN_SECONDS` | `60` | 触发后冷却期 |
| `LINIEN_LOG_BACKUPS` | `10` | LogTailer 扫描的 backup 文件数 |
| `LINIEN_LOG_DEFAULT_LINES` | `100` | `/logs/recent` 默认 lines |
| `LINIEN_LOG_PUSH_INTERVAL_SEC` | `5.0` | LogWatcher 周期 |
| `LINIEN_LOG_PUSH_MIN_LEVEL` | `WARNING` | 推送最低级别 (`WARNING`/`ERROR`) |
| `LINIEN_LOG_PUSH_BATCH_MAX` | `50` | 单批最大条数 |
| `LINIEN_POWER_BUFFER_SECONDS` | `60` | `/power/recent` 默认 window_seconds (实际样本受 30 Hz 限速) |
