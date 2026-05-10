# LabPilot Dev Environment Launcher
# Run: .\start-dev.ps1
# Starts: Python Backend → Vite Frontend (in separate windows)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== LabPilot Dev Launcher ===" -ForegroundColor Cyan

# 1. Kill any process on port 8000
Write-Host "`n[1/3] Checking port 8000..." -ForegroundColor Yellow
$portCheck = netstat -ano | findstr :8000 | findstr LISTENING
if ($portCheck) {
    $oldPid = ($portCheck -split '\s+')[-1]
    Write-Host "  Killing old process on port 8000 (PID: $oldPid)..." -ForegroundColor Yellow
    Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}
Write-Host "  Port 8000 is free" -ForegroundColor Green

# 2. Start Python backend in new window
Write-Host "`n[2/3] Starting Python backend on port 8000..." -ForegroundColor Yellow
$venvPython = "$projectRoot\venv\Scripts\python.exe"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$projectRoot'; & '$venvPython' -m uvicorn src.agent.websocket_server:create_notification_hub_app --factory --host 127.0.0.1 --port 8000"

Write-Host "  Backend window opened" -ForegroundColor Green

# Wait for backend to be ready
Start-Sleep -Seconds 3
if ((netstat -ano | findstr :8000 | findstr LISTENING)) {
    Write-Host "  Backend ready on port 8000" -ForegroundColor Green
} else {
    Write-Host "  Warning: Backend may not have started properly" -ForegroundColor Yellow
}

# 3. Start Vite frontend in new window
Write-Host "`n[3/3] Starting Vite frontend..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$projectRoot\frontend'; npm run dev"

Write-Host "`n=== All services started ===" -ForegroundColor Green
Write-Host "Two windows should be open:" -ForegroundColor White
Write-Host "  1. Backend (Python uvicorn)" -ForegroundColor White
Write-Host "  2. Frontend (Vite dev server)" -ForegroundColor White
