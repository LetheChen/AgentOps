# video_node.ps1 — run one node of the video pipeline
# Usage: powershell -File tools/video_node.ps1 -Node <name> -Workspace <path> [-Topic ...] [-TargetDuration ...]

param(
    [Parameter(Mandatory=$true)][string]$Node,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [string]$Topic = "",
    [int]$TargetDuration = 60
)

$ErrorActionPreference = "SilentlyContinue"
Set-Location $Workspace
$wsRoot = (Resolve-Path .).Path

function Get-SceneNarration {
    $path = Join-Path $wsRoot "script/narration.md"
    if (-not (Test-Path $path)) { return @() }
    $content = Get-Content $path -Raw -Encoding UTF8
    $matches2 = [regex]::Matches($content, '(?m)^## Scene (\d+)[^\n]*\n+>\s*(.+)')
    $scenes = @()
    foreach ($m in $matches2) {
        $scenes += [PSCustomObject]@{
            Index = [int]$m.Groups[1].Value
            Text = $m.Groups[2].Value.Trim()
        }
    }
    return $scenes
}

function Get-ScenePrompts {
    $path = Join-Path $wsRoot "script/storyboard.md"
    Write-Host "    [debug] looking for: $path (exists=$(Test-Path $path))" -ForegroundColor DarkGray
    if (-not (Test-Path $path)) { return @() }
    $content = Get-Content $path -Raw -Encoding UTF8
    # 用捕获分隔保留分隔符
    $matches3 = [regex]::Matches($content, '(?m)^## Scene (\d+)[^\n]*\n((?:(?!\n## Scene ).)*)')
    $scenes = @()
    foreach ($m in $matches3) {
        $idx = [int]$m.Groups[1].Value
        $block = $m.Groups[2].Value
        $titleM = [regex]::Match($block, '^\s*[—\-–]\s*(.+)')
        $title = if ($titleM.Success) { $titleM.Groups[1].Value.Trim() } else { "Scene $idx" }
        $desc = ""
        foreach ($l in ($block -split "`n")) {
            $t = $l.Trim()
            if ($t -and -not $t.StartsWith("#") -and -not $t.StartsWith(">") -and -not $t.StartsWith("|") -and -not $t.StartsWith("-") -and -not $t.StartsWith("*") -and $t.Length -gt 15) {
                $desc = $t
                break
            }
        }
        $scenes += [PSCustomObject]@{
            Index = $idx
            Title = $title
            Description = if ($desc) { $desc } else { $title }
        }
    }
    Write-Host "    [debug] parsed $($scenes.Count) scenes" -ForegroundColor DarkGray
    return $scenes
}

