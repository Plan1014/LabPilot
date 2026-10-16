# PDH-Locking 事件推送 + 日志访问 — Linien 侧实施任务

> **任务**:在 linien GUI 的 FastAPI 桥 (`D:\linien\linien-gui\linien_gui\ui\api_bridge.py`) 中扩展日志访问、锁定事件聚合、ERROR 推送三项能力,让 LabPilot AI 智能体能感知锁定抖动与异常日志。
>
> **外部依赖**:通过 `http://127.0.0.1:8000/notify` POST JSON 推送(已存在 `_send_notification_async` helper 复用)。**不要引入新传输协议**。
>
> **关键约定**:
> - 日志路径用 `appdirs.AppDirs("linien").user_data_dir / "linien.log"`(**不要硬编码**)。
> - 所有 `timestamp` 字段统一用 `datetime.now().astimezone().isoformat(timespec='seconds')` 产生本地时间字符串(带时区),并附 `epoch=time.time()`。
> - 进程启动时记录 `BOOT_ANCHOR_EPOCH = time.time()` 和 `BOOT_ANCHOR_LOCAL = datetime.now().astimezone().isoformat()`。首条 `/notify` 推送时附带 `boot_anchor` 和 `boot_anchor_epoch`,LabPilot 用它来识别"本次开机后的事件"。
> - 所有参数回调通过现有 `_cmd_queue` / `_invoke` / `process_commands` 模式 marshal 到 Qt 主线程。需要新增一个 `_event_queue: Queue[Callable]` 处理无返回值的回调(避免污染 `_cmd_queue` 的 Future 配对)。
> - 功率变化**不**主动推送(用户明确:功率本身持续抖动)。

---

## B.1 文件改动总览

只改一个文件:`D:\linien\linien-gui\linien_gui\ui\api_bridge.py`。

预计新增 ~280 行。常量、辅助类、聚合器、订阅、新端点,全部加在文件末尾的 `MainWindowAPIBridge` 类之前或之后。

## B.2 新增常量(放在 `LINIEN_WEBSOCKET_*` 之后)

```python
import re
import time
import pickle
import threading
from collections import deque
from pathlib import Path
from queue import Queue, Empty
from concurrent.futures import Future
from datetime import datetime
from typing import Optional, Tuple, List, Deque, Callable

from appdirs import AppDirs

# ---- Lock-event aggregator tunables (env-overridable) ----
LINIEN_LOCK_WINDOW_SECONDS   = int(os.getenv("LINIEN_LOCK_WINDOW_SECONDS", "60"))
LINIEN_LOCK_THRESHOLD_COUNT  = int(os.getenv("LINIEN_LOCK_THRESHOLD_COUNT", "5"))
LINIEN_LOCK_COOLDOWN_SECONDS = int(os.getenv("LINIEN_LOCK_COOLDOWN_SECONDS", "60"))

# ---- Power buffer tunables ----
LINIEN_POWER_BUFFER_SECONDS  = int(os.getenv("LINIEN_POWER_BUFFER_SECONDS", "60"))
LINIEN_POWER_SAMPLE_HZ       = float(os.getenv("LINIEN_POWER_SAMPLE_HZ", "10"))
POWER_BUFFER_CAPACITY        = int(LINIEN_POWER_BUFFER_SECONDS * LINIEN_POWER_SAMPLE_HZ)

# ---- Log access / ERROR push ----
LINIEN_LOG_BACKUPS           = int(os.getenv("LINIEN_LOG_BACKUPS", "3"))
LINIEN_LOG_DEFAULT_LINES     = int(os.getenv("LINIEN_LOG_DEFAULT_LINES", "100"))
LINIEN_LOG_PUSH_INTERVAL_SEC = float(os.getenv("LINIEN_LOG_PUSH_INTERVAL_SEC", "5.0"))
LINIEN_LOG_PUSH_MIN_LEVEL    = os.getenv("LINIEN_LOG_PUSH_MIN_LEVEL", "WARNING")  # WARNING | ERROR
LINIEN_LOG_PUSH_BATCH_MAX    = int(os.getenv("LINIEN_LOG_PUSH_BATCH_MAX", "50"))

# ---- Boot anchor (本进程时间锚点) ----
BOOT_ANCHOR_EPOCH: float = time.time()
BOOT_ANCHOR_LOCAL: str   = datetime.now().astimezone().isoformat(timespec="seconds")
```

