<#
.SYNOPSIS
  One-time setup for Claude Video Studio on a Windows 10/11 PC. Safe to run again (skips what is already there).

.EXAMPLE
  Double-click Setup.bat            (same as below)
  powershell -ExecutionPolicy Bypass -File setup.ps1
  powershell -ExecutionPolicy Bypass -File setup.ps1 -CpuOnly -SkipMotion -RunTests
#>
param(
    [switch]$CpuOnly,     # don't install the NVIDIA (CUDA) whisper build even if an NVIDIA GPU exists
    [switch]$SkipMotion,  # skip Node.js + Remotion/HyperFrames (motion graphics) - the studio works without them
    [switch]$RunTests     # run the 23-check self-test at the end
)
# 'Continue' on purpose: in Windows PowerShell 5.1, 'Stop' turns ANY stderr line from a native tool (whisper, npm,
# pip progress) into a fatal error. Every step checks $LASTEXITCODE / file existence explicitly and throws instead.
$ErrorActionPreference = 'Continue'
$PSDefaultParameterValues['*:ErrorAction'] = 'Stop'   # cmdlets (Expand-Archive, Move-Item, ...) still fail loudly
$ProgressPreference = 'SilentlyContinue'
$Root = $PSScriptRoot
$Eng = Join-Path $Root '_engines'
$Studio = Join-Path $Eng 'studio'
$Whisper = Join-Path $Eng 'whisper'
$VenvPy = Join-Path $Studio '.venv\Scripts\python.exe'

# Pinned versions (reproducible on every PC)
$WhisperTag = 'b5454'
$WhisperBase = "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperTag"
$WhisperZips = @{
    cpu  = @{ name = 'whisper-bin-x64.zip';                 size = 8928640 }
    cuda = @{ name = 'whisper-bin-win-cuda-11.8.0-x64.zip'; size = 284761231 }
}
$CublasPkg = 'nvidia-cublas-cu11==11.11.3.6'
$Models = @(
    @{ file = 'ggml-large-v3-turbo-q5_0.bin'; size = 574041195
       url = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin'
       sha = '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2' },
    @{ file = 'ggml-silero-v5.1.2.bin'; size = 885098
       url = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin'
       sha = '29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf' }
)

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    ok  $m" -ForegroundColor Green }
function Note($m) { Write-Host "    ..  $m" }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

function Refresh-Path {
    $env:Path = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;" +
                [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue 2>$null) }

