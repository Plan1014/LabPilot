"""Pydantic schemas for PNA Service API."""

from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field


class MeasureRequest(BaseModel):
    """Request to start a PNA measurement."""
    start_freq: int = Field(default=1, description="Start frequency in Hz")
    stop_freq: int = Field(default=1000000, description="Stop frequency in Hz")
    csv_filename: Optional[str] = Field(default=None, description="Output CSV filename")


class MeasureResponse(BaseModel):
    """Response after starting a measurement."""
    task_id: str
    status: str = "pending"


class TaskStatus(BaseModel):
    """Full task status response."""
    task_id: str
    status: str  # pending | running | completed | failed | cancelled
    created_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    csv_path: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    pna_connected: bool = False


class WebSocketMessage(BaseModel):
    """WebSocket notification payload."""
    type: str  # measurement_complete | measurement_failed
    task_id: str
    csv_path: Optional[str] = None
    summary: Optional[dict] = None
    error: Optional[str] = None


class ReadPnaRequest(BaseModel):
    """Request to extract key frequency points from a PNA measurement CSV.

    ``csv_path`` may be absolute, or relative to ``PNA_DATA_DIR``.
    """
    csv_path: str = Field(
        description="Path to CSV file (absolute, or relative to PNA_DATA_DIR)"
    )
    target_freqs: List[float] = Field(
        description="Target frequencies in Hz"
    )
    tolerance_factor: float = Field(
        default=0.05,
        description="Per-frequency tolerance as fraction of target (default 0.05 = 5%)",
    )


class KeyPoint(BaseModel):
    """One matched frequency/power pair."""
    frequency_hz: float
    power_dbm: float


class ReadPnaResponse(BaseModel):
    """Result of ``POST /read_pna``."""
    csv_path: str  # resolved absolute path actually read
    points: List[KeyPoint]
    missing: List[float] = []