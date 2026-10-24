$ErrorActionPreference = "Stop"

$jarvisDirectory = $PSScriptRoot
$skillsPath = Join-Path $jarvisDirectory "skills.json"
$cacheDirectory = Join-Path $jarvisDirectory "speech_cache"

if (-not (Test-Path -LiteralPath $skillsPath -PathType Leaf)) {
    throw "skills.json not found: $skillsPath"
}

$config = Get-Content -LiteralPath $skillsPath -Raw -Encoding UTF8 |
    ConvertFrom-Json

$actionMessages = @(
    $config.actions.PSObject.Properties |
        ForEach-Object { [string]$_.Value.message } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)

$messages = [System.Collections.Generic.List[string]]::new()

# These Base64 values are UTF-8 text for the fixed built-in replies. Keeping
# the script itself ASCII makes it reliable in Windows PowerShell 5.1.
@(
    "6K+t6Z+z5Zue5aSN5bey5byA5ZCv"
    "5Lu75Yqh5a6M5oiQ"
    "5oiR5ZCs5oeC5LqG77yM5L2G6L+Y5LiN5Lya5omn6KGM6L+Z5Liq5oyH5Luk"
    "5rKh5pyJ5ZCs5riF77yM6K+35YaN6K+05LiA5qyh"
) | ForEach-Object {
    $decoded = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($_)
    )
    $messages.Add($decoded)
}

@($config.settings.speech_messages) |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
    ForEach-Object { $messages.Add([string]$_) }

$actionMessages | ForEach-Object { $messages.Add($_) }

# Pre-generate every possible successful multi-action reply. There are only
# six built-in allowlisted actions, so this remains a small, deterministic set.
$combinationCount = [Math]::Pow(2, $actionMessages.Count)

for ($mask = 1; $mask -lt $combinationCount; $mask++) {
    $selected = [System.Collections.Generic.List[string]]::new()

    for ($index = 0; $index -lt $actionMessages.Count; $index++) {
        if (($mask -band (1 -shl $index)) -ne 0) {
            $selected.Add($actionMessages[$index])
        }
    }

    if ($selected.Count -gt 1) {
        $messages.Add(($selected -join ([char]0xFF0C)))
    }
}

if ($config.scenes) {
    $config.scenes.PSObject.Properties |
        ForEach-Object { [string]$_.Value.message } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $messages.Add($_) }
}

$uniqueMessages = @($messages | Sort-Object -Unique)

New-Item -ItemType Directory -Path $cacheDirectory -Force | Out-Null

Add-Type -AssemblyName System.Speech

$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$generated = 0

try {
    $speaker.SelectVoice("Microsoft Huihui Desktop")

    foreach ($message in $uniqueMessages) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($message)
        $hashBytes = $sha256.ComputeHash($bytes)
        $hash = ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
        $outputPath = Join-Path $cacheDirectory "$hash.wav"

        $speaker.SetOutputToWaveFile($outputPath)
        $speaker.Speak($message)
        $speaker.SetOutputToDefaultAudioDevice()
        $generated++
    }
}
finally {
    $speaker.Dispose()
    $sha256.Dispose()
}

Write-Host "Offline speech cache generated successfully." -ForegroundColor Green
Write-Host "Voice count: $generated"
Write-Host "Cache directory: $cacheDirectory"
