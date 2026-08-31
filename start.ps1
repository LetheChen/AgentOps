# AgentOps - Start Backend + Frontend (no popup windows)
# Usage:
#   .\start.ps1          Start services (background, no windows)
#   .\start.ps1 -Watch   Start + tail logs in this window (Ctrl+C stops tail, services keep running)
#   .\stop.ps1           Stop services

param(
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:PYTHONIOENCODING = 'utf-8'
$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# --- 1. Check frontend dependencies ---
if (-not (Test-Path "$root\web\node_modules")) {
    Write-Host "[1/4] Installing frontend dependencies..." -ForegroundColor Cyan
    Push-Location "$root\web"
    npm install --silent
    Pop-Location
} else {
    Write-Host "[1/4] Frontend dependencies OK." -ForegroundColor Gray
}

# --- 2. Check Python deps ---
Write-Host "[2/4] Checking backend dependencies..." -ForegroundColor Gray
$pyOk = $false
try {
    $null = python -c "import fastapi, uvicorn, httpx, yaml" 2>&1
    if ($LASTEXITCODE -eq 0) { $pyOk = $true }
} catch {}
if (-not $pyOk) {
    Write-Host "  Installing Python dependencies..." -ForegroundColor Cyan
    pip install fastapi uvicorn httpx pyyaml --quiet
}
Write-Host "  Backend dependencies OK." -ForegroundColor Gray

# --- 3. Start backend (hidden window, log to file) ---
$backendInUse = Get-NetTCPConnection -LocalPort 1987 -State Listen -ErrorAction SilentlyContinue
if ($backendInUse) {
    Write-Host "[3/4] Port 1987 in use, backend may already be running." -ForegroundColor Yellow
} else {
    # 3a. 如果 harness=opencode，先确保 opencode server 跑起来（端口 4096）
    $managerHarness = ""
    if (Test-Path "$root\config\agents\manager.yaml") {
        $mgr = Get-Content "$root\config\agents\manager.yaml" -Raw
        if ($mgr -match '^\s*harness:\s*(\S+)\s*$' -and $Matches[1]) { $managerHarness = $Matches[1].Trim() }
    }
    if ($managerHarness -eq "opencode") {
        $ocUse = Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue
        if (-not $ocUse) {
            Write-Host "  Starting opencode server (port 4096)..." -ForegroundColor Cyan
            $ocLog = Join-Path $logDir 'opencode.log'
            "" | Out-File $ocLog -Encoding utf8
            $ocBat = Join-Path $env:TEMP "agentops_start_opencode.bat"
            @"
@echo off
opencode serve --port 4096 > "$ocLog" 2>&1
"@ | Out-File -FilePath $ocBat -Encoding ascii
            Start-Process -FilePath 'cmd.exe' -ArgumentList @('/K', $ocBat) -WindowStyle Hidden
            # wait for opencode
            for ($j = 1; $j -le 15; $j++) {
                Start-Sleep -Seconds 1
                $chk = Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue
                if ($chk) { Write-Host "  opencode server ready." -ForegroundColor Green; break }
            }
        } else {
            Write-Host "  opencode server already running on 4096." -ForegroundColor Gray
        }
    }
    Write-Host "[3/4] Starting backend API (hidden) on http://localhost:1987 ..." -ForegroundColor Green
    # 清 __pycache__ 防止加载旧 .pyc（尤其切换 harness 后必须清理）
    Write-Host "  Clearing python cache..." -ForegroundColor Gray
    Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $backendLog = Join-Path $logDir 'backend.log'
    # 清空旧日志
    "" | Out-File $backendLog -Encoding utf8
    # 不加 --reload：uvicorn --reload 用 multiprocessing 子进程，
    # 子进程不继承父进程的 ProactorEventLoop policy，会让 codex harness
    # 在 SelectorEventLoop 上跑 asyncio.create_subprocess_exec → NotImplementedError
    # 改用 `python -u -m uvicorn` 直接启动，单进程 + api/server.py:41 设的 policy 生效
    $backendBat = Join-Path $env:TEMP "agentops_start_backend.bat"
    @"
@echo off
cd /d "$root"
set PYTHONIOENCODING=utf-8
python -u -m uvicorn api.server:app --host 127.0.0.1 --port 1987 > "$backendLog" 2>&1
"@ | Out-File -FilePath $backendBat -Encoding ascii
    # 必须用 cmd /K（保持 cmd 运行），否则 /C 会在 uvicorn 启动后立刻退出，连带杀掉 uvicorn
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/K', $backendBat) -WindowStyle Hidden

    # Wait for backend to be ready (真正 HTTP 200，而不是只检查端口 LISTENING)
    Write-Host "  Waiting for backend..." -ForegroundColor Gray
    $ready = $false
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:1987/" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if ($ready) {
        Write-Host "  Backend ready." -ForegroundColor Green
    } else {
        Write-Host "  Backend startup timeout! Check: $backendLog" -ForegroundColor Red
        Get-Content $backendLog -Tail 30 | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
    }
}

