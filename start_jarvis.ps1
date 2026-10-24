$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$jarvisDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $jarvisDirectory ".voice-venv\Scripts\python.exe"
$logDirectory = Join-Path $jarvisDirectory "logs"
$standardLog = Join-Path $logDirectory "jarvis.stdout.log"
$errorLog = Join-Path $logDirectory "jarvis.stderr.log"
$startupLog = Join-Path $logDirectory "startup.log"
$statusUrl = "http://127.0.0.1:8000/status"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-StartupLog {
    param([string]$Message)

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $startupLog -Value "[$timestamp] $Message" -Encoding UTF8
}

function Test-JarvisOnline {
    try {
        $statusResult = Invoke-RestMethod -Uri $statusUrl -Method Get -TimeoutSec 3
        return $statusResult.jarvis -eq "online"
    }
    catch {
        return $false
    }
}

if (Test-JarvisOnline) {
    Write-StartupLog "Jarvis is already online; duplicate start skipped."
    exit 0
}

$existingListener = Get-NetTCPConnection `
    -LocalPort 8000 `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($existingListener) {
    Write-StartupLog "Port 8000 is occupied by another process; Jarvis was not started."
    exit 2
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-StartupLog "Python executable was not found: $pythonPath"
    exit 3
}

try {
    $arguments = @(
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000"
    )

    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList $arguments `
        -WorkingDirectory $jarvisDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $standardLog `
        -RedirectStandardError $errorLog `
        -PassThru

    Write-StartupLog "Jarvis process started with PID $($process.Id)."

    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        if (Test-JarvisOnline) {
            Write-StartupLog "Jarvis startup check passed."
            exit 0
        }
    } while ((Get-Date) -lt $deadline -and -not $process.HasExited)

    if ($process.HasExited) {
        Write-StartupLog "Jarvis exited during startup with code $($process.ExitCode)."
        exit 4
    }

    Write-StartupLog "Jarvis did not become ready within 60 seconds."
    exit 5
}
catch {
    Write-StartupLog "Jarvis startup failed: $($_.Exception.Message)"
    exit 6
}

