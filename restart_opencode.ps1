# AgentOps - Restart opencode server with optional session cleanup
# Usage:
#   .\restart_opencode.ps1              # 重启 opencode（不清理 session）
#   .\restart_opencode.ps1 -CleanSessions  # 重启前清理所有历史 session
param(
    [switch]$CleanSessions
)

# Kill any running opencode
foreach ($p in Get-WmiObject Win32_Process -Filter "Name = 'opencode.exe'") {
  try { $p.Terminate() | Out-Null } catch {}
}
foreach ($p in Get-WmiObject Win32_Process -Filter "Name = 'cmd.exe'") {
  $cmd = (Get-WmiObject Win32_Process -Filter "ProcessId = $($p.ProcessId)" -ErrorAction SilentlyContinue).CommandLine
  if ($cmd -and $cmd -like '*opencode serve*') {
    try { $p.Terminate() | Out-Null } catch {}
  }
}
Start-Sleep 2

# 可选：清理 opencode session 残留
if ($CleanSessions) {
    $dbPath = Join-Path $env:USERPROFILE ".local\share\opencode\opencode.db"
    if (Test-Path $dbPath) {
        $before = python -c "import sqlite3; c=sqlite3.connect(r'$dbPath'); print(c.execute('SELECT COUNT(*) FROM session').fetchone()[0]); c.close()"
        Write-Host "Cleaning opencode sessions: $before sessions found"
        python -c "import sqlite3; c=sqlite3.connect(r'$dbPath'); c.execute('DELETE FROM session'); c.execute('DELETE FROM session_message'); c.execute('DELETE FROM session_input'); c.commit(); c.close()"
        Write-Host "Sessions cleaned."
    } else {
        Write-Host "opencode.db not found at $dbPath, skip cleanup."
    }
}

# Start opencode fresh
$opencodeDir = "D:\Program Files\nodejs\node_global"
$logPath = "E:\Project\AgentOps\logs\opencode.log"
Start-Process -FilePath "$opencodeDir\opencode.cmd" -ArgumentList "serve" -WorkingDirectory $opencodeDir -WindowStyle Hidden -RedirectStandardOutput $logPath -RedirectStandardError "$logPath.err"
Start-Sleep 6

# Check status
$port = (Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Host "opencode port 4096: $port"

if ($port -eq 1) {
  Write-Host ""
  Write-Host "=== /api/provider ==="
  $resp = Invoke-WebRequest -Uri "http://127.0.0.1:4096/api/provider" -UseBasicParsing -TimeoutSec 5
  $data = $resp.Content | ConvertFrom-Json
  foreach ($p in $data.data) {
    Write-Host "  Provider: $($p.id) ($($p.name))"
  }

  Write-Host ""
  Write-Host "=== /config ==="
  $cfg = (Invoke-WebRequest -Uri "http://127.0.0.1:4096/config" -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json
  foreach ($pn in $cfg.provider.PSObject.Properties) {
    $pm = $pn.Value
    Write-Host "  Provider '$($pn.Name)': npm=$($pm.npm)"
    foreach ($mn in $pm.models.PSObject.Properties) {
      Write-Host "    - $($mn.Name)"
    }
  }
}