# --- 4. Start frontend (hidden window, log to file) ---
$frontendInUse = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($frontendInUse) {
    Write-Host "[4/4] Port 5173 in use, frontend may already be running." -ForegroundColor Yellow
} else {
    Write-Host "[4/4] Starting frontend UI (hidden) on http://localhost:5173 ..." -ForegroundColor Green
    $frontendLog = Join-Path $logDir 'frontend.log'
    "" | Out-File $frontendLog -Encoding utf8
    $frontendBat = Join-Path $env:TEMP "agentops_start_frontend.bat"
    @"
@echo off
cd /d "$root\web"
npm run dev > "$frontendLog" 2>&1
"@ | Out-File -FilePath $frontendBat -Encoding ascii
    # 同 backend：用 /K 保持 cmd 运行
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/K', $frontendBat) -WindowStyle Hidden

    # Wait for frontend to be ready
    Write-Host "  Waiting for frontend..." -ForegroundColor Gray
    $ready = $false
    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 1
        $conn = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
        if ($conn) { $ready = $true; break }
    }
    if ($ready) {
        Write-Host "  Frontend ready." -ForegroundColor Green
    } else {
        Write-Host "  Frontend startup timeout! Check: $frontendLog" -ForegroundColor Red
        Get-Content $frontendLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
    }
}

# --- Summary ---
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Services started (background, no windows):" -ForegroundColor Green
Write-Host "    Backend API : http://localhost:1987" -ForegroundColor White
Write-Host "    API Docs    : http://localhost:1987/docs" -ForegroundColor Gray
Write-Host "    Frontend UI : http://localhost:5173" -ForegroundColor White
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Logs:    logs\backend.log" -ForegroundColor Gray
Write-Host "           logs\frontend.log" -ForegroundColor Gray
Write-Host "  Watch:   .\start.ps1 -Watch" -ForegroundColor Gray
Write-Host "  Stop:    .\stop.ps1" -ForegroundColor Yellow
Write-Host ""

# --- Optional: watch logs ---
if ($Watch) {
    Write-Host "Tailing logs (Ctrl+C to stop watching, services keep running)..." -ForegroundColor Cyan
    Write-Host ""
    # 轮询读取两个日志文件的新内容
    $backendLog = Join-Path $logDir 'backend.log'
    $frontendLog = Join-Path $logDir 'frontend.log'
    $bPos = 0; $fPos = 0
    try {
        while ($true) {
            if (Test-Path $backendLog) {
                $bContent = Get-Content $backendLog -ErrorAction SilentlyContinue
                if ($bContent -and $bContent.Count -gt $bPos) {
                    $bContent[$bPos..($bContent.Count - 1)] | ForEach-Object {
                        Write-Host "[BE] $_" -ForegroundColor Cyan
                    }
                    $bPos = $bContent.Count
                }
            }
            if (Test-Path $frontendLog) {
                $fContent = Get-Content $frontendLog -ErrorAction SilentlyContinue
                if ($fContent -and $fContent.Count -gt $fPos) {
                    $fContent[$fPos..($fContent.Count - 1)] | ForEach-Object {
                        Write-Host "[FE] $_" -ForegroundColor Green
                    }
                    $fPos = $fContent.Count
                }
            }
            Start-Sleep -Milliseconds 500
        }
    } catch {
        Write-Host "`nStopped watching. Services still running in background." -ForegroundColor Yellow
    }
}