## B.3 新增模块级队列(紧接 `_cmd_queue` 之后)

```python
_event_queue: "Queue[Callable[[], None]]" = Queue()
```

## B.4 修改 `process_commands`(已有方法 `api_bridge.py:284`)

```python
def process_commands(self):
    # 原 cmd_queue 部分(保持不变)
    while True:
        try:
            func, future = _cmd_queue.get_nowait()
            future.set_result(func())
        except Empty:
            break
        except Exception as e:
            future.set_exception(e)
    # 新增:drain event_queue(fire-and-forget 回调)
    while True:
        try:
            cb = _event_queue.get_nowait()
            cb()
        except Empty:
            break
        except Exception as e:
            print(f"[DEBUG] event queue error: {e}")
```

## B.5 新增辅助类(放在 `MainWindowAPIBridge` 类之前)

### B.5.1 `_resolve_log_path()`

```python
def _resolve_log_path() -> Path:
    return Path(AppDirs("linien").user_data_dir) / "linien.log"
```

### B.5.2 日志解析正则 + `LogTailer`

```python
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
    r"\s+-\s+(?P<logger>[\w.]+)"
    r"\s+-\s+(?P<level>\w+)"
    r"\s+-\s+(?P<msg>.*)$"
)

def _parse_log_line(raw: str):
    m = _LINE_RE.match(raw.rstrip("\n"))
    if not m:
        return None
    return {"ts": m["ts"], "logger": m["logger"],
            "level": m["level"], "message": m["msg"]}


class LogTailer:
    """增量、轮转安全的日志读取器。
    按文件路径跟踪上次读取偏移;当文件缩小(发生 rotate)时
    重新打开并从 0 开始,同时扫描最近的 N 个 backup。"""

    def __init__(self, log_path: Path, backups: int = 3):
        self.log_path = log_path
        self.backups = backups
        self._offsets: dict[str, int] = {}
        self._bytes_read = 0
        self._lock = threading.Lock()

    @staticmethod
    def _open(p): return open(p, "r", encoding="utf-8-sig", errors="replace")

    def read_incremental(self) -> List[str]:
        with self._lock:
            out: List[str] = []
            paths = [self.log_path] + [
                self.log_path.with_suffix(self.log_path.suffix + f".{i}")
                for i in range(1, self.backups + 1)
            ]
            for path in paths:
                if not path.exists():
                    continue
                size = path.stat().st_size
                prev = self._offsets.get(str(path), 0)
                if size < prev:
                    self._offsets.pop(str(path), None)
                    prev = 0
                if size > prev:
                    with self._open(path) as f:
                        f.seek(prev)
                        chunk = f.read()
                    self._offsets[str(path)] = size
                    self._bytes_read += len(chunk.encode("utf-8", "replace"))
                    out.extend(chunk.splitlines())
            return out
```

### B.5.3 `LogWatcher` — 周期扫描 + ERROR/WARNING 批量推送

