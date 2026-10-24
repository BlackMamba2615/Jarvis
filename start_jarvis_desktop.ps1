param([switch]$DryRun)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$jarvisDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonwPath = Join-Path $jarvisDirectory ".voice-venv\Scripts\pythonw.exe"
$logDirectory = Join-Path $jarvisDirectory "logs"
$startupLog = Join-Path $logDirectory "desktop_startup.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-DesktopStartupLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $startupLog -Value "[$timestamp] $Message" -Encoding UTF8
}

if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    Write-DesktopStartupLog "Desktop Python executable was not found."
    exit 3
}

try {
    $arguments = @("desktop_controller.py")
    if ($DryRun) {
        $arguments += "--dry-run"
    }
    $process = Start-Process `
        -FilePath $pythonwPath `
        -ArgumentList $arguments `
        -WorkingDirectory $jarvisDirectory `
        -WindowStyle Hidden `
        -PassThru
    Write-DesktopStartupLog "Desktop voice controller launch requested with PID $($process.Id); dry run: $DryRun."
    exit 0
}
catch {
    Write-DesktopStartupLog "Desktop voice controller failed to launch: $($_.Exception.Message)"
    exit 6
}
