# Run any kit script with the kit's own venv (Windows). Sets it up first if needed.
#   .\run.ps1                          -> web app with the arms from .env
#   .\run.ps1 web_app.py --sim         -> web app with simulated arms
#   .\run.ps1 first_move.py --list-ports
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) { & powershell -ExecutionPolicy Bypass -File .\setup.ps1 }
$cmd = if ($args.Count -eq 0) { @("web_app.py") } else { $args }
& .\.venv\Scripts\python.exe @cmd
