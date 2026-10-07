<#
plc_service.ps1 — start/stop/check SEVERAL simulated PLCs as background
processes on Windows. Each PLC has its own IP address (127.0.0.x is local
loopback on Windows, no network adapter needed) and runs plc_simulator.py.

v3: multi-PLC. Edit the $Plcs list below to add/remove PLCs.

SETUP (one time):
  1. Keep this file in the SAME folder as plc_simulator.py.
  2. pip install pyModbusTCP
  3. If scripts are blocked:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
     or per-run:              powershell -ExecutionPolicy Bypass -File .\plc_service.ps1 start

USE:
  .\plc_service.ps1 start      # start all PLCs
  .\plc_service.ps1 status
  .\plc_service.ps1 stop
  .\plc_service.ps1 restart

Logs:  .\logs\<PLC name>.log  and  .err
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action
)

# ---------------------------------------------------------------------
# Simulated PLCs — must match plc_metadata (ip_address, port)
# ---------------------------------------------------------------------
$Plcs = @(
    @{ Name = "PLC-1"; IP = "127.0.0.1"; Port = 502 },
    @{ Name = "PLC-2"; IP = "127.0.0.2"; Port = 502 },
    @{ Name = "PLC-3"; IP = "127.0.0.3"; Port = 502 }
)
$UpdateInterval = "0.1"   # seconds between simulated value changes

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SimScript = Join-Path $ScriptDir "plc_simulator.py"
$LogDir    = Join-Path $ScriptDir "logs"

function Resolve-PythonExe {
    foreach ($name in @("python", "python3", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Get-Paths($plc) {
    return @{
        Pid = Join-Path $LogDir "$($plc.Name).pid"
        Log = Join-Path $LogDir "$($plc.Name).log"
        Err = Join-Path $LogDir "$($plc.Name).err"
    }
}

function Get-RunningProcess($plc) {
    $p = Get-Paths $plc
    if (Test-Path $p.Pid) {
        $storedId = Get-Content $p.Pid -ErrorAction SilentlyContinue
        if ($storedId) {
            $proc = Get-Process -Id $storedId -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -match "python") { return $proc }
        }
    }
    return $null
}

function Start-Plc($plc, $pythonExe) {
    $label = "$($plc.Name) ($($plc.IP):$($plc.Port))"
    $existing = Get-RunningProcess $plc
    if ($existing) {
        Write-Host "$label already running (PID $($existing.Id))."
        return
    }
    $p = Get-Paths $plc
    Remove-Item $p.Log, $p.Err -ErrorAction SilentlyContinue

    try {
        $proc = Start-Process -FilePath $pythonExe `
            -ArgumentList @("`"$SimScript`"", "--host", $plc.IP, "--port", $plc.Port,
                            "--interval", $UpdateInterval, "--quiet") `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $p.Log `
            -RedirectStandardError $p.Err `
            -ErrorAction Stop
    } catch {
        Write-Host "ERROR launching ${label}: $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    $proc.Id | Out-File -FilePath $p.Pid -Encoding ascii
    Start-Sleep -Milliseconds 1500

    if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
        Write-Host "$label started (PID $($proc.Id))." -ForegroundColor Green
    } else {
        Write-Host "$label exited immediately." -ForegroundColor Red
        if (Test-Path $p.Err) { Get-Content $p.Err | Write-Host -ForegroundColor Yellow }
        Remove-Item $p.Pid -ErrorAction SilentlyContinue
    }
}

function Stop-Plc($plc) {
    $p = Get-Paths $plc
    $proc = Get-RunningProcess $plc
    if ($proc) {
        Stop-Process -Id $proc.Id -Force
        Write-Host "$($plc.Name) stopped (PID $($proc.Id))."
    } else {
        Write-Host "$($plc.Name) not running."
    }
    Remove-Item $p.Pid -ErrorAction SilentlyContinue
}

function Status-Plc($plc) {
    $proc = Get-RunningProcess $plc
    $label = "$($plc.Name) ($($plc.IP):$($plc.Port))"
    if ($proc) {
        Write-Host "$label running (PID $($proc.Id)), since $($proc.StartTime)" -ForegroundColor Green
    } else {
        Write-Host "$label not running." -ForegroundColor Yellow
    }
}

function Start-All {
    if (-not (Test-Path $SimScript)) {
        Write-Host "ERROR: plc_simulator.py not found at $SimScript" -ForegroundColor Red
        return
    }
    $pythonExe = Resolve-PythonExe
    if (-not $pythonExe) {
        Write-Host "ERROR: python / python3 / py not found on PATH." -ForegroundColor Red
        return
    }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Write-Host "Using interpreter: $pythonExe"
    foreach ($plc in $Plcs) { Start-Plc $plc $pythonExe }
}

try {
    switch ($Action) {
        "start"   { Start-All }
        "stop"    { foreach ($plc in $Plcs) { Stop-Plc $plc } }
        "status"  { foreach ($plc in $Plcs) { Status-Plc $plc } }
        "restart" { foreach ($plc in $Plcs) { Stop-Plc $plc }; Start-Sleep -Seconds 1; Start-All }
    }
} catch {
    Write-Host "UNEXPECTED ERROR:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor Red
}
