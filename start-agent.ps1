$ErrorActionPreference = "Stop"

# =========================
# Настройки
# Откройте этот файл и поменяйте URL сервера и токен на реальные.
# Токен должен быть таким же, как в start-server.ps1 на сервере.
# =========================
$AgentServerUrl = "http://192.168.2.20:5002"
$AgentToken = "my-very-long-random-secret-token-2026"
$AgentId = ""
$AgentPollIntervalSec = "2"
$AgentStatusPushIntervalSec = "2"
$AgentCommandWorkers = "6"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Ensure-Venv {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Step "Создаю виртуальное окружение .venv"
        python -m venv .venv
    }
}

function Ensure-Packages {
    Write-Step "Проверяю зависимости Python"

    $checkScript = @'
import importlib.util
import sys

required = {
    "flask": "flask",
    "yaml": "pyyaml",
    "sqlalchemy": "sqlalchemy",
    "paho.mqtt.client": "paho-mqtt",
}

missing = []
for module_name, package_name in required.items():
    if importlib.util.find_spec(module_name) is None:
        missing.append(package_name)

if missing:
    print(" ".join(missing))
    sys.exit(1)
'@

    $tmpPath = Join-Path $PWD ".tmp_check_packages_agent.py"
    Set-Content -Path $tmpPath -Value $checkScript -Encoding UTF8

    try {
        & ".\.venv\Scripts\python.exe" $tmpPath
        $missingText = $LASTEXITCODE
    }
    catch {
        $missingText = 1
    }
    finally {
        Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
    }

    if ($missingText -ne 0) {
        Write-Step "Устанавливаю зависимости"
        & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
        & ".\.venv\Scripts\python.exe" -m pip install flask pyyaml sqlalchemy paho-mqtt
    }
}

if (-not (Test-Path "agent.py")) {
    throw "Файл agent.py не найден. Запустите скрипт из папки проекта."
}

Write-Step "Подготовка окружения локального агента"
Ensure-Venv
Ensure-Packages

if (-not $env:PRINTFARM_SERVER_URL) {
    $env:PRINTFARM_SERVER_URL = $AgentServerUrl
}

if (-not $env:PRINTFARM_AGENT_ID) {
    if ([string]::IsNullOrWhiteSpace($AgentId)) {
        $env:PRINTFARM_AGENT_ID = $env:COMPUTERNAME
    }
    else {
        $env:PRINTFARM_AGENT_ID = $AgentId
    }
}

if (-not $env:PRINTFARM_AGENT_TOKEN) {
    $env:PRINTFARM_AGENT_TOKEN = $AgentToken
}

if (-not $env:PRINTFARM_AGENT_POLL_INTERVAL_SEC) {
    $env:PRINTFARM_AGENT_POLL_INTERVAL_SEC = $AgentPollIntervalSec
}

if (-not $env:PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC) {
    $env:PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC = $AgentStatusPushIntervalSec
}

if (-not $env:PRINTFARM_AGENT_COMMAND_WORKERS) {
    $env:PRINTFARM_AGENT_COMMAND_WORKERS = $AgentCommandWorkers
}

Write-Step "Параметры запуска"
Write-Host "PRINTFARM_SERVER_URL=$env:PRINTFARM_SERVER_URL"
Write-Host "PRINTFARM_AGENT_ID=$env:PRINTFARM_AGENT_ID"
Write-Host "PRINTFARM_AGENT_TOKEN=$env:PRINTFARM_AGENT_TOKEN"
Write-Host "PRINTFARM_AGENT_POLL_INTERVAL_SEC=$env:PRINTFARM_AGENT_POLL_INTERVAL_SEC"
Write-Host "PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC=$env:PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC"
Write-Host "PRINTFARM_AGENT_COMMAND_WORKERS=$env:PRINTFARM_AGENT_COMMAND_WORKERS"
Write-Host ""
Write-Host "Если URL = https://YOUR-SERVER-URL или токен = CHANGE_ME_TO_LONG_RANDOM_TOKEN, сначала задайте реальные значения." -ForegroundColor Yellow
Write-Host "Примеры перед запуском:" -ForegroundColor Yellow
Write-Host '$env:PRINTFARM_SERVER_URL = "https://printfarm.example.com"' -ForegroundColor Yellow
Write-Host '$env:PRINTFARM_AGENT_TOKEN = "my-super-secret-token"' -ForegroundColor Yellow

Write-Step "Запускаю локальный агент"
& ".\.venv\Scripts\python.exe" "agent.py"
