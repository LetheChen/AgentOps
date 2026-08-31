# AgentOps - Stop all services
# Usage: .\stop.ps1

$ErrorActionPreference = 'SilentlyContinue'

Write-Host "Stopping AgentOps services..." -ForegroundColor Cyan

# Helper: kill a process and all its children (process tree)
# NOTE: 参数名不能用 $pid (PowerShell 内置自动变量), 用 $procPid 避免冲突
function Stop-ProcessTree {
    param([int]$procPid)
    # taskkill /T kills the process tree (parent + all children)
    $null = & taskkill /F /T /PID $procPid 2>&1
}

# --- Backend (:1987) ---
$backendConns = Get-NetTCPConnection -LocalPort 1987 -State Listen -ErrorAction SilentlyContinue
if ($backendConns) {
    $pids = @()
    foreach ($conn in $backendConns) {
        $procId = $conn.OwningProcess
        if ($procId -and $procId -notin $pids) {
            $pids += $procId
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($proc) { $proc.ProcessName } else { "unknown" }
            Write-Host "  Backend  (port 1987) - killing PID $procId ($name) and child processes..." -ForegroundColor Yellow
            Stop-ProcessTree -procPid $procId
            Write-Host "  Backend  (port 1987) - terminated." -ForegroundColor Green
        }
    }
} else {
    Write-Host "  Backend  (port 1987) - not running." -ForegroundColor Gray
}

# --- Opencode server (:4096) ---
$opencodeConns = Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue
if ($opencodeConns) {
    $pids = @()
    foreach ($conn in $opencodeConns) {
        $procId = $conn.OwningProcess
        if ($procId -and $procId -notin $pids) {
            $pids += $procId
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($proc) { $proc.ProcessName } else { "unknown" }
            Write-Host "  Opencode (port 4096)  - killing PID $procId ($name) and child processes..." -ForegroundColor Yellow
            Stop-ProcessTree -procPid $procId
            Write-Host "  Opencode (port 4096)  - terminated." -ForegroundColor Green
        }
    }
} else {
    Write-Host "  Opencode (port 4096)  - not running." -ForegroundColor Gray
}

# --- Frontend (:5173) ---
$frontendConns = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($frontendConns) {
    $pids = @()
    foreach ($conn in $frontendConns) {
        $procId = $conn.OwningProcess
        if ($procId -and $procId -notin $pids) {
            $pids += $procId
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($proc) { $proc.ProcessName } else { "unknown" }
            Write-Host "  Frontend (port 5173)  - killing PID $procId ($name) and child processes..." -ForegroundColor Yellow
            Stop-ProcessTree -procPid $procId
            Write-Host "  Frontend (port 5173)  - terminated." -ForegroundColor Green
        }
    }
} else {
    Write-Host "  Frontend (port 5173)  - not running." -ForegroundColor Gray
}

# --- Also kill orphaned uvicorn workers (command line match) ---
Start-Sleep -Milliseconds 500
$orphans = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'uvicorn.*api.server' }
foreach ($o in $orphans) {
    Stop-ProcessTree -procPid $o.ProcessId
    Write-Host "  Orphaned uvicorn worker (PID $($o.ProcessId)) terminated." -ForegroundColor Yellow
}

# --- Verify ---
Start-Sleep -Seconds 1
$b = Get-NetTCPConnection -LocalPort 1987 -State Listen -ErrorAction SilentlyContinue
$f = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
Write-Host ""
if ($b -or $f) {
    Write-Host "  Warning: some ports still in use:" -ForegroundColor Yellow
    if ($b) { Write-Host "    1987 still listening" -ForegroundColor Yellow }
    if ($f) { Write-Host "    5173  still listening" -ForegroundColor Yellow }
} else {
    Write-Host "  All services stopped." -ForegroundColor Green
}
Write-Host ""