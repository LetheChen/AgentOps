# video_pipeline.ps1 — 完整 video-pipeline 一键运行（确定性，无需 LLM）
# 用法: pwsh tools/video_pipeline.ps1 -Topic "..." -TargetDuration 15
param(
    [Parameter(Mandatory=$true)][string]$Topic,
    [int]$TargetDuration = 60,
    [string]$RunId = ""
)

$ErrorActionPreference = "SilentlyContinue"
$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

if (-not $RunId) {
    $RunId = "run_" + (Get-Date -Format "yyyyMMdd_HHmmss_ffffff")
}
$wsRoot = Join-Path $root "workspace/video-pipeline/$RunId"
New-Item -ItemType Directory -Force -Path $wsRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $wsRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $wsRoot "script") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $wsRoot "media/audio") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $wsRoot "media/images") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $wsRoot "code") | Out-Null

Write-Host "========================================" -ForegroundColor Magenta
Write-Host "Video Pipeline: $Topic" -ForegroundColor Magenta
Write-Host "Run ID: $RunId" -ForegroundColor Magenta
Write-Host "Workspace: $wsRoot" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta

$nodes = @("search", "storyboard", "tts", "image_gen", "validate", "compose")
foreach ($n in $nodes) {
    Write-Host "`n[$($nodes.IndexOf($n)+1)/$($nodes.Count)] === $n ===" -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File "$PSScriptRoot/video_node.ps1" -Node $n -Workspace $wsRoot -Topic $Topic -TargetDuration $TargetDuration
    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ Node $n failed" -ForegroundColor Red
    }
}

# 总结
Write-Host "`n========================================" -ForegroundColor Magenta
Write-Host "Pipeline Done" -ForegroundColor Magenta
$outputMp4 = Join-Path $wsRoot "output.mp4"
if (Test-Path $outputMp4) {
    $size = [math]::Round((Get-Item $outputMp4).Length / 1e6, 2)
    Write-Host "[OK] output.mp4: $outputMp4 ($size MB)" -ForegroundColor Green
} else {
    Write-Host "[FAIL] output.mp4 not generated" -ForegroundColor Red
}
Write-Host "========================================" -ForegroundColor Magenta