```python
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

class LogWatcher:
    """每 N 秒调用一次,把新产生的 ≥MIN_LEVEL 日志批量推送到 /notify。"""

    def __init__(self, *, tailer: LogTailer, send, interval: float = 5.0,
                 min_level: str = "WARNING", batch_max: int = 50):
        self.tailer = tailer
        self.send = send  # callable(task_id, type, result)
        self.interval = interval
        self.min_level_num = _LEVEL_ORDER.get(min_level.upper(), 30)
        self.batch_max = batch_max
        self._last_lines: List[str] = []   # 已经推送过的行(用于去重,避免重复触发)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="LogWatcher")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[DEBUG] LogWatcher tick error: {e}")
            self._stop.wait(self.interval)

    def _tick(self):
        new_lines = self.tailer.read_incremental()
        # 去重:与上一批比较
        if self._last_lines and new_lines:
            # 简单策略:如果新内容的前缀和 _last_lines 后缀重叠,跳过重叠部分
            overlap = 0
            max_check = min(len(self._last_lines), len(new_lines))
            for i in range(max_check, 0, -1):
                if self._last_lines[-i:] == new_lines[:i]:
                    overlap = i
                    break
            new_lines = new_lines[overlap:]

        # 解析 + 过滤等级
        kept = []
        for raw in new_lines:
            entry = _parse_log_line(raw)
            if entry is None:
                continue
            level = entry.get("level", "UNKNOWN")
            if _LEVEL_ORDER.get(level, 0) >= self.min_level_num:
                kept.append(entry)
        if not kept:
            self._last_lines = (self._last_lines + new_lines)[-1000:]
            return

        # 限制 batch 大小
        truncated = len(kept) > self.batch_max
        kept = kept[-self.batch_max:]
        self._last_lines = (self._last_lines + new_lines)[-1000:]

        result = {
            "new_count": len(kept),
            "truncated": truncated,
            "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            "entries": kept,
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": time.time(),
            "boot_anchor": BOOT_ANCHOR_LOCAL,
            "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
        }
        self.send(
            task_id=f"event-log-errors-{int(time.time()*1000)}",
            ntype="log_errors",
            result=result,
        )
```

### B.5.4 `PowerBuffer`

```python
class PowerBuffer:
    """线程安全功率环形缓冲(默认 600 样本 = 60 s @ 10 Hz)。"""
    def __init__(self, capacity: int):
        self.capacity = max(8, capacity)
        self._buf: Deque[Tuple[float, float]] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    def push(self, ts: float, v: float):
        with self._lock:
            self._buf.append((ts, v))

    def snapshot(self, window_s: Optional[int] = None) -> List[Tuple[float, float]]:
        cutoff = (time.time() - window_s) if window_s else None
        with self._lock:
            data = list(self._buf)
        return [(t, v) for (t, v) in data if cutoff is None or t >= cutoff]
```

### B.5.5 `LockEventAggregator`

```python
class LockEventAggregator:
    """锁定事件滑窗去抖 + 摘要推送。
    - 60 s 内 ≥5 次 lock 翻转 → POST /notify (type=lock_anomaly)
    - 触发后进入 60 s cooldown
    - auto_relock_status 变为 failed/lost_during_settle → 立即推送 (type=relock_failed)
    """
    IMMEDIATE = {"failed", "lost_during_settle"}

    def __init__(self, *, send, source="pdh-locking",
                 window=60, threshold=5, cooldown=60):
        self.send = send
        self.source = source
        self.window = window
        self.threshold = threshold
        self.cooldown = cooldown
        self._events: Deque[Tuple[float, bool, bool]] = deque()
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def on_lock_change(self, new: bool, prev: bool):
        if new == prev:
            return
        now = time.time()
        fire = None
        with self._lock:
            self._events.append((now, bool(prev), bool(new)))
            cutoff = now - self.window
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
            if now >= self._cooldown_until and len(self._events) >= self.threshold:
                fire = list(self._events)
                self._events.clear()
                self._cooldown_until = now + self.cooldown
        if fire:
            self.send(
                task_id=f"event-lock-anomaly-{int(now*1000)}",
                ntype="lock_anomaly",
                result=self._build_digest(fire),
            )

    def on_auto_relock_status(self, status: str):
        if status not in self.IMMEDIATE:
            return
        ts = time.time()
        self.send(
            task_id=f"event-relock-{status}-{int(ts*1000)}",
            ntype="relock_failed",
            result={
                "status": status,
                "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "epoch": ts,
                "since_boot_seconds": round(ts - BOOT_ANCHOR_EPOCH, 2),
                "boot_anchor": BOOT_ANCHOR_LOCAL,
                "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
                "message": (f"Auto-relock entered unrecoverable state: {status}. "
                            f"Human intervention recommended."),
            },
        )

    def recent(self, window_min: Optional[int] = None) -> List[dict]:
        now = time.time()
        cutoff = (now - window_min * 60) if window_min else None
        with self._lock:
            data = list(self._events)
        if cutoff:
            data = [(t, o, n) for (t, o, n) in data if t >= cutoff]
        return [
            {"ts": t, "prev": o, "new": n,
             "kind": "lock_acquired" if n and not o else "lock_lost"}
            for (t, o, n) in data
        ]

    @staticmethod
    def _build_digest(events: List[Tuple[float, bool, bool]]) -> dict:
        acq = sum(1 for _, o, n in events if n and not o)
        lst = sum(1 for _, o, n in events if not n and o)
        t0, t1 = events[0][0], events[-1][0]
        return {
            "count": len(events),
            "window_seconds": round(t1 - t0, 2),
            "lock_acquired": acq,
            "lock_lost": lst,
            "events": [
                {"ts": t, "prev": o, "new": n,
                 "kind": "lock_acquired" if n and not o else "lock_lost"}
                for (t, o, n) in events
            ],
            "suggestion": ("Lock is flapping. Run /lock/status, "
                           "/power/recent?window_seconds=30, "
                           "consider relaxing PID or widening watch_lock_threshold."),
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": time.time(),
            "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            "boot_anchor": BOOT_ANCHOR_LOCAL,
            "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
        }
```