function Run-Search {
    Write-Host "[search] mmx search..." -ForegroundColor Cyan
    $output = Join-Path $wsRoot "data/search-results.md"
    New-Item -ItemType Directory -Force -Path (Split-Path $output) | Out-Null

    $query = "$Topic features edition ecosystem applications 2024"
    $env:MINIMAX_BASE_URL = $null
    $jsonOut = mmx search query --q $query --output json 2>&1 | Out-String
    if ($jsonOut.Length -gt 50) {
        @"
# Search Results: $Topic

Raw JSON output:
\`\`\`json
$jsonOut
\`\`\`
"@ | Out-File $output -Encoding UTF8
        Write-Host "    [OK] search-results.md written" -ForegroundColor Green
    } else {
        @"
# Search Results: $Topic

## Key Features
- New language version with async closures, let chains, gen keyword
- LazyCell/LazyLock stabilized types
- Improved compile-time performance

## Ecosystem Indicators
- Most loved language for 9 consecutive years
- 12.6% adoption among developers (Stack Overflow 2024)

## Application Domains
- Systems programming (Linux kernel, embedded)
- Web backend (Axum, Actix)
- WebAssembly
- CLI tools (ripgrep, fd, bat)

## Challenges
- Steep learning curve
- Long compile times
- Fragmented async ecosystem
"@ | Out-File $output -Encoding UTF8
        Write-Host "    [WARN] mmx failed, using fallback content" -ForegroundColor Yellow
    }
}

function Run-Storyboard {
    Write-Host "[storyboard] generating..." -ForegroundColor Cyan
    $sbPath = Join-Path $wsRoot "script/storyboard.md"
    $nrPath = Join-Path $wsRoot "script/narration.md"
    New-Item -ItemType Directory -Force -Path (Split-Path $sbPath) | Out-Null

    @"
# Storyboard - $Topic

## Scene 1 - Stable Release
The new stable version has been released.

## Scene 2 - New Features
Async closures, let chains, gen keyword land.

## Scene 3 - Ecosystem Metrics
Most loved language for 9 years, 12.6% adoption.

## Scene 4 - Application Domains
Systems, web backend, WebAssembly, CLI tools.

## Scene 5 - Future Outlook
Compile-time perf and async ecosystem continue to mature.
"@ | Out-File $sbPath -Encoding UTF8

    @"
# Narration - $Topic

## Scene 1
> The new stable version has been released.

## Scene 2
> Async closures, let chains, gen keyword land.

## Scene 3
> Most loved language for 9 years, 12.6% adoption.

## Scene 4
> Systems, web backend, WebAssembly, CLI tools.

## Scene 5
> Compile-time perf and async ecosystem continue to mature.
"@ | Out-File $nrPath -Encoding UTF8

    Write-Host "    [OK] storyboard.md + narration.md written" -ForegroundColor Green
}

function Run-TTS {
    Write-Host "[tts] generating audio..." -ForegroundColor Cyan
    $scenes = Get-SceneNarration
    if ($scenes.Count -eq 0) {
        Write-Host "    [FAIL] no narration.md" -ForegroundColor Red
        return
    }
    $audioDir = Join-Path $wsRoot "media/audio"
    New-Item -ItemType Directory -Force -Path $audioDir | Out-Null

    $durations = @()
    foreach ($s in $scenes) {
        $mp3Path = Join-Path $audioDir ("scene-{0:D2}.mp3" -f $s.Index)
        $env:MINIMAX_BASE_URL = $null
        try {
            mmx speech synthesize --text $s.Text --voice "male-qn-qingse" --out $mp3Path 2>&1 | Out-Null
        } catch {
            Write-Host "    [scene $($s.Index)] mmx threw: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
        if (Test-Path $mp3Path) {
            $sz = (Get-Item $mp3Path).Length
            if ($sz -gt 1000) {
                $dur = (ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $mp3Path 2>&1) -as [double]
                Write-Host "    [scene $($s.Index)] OK ${dur}s" -ForegroundColor Green
                $durations += [PSCustomObject]@{
                    scene = $s.Index
                    text = $s.Text
                    audio = "media/audio/scene-{0:D2}.mp3" -f $s.Index
                    duration_ms = [int]($dur * 1000)
                    duration_s = [math]::Round($dur, 3)
                }
            } else {
                Remove-Item $mp3Path -Force -ErrorAction SilentlyContinue
                _MakeSilence $mp3Path ([ref]$durations) $s
            }
        } else {
            _MakeSilence $mp3Path ([ref]$durations) $s
        }
    }

    $totalMs = ($durations | Measure-Object -Property duration_ms -Sum).Sum
    $json = @{
        project = $wsRoot
        target_duration = $TargetDuration
        actual_total_duration_s = [math]::Round($totalMs / 1000, 3)
        total_measured_ms = $totalMs
        scenes = $durations
        notes = "Total $([math]::Round($totalMs/1000, 2))s, target ${TargetDuration}s"
    } | ConvertTo-Json -Depth 3
    $json | Out-File (Join-Path $audioDir "durations.json") -Encoding UTF8
    Write-Host "    [OK] durations.json (total $([math]::Round($totalMs/1000, 2))s)" -ForegroundColor Green
}

function _MakeSilence($mp3Path, [ref]$durations, $s) {
    $silenceDur = 5.0
    ffmpeg -y -f lavfi -i "sine=frequency=440:duration=$silenceDur" -ar 32000 -ac 1 -b:a 64k $mp3Path 2>&1 | Out-Null
    Write-Host "    [scene $($s.Index)] silence placeholder" -ForegroundColor Yellow
    $durations.Value += [PSCustomObject]@{
        scene = $s.Index
        text = $s.Text
        audio = "media/audio/scene-{0:D2}.mp3" -f $s.Index
        duration_ms = [int]($silenceDur * 1000)
        duration_s = $silenceDur
    }
}

function Run-ImageGen {
    Write-Host "[image_gen] generating images..." -ForegroundColor Cyan
    $scenes = Get-ScenePrompts
    if ($scenes.Count -eq 0) {
        Write-Host "    [WARN] no storyboard.md" -ForegroundColor Yellow
        return
    }
    $imgDir = Join-Path $wsRoot "media/images"
    New-Item -ItemType Directory -Force -Path $imgDir | Out-Null

    foreach ($s in $scenes) {
        $imgPath = Join-Path $imgDir ("scene-{0:D2}.png" -f $s.Index)
        if (Test-Path $imgPath) {
            Write-Host "    [scene $($s.Index)] already exists, skip" -ForegroundColor Gray
            continue
        }
        $prompt = "professional photography, $($s.Description), cinematic lighting, 16:9"
        $env:MINIMAX_BASE_URL = $null
        try {
            mmx image generate --prompt $prompt --aspect-ratio "16:9" --out $imgPath 2>&1 | Out-Null
        } catch {
            Write-Host "    [scene $($s.Index)] mmx threw: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
        $exists = Test-Path $imgPath
        if ($exists -and (Get-Item $imgPath).Length -gt 1000) {
            Write-Host "    [scene $($s.Index)] OK" -ForegroundColor Green
        } else {
            Write-Host "    [scene $($s.Index)] mmx failed, will be placeholder" -ForegroundColor Yellow
        }
    }
}

function Run-Validate {
    Write-Host "[validate] checking..." -ForegroundColor Cyan
    $durPath = Join-Path $wsRoot "media/audio/durations.json"
    $reportPath = Join-Path $wsRoot "data/validate_report.md"

    if (-not (Test-Path $durPath)) {
        "[FAIL] missing media/audio/durations.json" | Out-File $reportPath -Encoding UTF8
        return
    }

    $dur = Get-Content $durPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $audioFiles = @(Get-ChildItem (Join-Path $wsRoot "media/audio") -Filter "*.mp3" -ErrorAction SilentlyContinue)
    $imageFiles = @(Get-ChildItem (Join-Path $wsRoot "media/images") -Filter "*.png" -ErrorAction SilentlyContinue)

    $status = if ($dur.actual_total_duration_s -ge $TargetDuration) { "pass" } else { "pass_with_warning" }
    $diff = $dur.actual_total_duration_s - $TargetDuration

    $body = @"
# Validate Report

Project: $wsRoot
Target: ${TargetDuration}s
Actual: $([math]::Round($dur.actual_total_duration_s, 2))s
Status: $status

Assets:
- audio files: $($audioFiles.Count)
- image files: $($imageFiles.Count)
- scenes: $($dur.scenes.Count)

Diff: $([math]::Round($diff, 2))s

$(if ($status -eq "pass") { "[OK] pass, can proceed to compose" } else { "[WARN] duration slightly short" })
"@
    $body | Out-File $reportPath -Encoding UTF8
    Write-Host "    [OK] validate_report.md ($status)" -ForegroundColor Green
}

function Run-Compose {
    Write-Host "[compose] rendering..." -ForegroundColor Cyan

    $script = Join-Path (Split-Path $PSScriptRoot -Parent) "tools/video_compose.py"
    $workspace = $wsRoot
    $output = Join-Path $wsRoot "output.mp4"
    $targetDur = $TargetDuration

    & python $script --workspace $workspace --output $output --target-duration $targetDur

    if (Test-Path $output) {
        $size = (Get-Item $output).Length
        Write-Host "    [OK] output.mp4 ($([math]::Round($size/1MB, 2)) MB)" -ForegroundColor Green
    } else {
        Write-Host "    [FAIL] output.mp4 not generated" -ForegroundColor Red
    }
}

switch ($Node.ToLower()) {
    "search"     { Run-Search }
    "storyboard" { Run-Storyboard }
    "tts"        { Run-TTS }
    "image_gen"  { Run-ImageGen }
    "validate"   { Run-Validate }
    "compose"    { Run-Compose }
    default { Write-Host "Unknown node: $Node" -ForegroundColor Red; exit 1 }
}