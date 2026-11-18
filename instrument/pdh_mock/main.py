"""Mock PDH-Locking service (linien-style) for LabPilot.

Simulates the linien FastAPI service that normally runs on RedPitaya, exposing
the endpoints documented in ``skills/pdh-locking/SKILL.md``. POSTs async task
results to LabPilot NotificationHub (port 8000) the same way the real linien
service does.

Start:
    uv run python -m instrument.pdh_mock.main

Endpoints mirror the real linien service:
    GET  /health
    POST /pi/calculate                  -> async, posts task_completed via /notify
    GET  /task/{task_id}                -> query PI calculation result
    GET  /lock/status                   -> {"locked": bool}
    POST /lock/manual                   -> acquire lock; posts lock events on settle
    POST /lock/stop                    -> release lock
    GET  /lock/recent?window_minutes=N  -> lock history
    POST /pid/set                       -> {"kp","ki","kd"} 0..8191
    POST /modulation/set                -> {"frequency_mhz","amplitude_vpp"}
    POST /plot/export                   -> writes CSV-ish waveform stub
    GET  /power/monitor                 -> single reading
    GET  /power/recent?window_seconds=N -> time series + stats
    GET  /logs/recent?lines=N&level=... -> fake log buffer

Admin (not in real linien — for local demo only):
    POST /admin/force_task_completed    -> synthesise task_completed push
    POST /admin/force_lock_anomaly      -> synthesise lock_anomaly push
    POST /admin/force_relock_failed     -> synthesise relock_failed push
    POST /admin/force_log_errors        -> synthesise log_errors push
    GET  /admin/state                   -> dump full mock state as JSON
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Deque

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import httpx

# ---------- Config ----------

HUB_URL = os.getenv("NOTIFICATION_HUB_URL", "http://127.0.0.1:8000")
PID_RANGE = (0, 8191)
MOD_FREQ_RANGE = (0.0, 99.0)
MOD_AMP_RANGE = (0.0, 2.0)
PLOT_DIR = Path(os.getenv("PDH_MOCK_PLOT_DIR", "tmp/pdh_mock_plots")).resolve()
PLOT_DIR.mkdir(parents=True, exist_ok=True)

BOOT_ANCHOR = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _since_boot() -> float:
    """Seconds since this mock process started (counterpart of linien's)."""
    return round(time.time() - _BOOT_TS, 1)


_BOOT_TS = time.time()

app = FastAPI(title="PDH Mock (linien-style)", version="mock-1.0")

# ---------- State (in-memory) ----------

STATE: dict[str, Any] = {
    "locked": False,
    "kp": 1000,
    "ki": 500,
    "kd": 50,
    "frequency_mhz": 10.5,
    "amplitude_vpp": 1.2,
    "power_dbm": -12.34,        # current target; real linien reads GUI label
    "auto_relock_status": "stopped",
    "watch_lock_threshold": 0.05,
}

_tasks: dict[str, dict[str, Any]] = {}           # task_id -> {status, P, I, ...}
_lock_events: Deque[dict[str, Any]] = deque(maxlen=200)
_power_samples: Deque[dict[str, Any]] = deque(maxlen=1800)  # ~60s at 30 Hz
_log_buffer: Deque[dict[str, Any]] = deque(maxlen=500)


def _now_local() -> str:
    """Local time string matching linien's asctime style (with TZ offset)."""
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")


def _push_to_hub(type_: str, result: dict, task_id: str | None = None) -> None:
    """POST an event to NotificationHub (/notify). Fire-and-forget."""
    payload = {
        "source": "pdh-locking",
        "task_id": task_id or str(uuid.uuid4()),
        "type": type_,
        "result": result,
    }

    async def _post() -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                await client.post(f"{HUB_URL}/notify", json=payload)
            except Exception as exc:  # don't crash the mock if hub is down
                _log("WARNING", f"hub POST failed: {exc}")

    asyncio.create_task(_post())


def _log(level: str, message: str) -> None:
    entry = {
        "ts": _now_local(),
        "logger": "linien_server.mock",
        "level": level,
        "message": message,
    }
    _log_buffer.append(entry)


# ---------- Pydantic request models ----------


class PIDBody(BaseModel):
    kp: int = Field(ge=PID_RANGE[0], le=PID_RANGE[1])
    ki: int = Field(ge=PID_RANGE[0], le=PID_RANGE[1])
    kd: int = Field(ge=PID_RANGE[0], le=PID_RANGE[1])


class ModulationBody(BaseModel):
    frequency_mhz: float = Field(ge=MOD_FREQ_RANGE[0], le=MOD_FREQ_RANGE[1])
    amplitude_vpp: float = Field(ge=MOD_AMP_RANGE[0], le=MOD_AMP_RANGE[1])


class PlotExportBody(BaseModel):
    save_path: str = "spectrum.png"


# ---------- Background workers ----------


@app.on_event("startup")
async def _start_workers() -> None:
    """Power simulation runs forever; power is 30 Hz (matches real linien rate)."""

    async def power_sim() -> None:
        while True:
            # Random walk around current power_dbm
            STATE["power_dbm"] = round(STATE["power_dbm"] + random.gauss(0, 0.05), 3)
            _power_samples.append({
                "t_offset": round(time.time() - _BOOT_TS, 3),
                "epoch": time.time(),
                "local_time": _now_local(),
                "power_dbm": STATE["power_dbm"],
            })
            await asyncio.sleep(1 / 30)

    asyncio.create_task(power_sim())


# ---------- Core endpoints (mirror real linien) ----------


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "mock": True, "since_boot_seconds": _since_boot(),
            "boot_anchor": BOOT_ANCHOR}