## B.6 `MainWindowAPIBridge.__init__` 末尾追加订阅

紧接现有 `self._ws_client.start()` 之后:

```python
self._log_tailer = LogTailer(_resolve_log_path(), LINIEN_LOG_BACKUPS)
self._log_watcher = LogWatcher(
    tailer=self._log_tailer,
    send=lambda task_id, ntype, result:
        self._ws_client._send_notification_async(
            task_id,
            {
                "event_type": ntype,
                **result,
                "boot_anchor": BOOT_ANCHOR_LOCAL,
                "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
                "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            },
        ),
    interval=LINIEN_LOG_PUSH_INTERVAL_SEC,
    min_level=LINIEN_LOG_PUSH_MIN_LEVEL,
    batch_max=LINIEN_LOG_PUSH_BATCH_MAX,
)
self._log_watcher.start()

self._power_buffer = PowerBuffer(POWER_BUFFER_CAPACITY)
self._lock_prev: Optional[bool] = None
self._lock_aggregator = LockEventAggregator(
    send=lambda task_id, ntype, result:
        self._ws_client._send_notification_async(
            task_id,
            {
                "event_type": ntype,
                **result,
                "boot_anchor": BOOT_ANCHOR_LOCAL,
                "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
                "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            },
        ),
    window=LINIEN_LOCK_WINDOW_SECONDS,
    threshold=LINIEN_LOCK_THRESHOLD_COUNT,
    cooldown=LINIEN_LOCK_COOLDOWN_SECONDS,
)

panel = self.locking_panel
params = getattr(panel, "parameters", None)
if params is not None:
    self._lock_prev = bool(params.lock.value)
    params.lock.add_callback(self._on_lock_change_cb)
    params.auto_relock_status.add_callback(self._on_auto_relock_cb)
    params.to_plot.add_callback(self._on_to_plot_cb)
```

## B.7 三个线程安全的回调包装方法

```python
def _on_lock_change_cb(self, new):
    prev, self._lock_prev = self._lock_prev, bool(new)
    _event_queue.put(lambda: self._lock_aggregator.on_lock_change(bool(new), bool(prev)))

def _on_auto_relock_cb(self, status):
    _event_queue.put(lambda: self._lock_aggregator.on_auto_relock_status(status))

def _on_to_plot_cb(self, blob):
    _event_queue.put(lambda: self._ingest_power(blob))

def _ingest_power(self, blob):
    try:
        data = pickle.loads(blob) if blob else {}
    except Exception:
        return
    v = None
    for k in ("power_adc_a", "power_adc_b", "slow"):
        raw = data.get(k)
        if raw is None:
            continue
        try:
            v = float(raw[-1] if hasattr(raw, "__getitem__") else raw)
            break
        except Exception:
            v = None
    if v is None:
        es = data.get("error_signal")
        if es is not None and len(es) > 0:
            try:
                v = float(abs(sum(es) / len(es)))
            except Exception:
                return
        else:
            return
    self._power_buffer.push(time.time(), v)
```

