param(
  [switch]$Autostart
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

if (!(Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example. Please edit it before running in production."
}

if (!(Test-Path "venv\\Scripts\\Activate.ps1")) {
  python -m venv venv
}

& "venv\\Scripts\\Activate.ps1"
python -m pip install -r "requirements.txt"

if (Test-Path "docker-compose.yml") {
  if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose -f "docker-compose.yml" up -d
  } elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    docker-compose -f "docker-compose.yml" up -d
  } else {
    Write-Host "Docker not found. Skipping PostgreSQL startup."
  }
}

if ($Autostart) {
  & ".\\install_autostart.bat"
}

Write-Host "Install completed."