@app.post("/pi/calculate")
async def pi_calculate() -> dict[str, str]:
    """Async PI calculation. Returns task_id immediately; posts result later."""
    if STATE["locked"]:
        raise HTTPException(409, "Cannot run PI calculation while locked")

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "pending", "started_at": _since_boot()}
    _log("INFO", f"PI calculation started task_id={task_id}")

    async def _run_pi() -> None:
        await asyncio.sleep(random.uniform(2.0, 4.0))
        # Mock optimiser output — random-but-plausible
        P = random.randint(800, 4000)
        I = random.randint(200, 6000)
        _tasks[task_id] = {"status": "success", "P": P, "I": I,
                            "finished_at": _since_boot()}
        STATE["kp"] = P
        STATE["ki"] = I
        _push_to_hub(
            "task_completed",
            {"status": "success", "P": P, "I": I,
             "since_boot_seconds": _since_boot(),
             "boot_anchor": BOOT_ANCHOR,
             "local_time": _now_local(),
             "task_id": task_id},
            task_id=task_id,
        )
        _log("INFO", f"PI calculation done task_id={task_id} P={P} I={I}")

    asyncio.create_task(_run_pi())
    return {"task_id": task_id, "status": "pending"}


@app.get("/task/{task_id}")
async def task_result(task_id: str) -> dict[str, Any]:
    rec = _tasks.get(task_id)
    if not rec:
        raise HTTPException(404, "Task not found or expired")
    return {"task_id": task_id, **rec}


@app.get("/lock/status")
async def lock_status() -> dict[str, Any]:
    return {"locked": STATE["locked"],
            "auto_relock_status": STATE["auto_relock_status"]}


@app.post("/lock/manual")
async def lock_manual() -> dict[str, Any]:
    STATE["locked"] = True
    STATE["auto_relock_status"] = "running"
    _lock_events.append({"t": _since_boot(), "event": "locked",
                        "local_time": _now_local()})
    _log("INFO", "manual lock engaged")

    async def _settle() -> None:
        # Simulate settle, then update power (drop if lock worked)
        await asyncio.sleep(2.0)
        STATE["power_dbm"] = round(STATE["power_dbm"] - random.uniform(1.5, 2.5), 3)

    asyncio.create_task(_settle())
    return {"status": "locking", "message": "Lock engaged, settling..."}


