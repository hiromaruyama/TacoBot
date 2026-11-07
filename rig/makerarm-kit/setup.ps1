# One-time setup on Windows: Python 3.10+ virtual env + dependencies, and a .env to fill in.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { py -3 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env - set ARM_PORTS (and the LiveKit key/secret if you use LiveKit)."
}

& .\.venv\Scripts\python.exe -c "import maker_arm, livekit.rtc, livekit.api, aiohttp; print('setup OK')"
