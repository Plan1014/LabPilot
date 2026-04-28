# LabPilot Tauri dev environment setup
# Run: .\start-tauri.ps1
# Copy this file to start-tauri.ps1 and update the MSVC paths for your machine

$ErrorActionPreference = "Stop"

# === MSVC Build Tools (update these paths for your machine) ===
$env:PATH = "D:\Program Files (x86)\Microsoft Visual Studio\<version>\VC\Tools\MSVC\<version>\bin\Hostx64\x64;" + $env:PATH
$env:LIB = "D:\Program Files (x86)\Microsoft Visual Studio\<version>\VC\Tools\MSVC\<version>\lib\x64;C:\Program Files (x86)\Windows Kits\10\lib\<version>\um\x64;C:\Program Files (x86)\Windows Kits\10\lib\<version>\ucrt\x64;" + $env:LIB
$env:INCLUDE = "D:\Program Files (x86)\Microsoft Visual Studio\<version>\VC\Tools\MSVC\<version>\include;C:\Program Files (x86)\Windows Kits\10\include\<version>\ucrt;C:\Program Files (x86)\Windows Kits\10\include\<version>\um;C:\Program Files (x86)\Windows Kits\10\include\<version>\shared;" + $env:INCLUDE

Write-Host "MSVC env ready" -ForegroundColor Green

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = "$projectRoot\venv\Scripts\python.exe"

# Start Python backend (NotificationHub) in background
Write-Host "Starting Python backend on port 8000..." -ForegroundColor Cyan
$backendProcess = Start-Process -FilePath $venvPython `
    -ArgumentList "-m", "uvicorn", "src.agent.websocket_server:create_notification_hub_app", "--factory", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2

# Verify backend is running
try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/docs" -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue
    if ($response.StatusCode -eq 200) {
        Write-Host "Backend ready on port 8000" -ForegroundColor Green
    }
} catch {
    Write-Host "Warning: Backend may not be ready yet" -ForegroundColor Yellow
}

Set-Location "$projectRoot\frontend"

Write-Host "Starting Tauri dev server..." -ForegroundColor Cyan
& 'C:\Users\Charleslee\.cargo\bin\cargo.exe' tauri dev

# Cleanup backend on exit
if ($backendProcess -and !$backendProcess.HasExited) {
    Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
}
