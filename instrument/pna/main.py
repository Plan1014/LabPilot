"""PNA Service FastAPI application.

Results are pushed to NotificationHub at port 8000 via HTTP POST.
"""

import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException

from instrument.pna.config import PNA_PORT
from instrument.pna.schemas import (
    MeasureRequest,
    MeasureResponse,
    TaskStatus,
    HealthResponse,
)
from instrument.pna.task_manager import task_manager
from instrument.pna.pna_instrument import PNAInstrument

# NotificationHub address
NOTIFICATION_HUB_URL = "http://127.0.0.1:8000"

# Global PNA connection (persistent, long-lived)
_pna: Optional[PNAInstrument] = None


# ==================== Task Callback ====================

def measurement_callback(task_id: str, status: str, result: dict):
    """Called when measurement completes. Posts result to NotificationHub."""
    if status == "completed":
        task_manager.complete_task(
            task_id,
            result=result,
            csv_path=result.get("csv_path", ""),
        )
        _post_notification({
            "source": "pna",
            "task_id": task_id,
            "type": "task_completed",
            "result": result,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
    else:
        task_manager.fail_task(task_id, error=result.get("error", "Unknown error"))
        _post_notification({
            "source": "pna",
            "task_id": task_id,
            "type": "task_failed",
            "error": result.get("error", "Unknown error"),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })


def _post_notification(payload: dict):
    """Post notification to NotificationHub asynchronously."""
    def send():
        try:
            import requests
            requests.post(f"{NOTIFICATION_HUB_URL}/notify", json=payload, timeout=5)
        except Exception as e:
            print(f"Failed to post notification: {e}")

    thread = threading.Thread(target=send, daemon=True)
    thread.start()


def _run_measurement_with_connection(task_id: str, start_freq: int, stop_freq: int, csv_filename: str, callback=None):
    """Run measurement using the persistent PNA connection."""
    try:
        # Configure using persistent connection
        _pna.configure(start_freq, stop_freq)
        trace_data = _pna.measure()
        csv_path = _pna.save(trace_data, csv_filename)

        result = {
            "status": "success",
            "csv_path": str(csv_path),
            "trace_points": len(trace_data) // 2,
        }
        if callback:
            callback(task_id, "completed", result)
    except Exception as e:
        print(f"PNA measurement error: {e}")
        result = {"status": "failed", "error": str(e)}
        if callback:
            callback(task_id, "failed", result)


# ==================== FastAPI App ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pna
    _pna = PNAInstrument()
    try:
        _pna.connect()
        print(f"PNA connected: {_pna._pna.query_str('*IDN?')}")
    except Exception as e:
        print(f"PNA connection failed: {e}")
        _pna = None
    yield
    if _pna:
        _pna.disconnect()


app = FastAPI(title="PNA Service", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", pna_connected=_pna is not None and _pna._pna is not None)


@app.post("/measure", response_model=MeasureResponse)
async def start_measurement(req: MeasureRequest):
    """Start a PNA measurement."""
    if _pna is None or _pna._pna is None:
        raise HTTPException(status_code=503, detail="PNA not connected")

    if task_manager.has_running_task():
        raise HTTPException(
            status_code=400,
            detail="Measurement already in progress. Please wait for completion."
        )

    task_id = task_manager.create_task()

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    csv_filename = req.csv_filename or f"trace_{timestamp}.csv"

    task_manager.update_task(task_id, status="running")

    thread = threading.Thread(
        target=_run_measurement_with_connection,
        args=(task_id, req.start_freq, req.stop_freq, csv_filename),
        kwargs={"callback": measurement_callback},
    )
    thread.start()

    return MeasureResponse(task_id=task_id, status="pending")


@app.get("/measure/{task_id}", response_model=TaskStatus)
async def get_measurement_status(task_id: str):
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatus(**task)


@app.post("/measure/{task_id}/cancel")
async def cancel_measurement(task_id: str):
    success = task_manager.cancel_task(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot cancel task (not found or already completed)",
        )
    return {"status": "cancelled", "task_id": task_id}


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PNA_PORT)
