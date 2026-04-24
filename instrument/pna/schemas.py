"""Pydantic schemas for PNA Service API."""

from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field


class MeasureRequest(BaseModel):
    """Request to start a PNA measurement."""
    start_freq: int = Field(default=1, description="Start frequency in Hz")
    stop_freq: int = Field(default=100000, description="Stop frequency in Hz")
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