> **线程模型**:`parameters.*.add_callback` 在 server 线程触发回调;通过 `_event_queue` 投递到 Qt 主线程消费,沿用 `process_commands` 路径。

## B.8 新增三个 HTTP 端点

在 `_register_routes` 末尾追加:

```python
@self.app.get("/logs/recent")
def get_logs_recent(lines: int = LINIEN_LOG_DEFAULT_LINES,
                    level: Optional[str] = None,
                    grep: Optional[str] = None):
    """Tail linien.log, parse, filter, return JSON.
    Query params:
      lines : 最多返回的解析行数 (tail-most,默认 100)
      level : 级别精确过滤,例 ERROR/WARNING/INFO (大小写不敏感)
      grep  : 在 message 上做子串过滤 (大小写不敏感)
    返回值含 local_time / epoch / boot_anchor / since_boot_seconds
    以便 LabPilot 知道何时发生、本次开机后多久发生。
    """
    def _job():
        raw = self._log_tailer.read_incremental()
        # 首次冷启动 fallback:取文件末尾 64KB 兜底
        if not raw and not getattr(self._log_tailer, "_warmed", False):
            self._log_tailer._warmed = True
            p = _resolve_log_path()
            if p.exists():
                with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                    f.seek(0, 2); size = f.tell()
                    f.seek(max(0, size - 65536))
                    raw = f.read().splitlines()
                    self._log_tailer._offsets[str(p)] = size
        parsed = []
        for r in raw:
            e = _parse_log_line(r)
            parsed.append(e if e else {"ts": None, "logger": None,
                                       "level": "UNKNOWN", "message": r})
        if level:
            L = level.upper()
            parsed = [e for e in parsed if (e["level"] or "").upper() == L]
        if grep:
            g = grep.lower()
            parsed = [e for e in parsed if g in (e["message"] or "").lower()]
        parsed = parsed[-lines:]
        return {
            "lines": parsed,
            "count": len(parsed),
            "total_bytes_read": self._log_tailer._bytes_read,
            "log_path": str(_resolve_log_path()),
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": time.time(),
            "boot_anchor": BOOT_ANCHOR_LOCAL,
            "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
            "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            "filter_hints": "ts 是本地时间字符串(linien 端 asctime 格式),不需要做时区换算",
        }
    return self._invoke(_job)

@self.app.get("/power/recent")
def get_power_recent(window_seconds: int = LINIEN_POWER_BUFFER_SECONDS):
    """最近功率时序(仅查询,不主动推送)。"""
    def _job():
        snap = self._power_buffer.snapshot(window_seconds)
        t0 = snap[0][0] if snap else None
        return {
            "window_seconds": window_seconds,
            "count": len(snap),
            "capacity": self._power_buffer.capacity,
            "cold_start": len(snap) == 0,
            "samples": [{"t_offset": round(t - (t0 or t), 3),
                         "epoch": round(t, 3),
                         "local_time": datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"),
                         "power_dbm": v}
                        for (t, v) in snap] if t0 else [],
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": time.time(),
            "boot_anchor": BOOT_ANCHOR_LOCAL,
            "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
            "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
            "unit_note": "power_dbm 当前是 ADC counts(非校准 dBm)",
        }
    return self._invoke(_job)

@self.app.get("/lock/recent")
def get_lock_recent(window_minutes: int = 5):
    """当前锁定事件滑窗(默认 5 分钟)。"""
    def _job():
        events = self._lock_aggregator.recent(window_minutes)
        return {
            "window_minutes": window_minutes,
            "count": len(events),
            "config": {
                "threshold": LINIEN_LOCK_THRESHOLD_COUNT,
                "cooldown_seconds": LINIEN_LOCK_COOLDOWN_SECONDS,
                "window_seconds": LINIEN_LOCK_WINDOW_SECONDS,
            },
            "events": events,
            "current_lock_state": bool(self._lock_prev),
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "epoch": time.time(),
            "boot_anchor": BOOT_ANCHOR_LOCAL,
            "boot_anchor_epoch": BOOT_ANCHOR_EPOCH,
            "since_boot_seconds": round(time.time() - BOOT_ANCHOR_EPOCH, 2),
        }
    return self._invoke(_job)
```