@app.post("/lock/stop")
async def lock_stop() -> dict[str, Any]:
    STATE["locked"] = False
    STATE["auto_relock_status"] = "stopped"
    _lock_events.append({"t": _since_boot(), "event": "unlocked",
                        "local_time": _now_local()})
    _log("INFO", "lock released")
    return {"status": "stopped", "message": "Lock successfully stopped"}


@app.get("/lock/recent")
async def lock_recent(window_minutes: int = 60) -> dict[str, Any]:
    cutoff = _since_boot() - window_minutes * 60
    events = [e for e in _lock_events if e["t"] >= cutoff]
    return {"events": events, "count": len(events),
            "window_minutes": window_minutes,
            "since_boot_seconds": _since_boot()}


@app.post("/pid/set")
async def pid_set(body: PIDBody) -> dict[str, Any]:
    STATE.update({"kp": body.kp, "ki": body.ki, "kd": body.kd})
    _log("INFO", f"PID updated kp={body.kp} ki={body.ki} kd={body.kd}")
    return {"status": "updated", "kp": body.kp, "ki": body.ki, "kd": body.kd}


@app.post("/modulation/set")
async def modulation_set(body: ModulationBody) -> dict[str, Any]:
    STATE["frequency_mhz"] = body.frequency_mhz
    STATE["amplitude_vpp"] = body.amplitude_vpp
    _log("INFO", f"modulation freq={body.frequency_mhz}MHz amp={body.amplitude_vpp}Vpp")
    return {"status": "updated",
            "freq_mhz": body.frequency_mhz,
            "amp_vpp": body.amplitude_vpp}


@app.post("/plot/export")
async def plot_export(body: PlotExportBody) -> dict[str, Any]:
    target = PLOT_DIR / Path(body.save_path).name
    # 真正写一个最小 CSV stub (real linien saves a PNG; this is enough for `/plot/export` smoke test)
    target.write_text("t,spectrum\n0,0.0\n1,0.5\n2,0.3\n")
    return {"status": "saved", "path": str(target)}


@app.get("/power/monitor")
async def power_monitor() -> dict[str, Any]:
    return {"power_dbm": STATE["power_dbm"],
            "local_time": _now_local(),
            "since_boot_seconds": _since_boot()}


@app.get("/power/recent")
async def power_recent(window_seconds: int = 30) -> dict[str, Any]:
    cutoff = _since_boot() - window_seconds
    samples = [s for s in _power_samples if s["t_offset"] >= cutoff]
    if not samples:
        return {"status": "cold_start", "samples": [],
                "current_power": f"{STATE['power_dbm']} dBm",
                "rate_limit_hz": 30,
                "data_manager_anchor_epoch": _BOOT_TS}
    powers = [s["power_dbm"] for s in samples]
    return {
        "status": "ok",
        "samples": samples,
        "stats": {"min": min(powers), "max": max(powers),
                  "avg": round(sum(powers) / len(powers), 3),
                  "count": len(powers)},
        "current_power": f"{STATE['power_dbm']} dBm",
        "rate_limit_hz": 30,
        "data_manager_anchor_epoch": _BOOT_TS,
        "since_boot_seconds": _since_boot(),
    }


@app.get("/logs/recent")
async def logs_recent(lines: int = 100, level: str = "INFO",
                      grep: str | None = None) -> dict[str, Any]:
    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    threshold = level_order.get(level.upper(), 1)
    buf = list(_log_buffer)
    if grep:
        buf = [e for e in buf if grep.lower() in e["message"].lower()]
    buf = [e for e in buf if level_order.get(e["level"], 0) >= threshold]
    return {
        "lines": buf[-lines:],
        "count": len(buf),
        "log_path": "<mock-in-memory>",
        "filter_hints": {"level": level, "grep": grep},
        "boot_anchor": BOOT_ANCHOR,
        "since_boot_seconds": _since_boot(),
    }