function Winget-Install($id, $cmd) {
    if (Have $cmd) { Ok "$cmd already installed"; return }
    if (-not (Have 'winget')) { throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run setup again." }
    Note "installing $id (winget)..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent | Out-Host
    Refresh-Path
    if (-not (Have $cmd)) { throw "$id was installed but '$cmd' is not on PATH yet. Close this window and run Setup.bat again." }
    Ok "$cmd installed"
}

function Get-File([string]$url, [string]$out, [long]$size = 0, [string]$sha = '') {
    $name = Split-Path $out -Leaf
    if (Test-Path $out) {
        $good = $true
        if ($size -gt 0 -and (Get-Item $out).Length -ne $size) { $good = $false }
        if ($good -and $sha) { $good = ((Get-FileHash $out -Algorithm SHA256).Hash.ToLower() -eq $sha) }
        if ($good) { Ok "$name already downloaded"; return }
        Remove-Item $out -Force
    }
    New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
    $part = "$out.part"
    Note ("downloading {0} ({1:N0} MB)..." -f $name, ($size / 1MB))
    for ($i = 1; $i -le 80; $i++) {
        $have = 0; if (Test-Path $part) { $have = (Get-Item $part).Length }
        if ($size -gt 0 -and $have -ge $size) { break }
        # -C - resumes; speed-limit aborts a stalled connection so the next try gets a fresh one
        & curl.exe -L -s -S -C - --connect-timeout 20 --speed-limit 50000 --speed-time 20 -o $part $url
        $code = $LASTEXITCODE
        $have = 0; if (Test-Path $part) { $have = (Get-Item $part).Length }
        if ($code -eq 0 -and ($size -le 0 -or $have -ge $size)) { break }
        Note ("{0:N0} / {1:N0} MB, connection dropped - resuming (try {2})" -f ($have / 1MB), ($size / 1MB), $i)
        Start-Sleep -Seconds 2
    }
    if ($size -gt 0 -and (Get-Item $part).Length -ne $size) { throw "$name did not download completely. Check the internet connection and run setup again (it resumes)." }
    if ($sha -and ((Get-FileHash $part -Algorithm SHA256).Hash.ToLower() -ne $sha)) {
        Remove-Item $part -Force
        throw "$name failed its checksum (corrupt download). Run setup again."
    }
    Move-Item $part $out -Force
    Ok "$name downloaded and verified"
}

function Find-Python {
    foreach ($c in @('py -3.12', 'py -3', 'python')) {
        $exe, $arg = $c.Split(' ', 2)
        if (-not (Have $exe)) { continue }
        try {
            $v = if ($arg) { & $exe $arg -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null } else { & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null }
            if ($v -and [version]$v -ge [version]'3.10') { return , @($exe, $arg) }
        } catch { }
    }
    return $null
}

Write-Host "Claude Video Studio setup  ($Root)" -ForegroundColor White
Refresh-Path

# fail early with a clear message instead of a cryptic error halfway through a 2.5 GB download
$needGB = 6; if ($SkipMotion) { $needGB = 4 }
$drive = (Get-Item $Root).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
if ($freeGB -lt $needGB) { throw "Only $freeGB GB free on drive $($drive.Name): - setup needs about $needGB GB. Free some space and run Setup.bat again." }
if ($Root -match '[&%!^]') { Warn "The folder path contains & % ! or ^ - batch files may misbehave. A simple path like D:\Claude_Video_Editor is safest." }

# ---------------------------------------------------------------- 1. system tools
Step '1/7  System tools (FFmpeg, yt-dlp)'
Winget-Install 'Gyan.FFmpeg' 'ffmpeg'
Winget-Install 'yt-dlp.yt-dlp' 'yt-dlp'
$hasNvenc = (& ffmpeg -hide_banner -encoders 2>$null | Select-String 'h264_nvenc') -ne $null
if ($hasNvenc) { Ok 'FFmpeg has NVIDIA encoder support' }

# ---------------------------------------------------------------- 2. python + studio app
Step '2/7  Python + studio app'
$py = Find-Python
if (-not $py) {
    Winget-Install 'Python.Python.3.12' 'py'
    $py = Find-Python
    if (-not $py) { throw 'Python 3.10+ not found after install. Close this window and run Setup.bat again.' }
}
if (-not (Test-Path $VenvPy)) {
    Note 'creating virtual environment...'
    if ($py[1]) { & $py[0] $py[1] -m venv (Join-Path $Studio '.venv') } else { & $py[0] -m venv (Join-Path $Studio '.venv') }
}
& $VenvPy -m pip install -q --disable-pip-version-check -r (Join-Path $Studio 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
Ok ("studio environment ready (" + (& $VenvPy -c "import sys;print('Python', sys.version.split()[0])") + ')')

# ---------------------------------------------------------------- 3. whisper.cpp
Step "3/7  Whisper speech-to-text (whisper.cpp $WhisperTag)"
$nvidia = $false
try { $nvidia = [bool](Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { $_.Name -match 'NVIDIA' }) } catch { }
$wantCuda = $nvidia -and -not $CpuOnly
if ($nvidia) { Ok 'NVIDIA GPU found' } else { Note 'no NVIDIA GPU - using the CPU build (slower, works fine)' }
$dl = Join-Path $Eng '_downloads'
$flavours = @('cpu'); if ($wantCuda) { $flavours += 'cuda' }
foreach ($f in $flavours) {
    $exe = Join-Path $Whisper "$f\Release\whisper-cli.exe"
    if (Test-Path $exe) { Ok "$f build already installed"; continue }
    $z = $WhisperZips[$f]
    $zip = Join-Path $dl $z.name
    Get-File "$WhisperBase/$($z.name)" $zip $z.size
    Expand-Archive -Force $zip (Join-Path $Whisper $f)
    if (-not (Test-Path $exe)) { throw "whisper $f build extracted but whisper-cli.exe is missing." }
    Remove-Item $zip -Force
    Ok "$f build installed"
}
if ($wantCuda) {
    $rel = Join-Path $Whisper 'cuda\Release'
    if (Test-Path (Join-Path $rel 'cublas64_11.dll')) { Ok 'cuBLAS (NVIDIA math library) present' }
    else {
        # the CUDA zip does not bundle cuBLAS; without it whisper silently falls back to the CPU
        Note 'fetching cuBLAS from NVIDIA''s official package (~400 MB)...'
        $tmp = Join-Path $dl 'cublas'
        & $VenvPy -m pip download $CublasPkg --no-deps --platform win_amd64 --only-binary=:all: --python-version 3.12 -d $tmp -q --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { throw 'cuBLAS download failed. Run setup again (or use -CpuOnly).' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $whl = Get-ChildItem $tmp -Filter *.whl | Select-Object -First 1
        $zipf = [IO.Compression.ZipFile]::OpenRead($whl.FullName)
        try {
            foreach ($e in $zipf.Entries) { if ($e.Name -like '*.dll') { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $rel $e.Name), $true) } }
        } finally { $zipf.Dispose() }
        Remove-Item $tmp -Recurse -Force
        Ok 'cuBLAS installed'
    }
}

# ---------------------------------------------------------------- 4. models
Step '4/7  Speech models'
foreach ($m in $Models) { Get-File $m.url (Join-Path $Whisper "models\$($m.file)") $m.size $m.sha }

# ---------------------------------------------------------------- 5. smoke test whisper
Step '5/7  Checking Whisper runs'
$tone = Join-Path $dl 'tone.wav'
New-Item -ItemType Directory -Force $dl | Out-Null
& ffmpeg -y -v error -f lavfi -i 'sine=frequency=440:duration=2' -ar 16000 -ac 1 $tone
$cli = Join-Path $Whisper 'cpu\Release\whisper-cli.exe'
if ($wantCuda) { $cli = Join-Path $Whisper 'cuda\Release\whisper-cli.exe' }
$out = & $cli -m (Join-Path $Whisper 'models\ggml-large-v3-turbo-q5_0.bin') -f $tone -l en 2>&1 | Out-String
$code = $LASTEXITCODE
Remove-Item $tone -Force -ErrorAction SilentlyContinue
if ($code -ne 0) { throw "whisper-cli failed:`n$out" }
if ($out -match 'CUDA0') { Ok 'Whisper runs on the NVIDIA GPU' }
elseif ($wantCuda) { Warn 'Whisper CUDA build ran on the CPU (GPU not usable) - analysis will be slower' }
else { Ok 'Whisper runs (CPU)' }

# ---------------------------------------------------------------- 6. motion graphics (optional)
Step '6/7  Motion graphics engines (Remotion, HyperFrames)'
if ($SkipMotion) { Note 'skipped (-SkipMotion)' }
else {
    Winget-Install 'OpenJS.NodeJS.LTS' 'npm'
    foreach ($p in @('remotion', 'hyperframes')) {
        $dir = Join-Path $Eng $p
        if (Test-Path (Join-Path $dir 'node_modules')) { Ok "$p already installed"; continue }
        Note "installing $p packages (npm ci)..."
        Push-Location $dir
        try { & npm ci --no-fund --no-audit --loglevel=error | Out-Host; if ($LASTEXITCODE -ne 0) { throw "npm ci failed in $p" } } finally { Pop-Location }
        Ok "$p installed"
    }
    # HyperFrames: no usage telemetry, no auto-update checks, never auto-install its skills into ~/.claude
    foreach ($v in 'HYPERFRAMES_SKIP_SKILLS', 'HYPERFRAMES_NO_TELEMETRY', 'HYPERFRAMES_NO_UPDATE_CHECK') {
        [Environment]::SetEnvironmentVariable($v, '1', 'User')
    }
    Ok 'HyperFrames privacy switches set'
}

# ---------------------------------------------------------------- 7. folders
Step '7/7  Working folders'
foreach ($d in 'Raw Footage', 'References', 'Storyboards', 'Exports', 'Brand Kit') {
    New-Item -ItemType Directory -Force (Join-Path $Root $d) | Out-Null
}
Ok 'Raw Footage, References, Storyboards, Exports, Brand Kit'

# ---------------------------------------------------------------- Library (stickers + sound effects, ~120 MB, free licences)
Step 'Library: 1,600 3D stickers (Microsoft Fluent Emoji, MIT) + 320 sound effects (Wyzowl SFX Kit, CC0)'
Push-Location $Studio
try { & $VenvPy library.py setup; if ($LASTEXITCODE -ne 0) { Write-Host '   Library not complete - the studio works without it; run Setup again later.' -ForegroundColor Yellow } else { Ok 'Library ready' } } finally { Pop-Location }

# ---------------------------------------------------------------- voice remover (a reference's background music)
Step 'Voice remover: Demucs (Meta, MIT) on the CPU, ~400 MB - for "Background audio" of a reference look'
$Sep = Join-Path $Root '_engines\separate'
$SepPy = Join-Path $Sep '.venv\Scripts\python.exe'
try {
    if (-not (Test-Path $SepPy)) {
        if ($py[1]) { & $py[0] $py[1] -m venv (Join-Path $Sep '.venv') } else { & $py[0] -m venv (Join-Path $Sep '.venv') }
    }
    & $SepPy -m pip install -q --disable-pip-version-check torch torchaudio --index-url https://download.pytorch.org/whl/cpu
    if ($LASTEXITCODE -ne 0) { throw 'torch' }
    & $SepPy -m pip install -q --disable-pip-version-check demucs numpy
    if ($LASTEXITCODE -ne 0) { throw 'demucs' }
    Ok 'voice remover ready (the model, ~80 MB, downloads on first use)'
} catch {
    Write-Host '   Voice remover not installed - everything else works; "Background audio" needs it. Run Setup again later.' -ForegroundColor Yellow
}

if ($RunTests) {
    Step 'Self-test'
    Push-Location $Studio
    try { & $VenvPy selftest.py; $code = $LASTEXITCODE } finally { Pop-Location }
    if ($code -ne 0) { throw 'Self-test failed - see the FAIL lines above.' }
}

Write-Host "`nSetup complete. Double-click 'Start Studio.bat' to open the studio." -ForegroundColor Green