## B.9 验证步骤

接手者实施后跑:

1. **冷启动日志查询**:启动 GUI,**立即**调用 `curl "http://127.0.0.1:8001/logs/recent?lines=20"` 应返回解析后的历史日志(含 `local_time`/`boot_anchor`/`since_boot_seconds`)。
2. **ERROR 推送**:故意制造一个 ERROR(如 `logging.getLogger("linien_server.foo").error("test for pdh push")`),等 5–10 秒,LabPilot NotificationQueue 应收到一条 `type=log_errors`,entries 里含该条。
3. **滑窗去抖**:60 s 内手动 `/lock/manual` + `/lock/stop` 交替 6 次,LabPilot 应收到 **恰好 1 条** `type=lock_anomaly`(digest,count=6)。
4. **立即触发**:故意把 `parameters.auto_relock_status` 设成 `failed`(可在调试菜单或 rpyc 命令),LabPilot 应立即收到 1 条 `type=relock_failed`,**无需等阈值**。
5. **功率被动模式**:`/power/recent` 数据持续更新,**不**触发任何 `/notify`。
6. **boot anchor 一致性**:连续两次 `/logs/recent` 调用返回的 `boot_anchor_epoch` 应相同;与首条 `/notify` 推送里的 `boot_anchor_epoch` 一致。

## B.10 关键技术决策回顾

| 决策 | 选择 | 理由 |
|---|---|---|
| 服务形态 | 扩展 linien GUI `api_bridge.py`(8001) | 实验室现有 GUI 已是 PDH 服务 |
| 推送通道 | HTTP POST `/notify`(复用 `_send_notification_async`) | 与 PI 计算任务完成通知同 schema,HTTP 比 WebSocket 更可靠 |
| lock_anomaly 触发 | 60s/5次 + 60s cooldown | 避免噪声 |
| relock_failed 触发 | 立即推送 | 严重事件不可恢复,需要人工介入 |
| ERROR 日志触发 | 5s 批量推送,min level 默认 WARNING | 比 lock_anomaly 噪音更低 |
| 功率推送 | **不**主动推送 | 用户明确:功率本身持续抖动 |
| 日志路径 | `appdirs.AppDirs("linien").user_data_dir` | 与 linien server 同源,不硬编码 |
| 线程模型 | 回调 → `_event_queue` → Qt 主线程 `process_commands` | 与现有 `_invoke` 一致 |
| 时间字段 | local_time (本地时间字符串 + 时区) + epoch + since_boot_seconds | 一致带时区,无须时钟协议 |
| AI 接口 | 继续 curl | 用户明确不引入 LangChain 类型化工具 |

---

## 给 LabPilot 侧的反馈要求

完成后,请把以下信息反馈给 LabPilot 智能体,以便我方同步修改 `SKILL.md` 和 NotificationHub:

1. **实际字段命名**:如果实施过程中调整了任何 result 字段名(如 `entries`/`events`/`lock_acquired` 等),请列出最终命名。
2. **实际推送的 `event_type` 字符串**(本设计假设 `lock_anomaly` / `relock_failed` / `log_errors`)。
3. **实际推送时的 `task_id` 前缀格式**(如 `event-lock-anomaly-*` 是否被替换)。
4. **`auto_relock_status` 的实际合法值集合**(本设计假设 `idle | running | success | failed | settled | unstable | lost_during_settle`)。
5. **`parameters.to_plot` 里实际可用的功率键**(本设计尝试 `power_adc_a` / `power_adc_b` / `slow`,如另有 `slow_out` 等键请告知)。
6. **`MainWindowAPIBridge.__init__` 里能否访问 `self.locking_panel.parameters`**(若不能,需要找到等价入口)。
7. **`logging.getLogger("linien_server.foo").error(...)` 是否会真的写入 `linien.log`**(即 `LOG_FILE_PATH` 配置是否生效)。
