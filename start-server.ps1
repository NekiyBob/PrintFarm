$ErrorActionPreference = "Stop"

# =========================
# Настройки
# Откройте этот файл и поменяйте значение токена на своё.
# Тот же самый токен нужно указать в start-agent.ps1 на локальном ПК.
# =========================
$ServerAgentToken = "CHANGE_ME_TO_LONG_RANDOM_TOKEN"

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

    $tmpPath = Join-Path $PWD ".tmp_check_packages_server.py"
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

if (-not (Test-Path "app.py")) {
    throw "Файл app.py не найден. Запустите скрипт из папки проекта."
}

Write-Step "Подготовка окружения сервера"
Ensure-Venv
Ensure-Packages

$env:PRINTFARM_ROLE = "server"

if (-not $env:PRINTFARM_AGENT_TOKEN) {
    $env:PRINTFARM_AGENT_TOKEN = $ServerAgentToken
}

Write-Step "Параметры запуска"
Write-Host "PRINTFARM_ROLE=$env:PRINTFARM_ROLE"
Write-Host "PRINTFARM_AGENT_TOKEN=$env:PRINTFARM_AGENT_TOKEN"
Write-Host ""
Write-Host "Если токен сейчас = CHANGE_ME_TO_LONG_RANDOM_TOKEN, остановите скрипт и задайте свой токен." -ForegroundColor Yellow
Write-Host "Пример перед запуском:" -ForegroundColor Yellow
Write-Host '$env:PRINTFARM_AGENT_TOKEN = "my-super-secret-token"' -ForegroundColor Yellow

Write-Step "Запускаю сервер"
& ".\.venv\Scripts\python.exe" "app.py"
