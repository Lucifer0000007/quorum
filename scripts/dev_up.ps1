<#
Start Quorum for a local demo on a Windows machine where Docker runs inside WSL2 (see README, Setup).
Run it in your own terminal and leave the window open. Ctrl+C stops the API server.

    powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 [-Distro Debian] [-Port 8000] [-NoServer]

What it does: keeps WSL alive (WSL stops idle distros, which stops Docker and FalkorDB with them), starts FalkorDB,
ingests the corpus if the graph is missing or not ready, then serves the app on http://127.0.0.1:<Port>.
#>
param(
    [string]$Distro = "Debian",
    [int]$Port = 8000,
    [switch]$NoServer
)
$ErrorActionPreference = "Continue"   # native commands write progress to stderr; we check exit codes instead
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "No .venv found. Create it: uv venv .venv --python 3.12; uv pip install -r requirements.txt --python .venv\Scripts\python.exe"
}

# 1. Keep the WSL VM alive with one hidden session (only if there isn't one already).
$keepalive = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" | Where-Object { $_.CommandLine -match "sleep infinity" }
if ($keepalive) {
    Write-Host "WSL keepalive already running."
} else {
    Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "-u", "root", "--", "sleep", "infinity") -WindowStyle Hidden
    Write-Host "Started a hidden WSL keepalive (stop it with: wsl --terminate $Distro)."
}

# 2. FalkorDB
$wslRoot = (wsl.exe -d $Distro -u root -- wslpath -a ($root -replace '\\', '/')).Trim()
wsl.exe -d $Distro -u root -- sh -c "cd '$wslRoot' && docker compose up -d falkordb"
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (is Docker installed in $Distro?)" }
& $py scripts\wait_falkordb.py
if ($LASTEXITCODE -ne 0) { throw "FalkorDB did not answer PING on localhost:6379" }

# 3. Graph: ingest only when it is missing or not ready (ingest is idempotent, so re-running it is always safe).
& $py -m scripts.check_graph *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Graph missing or not ready; ingesting..."
    if (-not (Test-Path "data\enhancements\keps")) {
        git clone --depth 1 -c core.longpaths=true https://github.com/kubernetes/enhancements data/enhancements
        if ($LASTEXITCODE -ne 0) { throw "could not clone kubernetes/enhancements" }
    }
    & $py -m ingest.load
    if ($LASTEXITCODE -ne 0) { throw "ingest failed" }
}
& $py -m scripts.check_graph | Select-Object -Last 1

if ($NoServer) { return }
Write-Host "Quorum: http://127.0.0.1:$Port   (Ctrl+C to stop)"
& $py -m uvicorn agent.api:app --host 127.0.0.1 --port $Port
