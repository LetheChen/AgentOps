# ============================================================
# AgentOps — 本地源码一键安装脚本 (Windows PowerShell)
# ============================================================
#
# 用途：不依赖 Docker，直接在主机上跑后端 + 前端。
#
# 用法（在项目根目录）：
#   .\install-local.ps1
#
# 依赖：Python 3.11+ / Node 20+ / npm
# ============================================================

$ErrorActionPreference = 'Stop'

function Log  { param($m) Write-Host "[AgentOps] $m" -ForegroundColor Cyan }
function Ok   { param($m) Write-Host "[OK] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

# ── 0. 前置检查 ──────────────────────────────────
try { $pyVer = python --version } catch { Die "python 未安装（需 3.11+）" }
try { $nodeVer = node --version } catch { Die "node 未安装（需 20+）" }
try { $null = npm --version } catch { Die "npm 未安装" }
Log "$pyVer / $nodeVer"

# ── 1. 项目根目录 ────────────────────────────────
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# ── 2. Python 虚拟环境 ───────────────────────────
if (-not (Test-Path .venv)) {
    Log "创建 Python 虚拟环境 .venv"
    python -m venv .venv
}
& .venv\Scripts\Activate.ps1
Ok "激活虚拟环境"

Log "升级 pip + 安装 Python 依赖"
python -m pip install --upgrade pip --quiet
python -m pip install -e . --quiet
Ok "Python 依赖已安装"

# ── 3. .env ──────────────────────────────────────
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Warn "已创建 .env（默认配置）；如需企业微信推送 / 大模型 API Key 请编辑 .env"
} else {
    Ok ".env 已存在"
}

# ── 4. 前端依赖 ──────────────────────────────────
if (-not (Test-Path web/node_modules)) {
    Log "安装前端依赖（首次约 2-5 分钟）"
    Push-Location web
    npm install --no-audit --no-fund
    Pop-Location
    Ok "前端依赖已安装"
} else {
    Ok "前端依赖已存在"
}

# ── 5. 数据库初始化 ──────────────────────────────
Log "初始化 audit.db（如未存在）"
python -c "from audit import SqliteEventStore; s = SqliteEventStore('audit.db'); print(f'[init] audit.db ready: {s.db_path}')"
Ok "数据库就绪"

# ── 6. 启动服务 ──────────────────────────────────
Log "启动后端（端口 1987）+ 前端（端口 5173）"

if (-not (Test-Path logs)) { New-Item -ItemType Directory -Path logs | Out-Null }

# 后端：Start-Process 后台启动
$backendArgs = @('-u', '-m', 'uvicorn', 'api.server:app', '--host', '0.0.0.0', '--port', '1987')
$backendProc = Start-Process -FilePath python -ArgumentList $backendArgs `
    -WorkingDirectory $scriptDir `
    -RedirectStandardOutput (Join-Path $scriptDir 'logs\backend.log') `
    -RedirectStandardError  (Join-Path $scriptDir 'logs\backend.log') `
    -WindowStyle Hidden -PassThru
$backendProc.Id | Out-File .backend.pid -Encoding ascii
Log "后端 PID: $($backendProc.Id)"

# 前端
Push-Location web
$frontendProc = Start-Process -FilePath npm -ArgumentList @('run', 'dev') `
    -WorkingDirectory (Get-Location) `
    -RedirectStandardOutput (Join-Path $scriptDir 'logs\frontend.log') `
    -RedirectStandardError  (Join-Path $scriptDir 'logs\frontend.log') `
    -WindowStyle Hidden -PassThru
$frontendProc.Id | Out-File ..\.frontend.pid -Encoding ascii
Pop-Location
Log "前端 PID: $($frontendProc.Id)"

# ── 7. 等待就绪 ─────────────────────────────────
Log "等待后端就绪（最多 60 秒）"
$ready = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:1987/" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if ($ready) { Ok "后端已就绪" } else { Warn "后端健康检查超时，请查看 logs\backend.log" }

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "       AgentOps 本地启动成功" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "访问入口：" -ForegroundColor White
Write-Host "  - 前端 UI : http://localhost:5173"
Write-Host "  - API     : http://localhost:1987"
Write-Host "  - Swagger : http://localhost:1987/docs"
Write-Host ""
Write-Host "停止服务：Stop-Process -Id (Get-Content .backend.pid),(Get-Content .frontend.pid)"
Write-Host "日志：Get-Content logs\backend.log -Wait"
Write-Host ""