# ---------- Admin endpoints (mock-only, NOT in real linien) ----------


@app.get("/admin/state")
async def admin_state() -> dict[str, Any]:
    return {
        "state": STATE,
        "tasks_pending": sum(1 for t in _tasks.values() if t["status"] == "pending"),
        "tasks_done":    sum(1 for t in _tasks.values() if t["status"] == "success"),
        "lock_events":   len(_lock_events),
        "power_samples": len(_power_samples),
        "log_entries":   len(_log_buffer),
        "since_boot_seconds": _since_boot(),
        "boot_anchor": BOOT_ANCHOR,
    }


@app.post("/admin/force_task_completed")
async def force_task_completed() -> dict[str, Any]:
    """Synthesise a task_completed push — useful for demo without waiting for PI."""
    P = random.randint(800, 4000)
    I = random.randint(200, 6000)
    _push_to_hub(
        "task_completed",
        {"status": "success", "P": P, "I": I,
         "since_boot_seconds": _since_boot(),
         "boot_anchor": BOOT_ANCHOR,
         "local_time": _now_local()},
        task_id=f"forced-{uuid.uuid4().hex[:8]}",
    )
    return {"pushed": "task_completed", "P": P, "I": I}


@app.post("/admin/force_lock_anomaly")
async def force_lock_anomaly() -> dict[str, Any]:
    msg = ("Lock is flapping. Run /lock/status, /power/recent?window_seconds=30, "
           "consider relaxing PID or widening watch_lock_threshold. "
           f"(6 events in {random.uniform(20, 60):.1f}s, 3 acquired, 3 lost; "
           f"linien 开机后 {_since_boot()}s)")
    _push_to_hub(
        "lock_anomaly",
        {"event_type": "lock_anomaly",
         "message": msg,
         "since_boot_seconds": _since_boot(),
         "boot_anchor": BOOT_ANCHOR,
         "local_time": _now_local()},
    )
    return {"pushed": "lock_anomaly"}


@app.post("/admin/force_relock_failed")
async def force_relock_failed() -> dict[str, Any]:
    _push_to_hub(
        "relock_failed",
        {"event_type": "relock_failed",
         "message": f"Auto-relock entered unrecoverable state: failed. "
                    f"Human intervention recommended. "
                    f"(linien 开机后 {_since_boot()}s)",
         "since_boot_seconds": _since_boot(),
         "boot_anchor": BOOT_ANCHOR,
         "local_time": _now_local()},
    )
    STATE["auto_relock_status"] = "failed"
    return {"pushed": "relock_failed"}


@app.post("/admin/force_log_errors")
async def force_log_errors() -> dict[str, Any]:
    entries = [
        {"ts": _now_local(), "logger": "linien_server.foo",
         "level": "ERROR", "message": "test for pdh push"},
        {"ts": _now_local(), "logger": "linien_server.parameters",
         "level": "WARNING", "message": "watch_lock_threshold too tight"},
        {"ts": _now_local(), "logger": "linien_server.fast_relock",
         "level": "ERROR", "message": "fast_relock failed: timeout"},
    ]
    _push_to_hub(
        "log_errors",
        {"event_type": "log_errors",
         "new_entries": entries,
         "count": len(entries),
         "message": f"{len(entries)} new WARNING/ERROR entries",
         "since_boot_seconds": _since_boot(),
         "boot_anchor": BOOT_ANCHOR,
         "local_time": _now_local()},
    )
    return {"pushed": "log_errors", "count": len(entries)}


# ---------- Entrypoint ----------


def main() -> None:
    import uvicorn
    port = int(os.getenv("PDH_MOCK_PORT", "8001"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
