# ============================================================
# AgentOps — Docker 一键安装脚本 (Windows PowerShell)
# ============================================================
#
# 用途：在已安装 Docker Desktop 的 Windows 机器上一键拉起 AgentOps。
#
# 用法（在项目根目录）：
#   .\install.ps1
#
# 环境变量（可选）：
#   $env:AGENTOPS_VERSION       镜像 tag（默认 latest）
#   $env:AGENTOPS_WEB_PORT      Web 端口（默认 80；单容器仅暴露 80）
#   $env:AGENTOPS_DATA_DIR      数据卷 host 路径（默认 .\data）
#   $env:SKIP_BUILD             设为 1 跳过 docker build
# ============================================================

$ErrorActionPreference = 'Stop'

# ── 颜色输出 ──────────────────────────────────────────
function Log  { param($m) Write-Host "[AgentOps] $m" -ForegroundColor Cyan }
function Ok   { param($m) Write-Host "[OK] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

# ── 0. 前置检查 ──────────────────────────────────────
try { $null = docker --version } catch { Die "docker 未安装。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/" }
Ok "检测到 docker"

# 检测 docker compose
$composeCmd = $null
try {
    $null = docker compose version
    $composeCmd = "docker compose"
} catch {
    try {
        $null = docker-compose --version
        $composeCmd = "docker-compose"
    } catch {
        Die "未检测到 docker compose。请升级 Docker Desktop 或单独安装 docker-compose。"
    }
}
Ok "检测到 $composeCmd"

# ── 1. 项目根目录 ────────────────────────────────────
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
Log "项目目录: $scriptDir"

# ── 2. .env ──────────────────────────────────────────
if (-not (Test-Path .env)) {
    Log "未检测到 .env，从 .env.example 复制"
    Copy-Item .env.example .env
    Warn "已创建 .env，请根据需要填入 API Key 等敏感配置（生产前必填）"
    if ([Environment]::UserInteractive) {
        Warn "按任意键继续（默认配置即可启动），或 Ctrl+C 退出先编辑 .env"
        $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    }
} else {
    Ok ".env 已存在"
}

# ── 3. 数据目录 ──────────────────────────────────────
$dataDir = if ($env:AGENTOPS_DATA_DIR) { $env:AGENTOPS_DATA_DIR } else { Join-Path $scriptDir 'data' }
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Ok "创建数据目录: $dataDir"
}

# ── 4. 构建镜像 ──────────────────────────────────────
if ($env:SKIP_BUILD -eq '1') {
    Log "跳过构建（SKIP_BUILD=1），将直接拉取镜像"
    & docker compose pull agentops 2>&1 | Out-Null
} else {
    Log "构建单镜像 agentops:latest（首次约 5-10 分钟）"
    & docker build -t agentops:latest -f docker/agentops/Dockerfile .
    if ($LASTEXITCODE -ne 0) { Die "镜像构建失败" }
    Ok "镜像构建完成"
}

# ── 5. 启动主服务（首次启动会自动建表 + admin 账号）─────
Log "启动 AgentOps 单容器全栈（后台运行）"
& docker compose up -d agentops
if ($LASTEXITCODE -ne 0) { Die "服务启动失败" }

# ── 6. 等待就绪 ─────────────────────────────────────
Log "等待健康检查通过（最多 60 秒）"
$ready = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:${webPort}/" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if ($ready) {
    Ok "AgentOps 全栈已就绪"
} else {
    Warn "健康检查超时，可执行 'docker compose logs agentops' 查看日志"
}

# ── 7. 输出 ──────────────────────────────────────────
$webPort = if ($env:AGENTOPS_WEB_PORT) { $env:AGENTOPS_WEB_PORT } else { "80" }

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "       AgentOps 启动成功" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "访问入口（单端口 80，前后端全栈合一）：" -ForegroundColor White
Write-Host "  - 前端 UI   : http://localhost:${webPort}"
Write-Host "  - API       : http://localhost:${webPort}/api/..."
Write-Host "  - Swagger   : http://localhost:${webPort}/docs"
Write-Host ""
Write-Host "默认账号（仅 .env 含 AGENTOPS_BOOTSTRAP_PASSWORD 时启用）：" -ForegroundColor White
Write-Host "  - 用户名    : admin"
Write-Host "  - 密码      : 见 .env 或 data\bootstrap-password.txt"
Write-Host ""
Write-Host "常用命令：" -ForegroundColor White
Write-Host "  - 查看日志  : docker compose logs -f agentops"
Write-Host "  - 停止      : docker compose down"
Write-Host "  - 重启      : docker compose restart"
Write-Host "  - 升级镜像  : docker compose pull ; docker compose up -d"
Write-Host ""
