# LabPilot Dev Environment Launcher
# Run: .\start-dev.ps1
# Starts: Python Backend → Vite Frontend (in separate windows)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== LabPilot Dev Launcher ===" -ForegroundColor Cyan

# ================= Pre-flight checks =================
Write-Host "`n[Pre-flight] Environment checks..." -ForegroundColor Yellow

# .env
if (-not (Test-Path "$projectRoot\.env")) {
    Write-Host "  ! .env not found." -ForegroundColor Yellow
    Write-Host "    Run:  cp .env.example .env   then fill ANTHROPIC_API_KEY" -ForegroundColor Yellow
    Write-Host "    Continuing anyway (backend will start but may 401 without API key)..." -ForegroundColor DarkYellow
} else {
    # 如果用户填的是 OPENAI_API_KEY，提示会用 ChatOpenAI 直连上游
    $envContent = Get-Content "$projectRoot\.env" -Raw
    if ($envContent -match '(?m)^OPENAI_API_KEY\s*=\s*\S+') {
        $hasAnthropic = $envContent -match '(?m)^ANTHROPIC_API_KEY\s*=\s*\S+'
        $hasLiteLLM = $envContent -match '(?m)^LITELLM_MODEL\s*=\s*\S+'
        if (-not $hasAnthropic) {
            Write-Host "  ℹ  OPENAI_API_KEY detected. Will connect via ChatOpenAI directly (no LiteLLM Proxy)." -ForegroundColor Cyan
            if (-not $hasLiteLLM) {
                Write-Host "    ! LITELLM_MODEL is empty — set it (e.g. qwen2.5-14b-instruct or gpt-4o)" -ForegroundColor Yellow
            }
        }
    }
}

# frontend/node_modules
$frontendNodeModules = "$projectRoot\frontend\node_modules"
if (-not (Test-Path $frontendNodeModules)) {
    Write-Host "  ! frontend/node_modules not found." -ForegroundColor Yellow
    $ans = Read-Host "    Run 'npm install' in frontend/ now? [Y/n]"
    if ($ans -eq '' -or $ans -match '^[Yy]') {
        Write-Host "    Installing frontend dependencies (may take a few minutes)..." -ForegroundColor Cyan
        Push-Location "$projectRoot\frontend"
        try {
            & npm install
            if ($LASTEXITCODE -ne 0) {
                throw "npm install failed with exit code $LASTEXITCODE"
            }
        } finally {
            Pop-Location
        }
        Write-Host "    Frontend dependencies installed." -ForegroundColor Green
    } else {
        Write-Host "    Skipped. 'npm run dev' will fail until you run 'npm install'." -ForegroundColor Yellow
    }
}

# ================= 1. Kill any process on port 8000 =================
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

# Keep sentence_transformers from phoning home on every startup.
# archival_store.py also setdefaults these; this is for direct invocations.
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"

# Prefer uv-managed .venv; fall back to legacy venv for backwards compat
$venvPython = "$projectRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $legacyVenv = "$projectRoot\venv\Scripts\python.exe"
    if (Test-Path $legacyVenv) {
        Write-Host "  .venv not found, falling back to legacy venv\" -ForegroundColor DarkYellow
        $venvPython = $legacyVenv
    } else {
        Write-Host "  ERROR: No Python virtual environment found." -ForegroundColor Red
        Write-Host "  Run 'uv sync' first to create .venv\" -ForegroundColor Red
        exit 1
    }
}

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
