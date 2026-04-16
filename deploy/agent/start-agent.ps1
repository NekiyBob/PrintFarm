$ErrorActionPreference = "Stop"

# =========================
# Settings
# SERVER_BASE_URL and AGENT_SHARED_TOKEN are configured directly in agent.py.
# This script only sets the remaining runtime options.
# =========================
$AgentId = ""
$AgentPollIntervalSec = "2"
$AgentStatusPushIntervalSec = "2"
$AgentCommandWorkers = "6"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-BootstrapPython {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        return @{ Command = "py"; Args = @("-3") }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return @{ Command = "python"; Args = @() }
    }

    throw "Python 3 was not found in PATH."
}

function Ensure-Venv {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Step "Creating virtual environment"
        $bootstrap = Get-BootstrapPython
        & $bootstrap.Command @($bootstrap.Args + @("-m", "venv", ".venv"))
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create .venv"
        }
    }
}

function Ensure-Packages {
    Write-Step "Checking Python dependencies"

    $checkScript = @"
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
"@

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
        Write-Step "Installing dependencies"
        & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
        & ".\.venv\Scripts\python.exe" -m pip install flask pyyaml sqlalchemy paho-mqtt
    }
}

if (-not (Test-Path "agent.py")) {
    throw "agent.py was not found. Run this script from the project folder."
}

Write-Step "Preparing local agent environment"
Ensure-Venv
Ensure-Packages

if (-not $env:PRINTFARM_AGENT_ID) {
    if ([string]::IsNullOrWhiteSpace($AgentId)) {
        $env:PRINTFARM_AGENT_ID = $env:COMPUTERNAME
    }
    else {
        $env:PRINTFARM_AGENT_ID = $AgentId
    }
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

Write-Step "Launch settings"
Write-Host "PRINTFARM_SERVER_URL=from agent.py"
Write-Host "PRINTFARM_AGENT_TOKEN=from agent.py"
Write-Host "PRINTFARM_AGENT_ID=$env:PRINTFARM_AGENT_ID"
Write-Host "PRINTFARM_AGENT_POLL_INTERVAL_SEC=$env:PRINTFARM_AGENT_POLL_INTERVAL_SEC"
Write-Host "PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC=$env:PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC"
Write-Host "PRINTFARM_AGENT_COMMAND_WORKERS=$env:PRINTFARM_AGENT_COMMAND_WORKERS"
Write-Host ""
Write-Host "Edit SERVER_BASE_URL and AGENT_SHARED_TOKEN directly in agent.py before launch." -ForegroundColor Yellow

Write-Step "Starting local agent"
& ".\.venv\Scripts\python.exe" "agent.py"
