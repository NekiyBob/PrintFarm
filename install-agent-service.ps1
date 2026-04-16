$ErrorActionPreference = "Stop"

param(
    [ValidateSet("Install", "Update", "Uninstall", "Start", "Stop", "Restart", "Status")]
    [string]$Action = "Install"
)

# =========================
# Settings
# Copy this folder to the clean PC, then run this script as Administrator.
# It will create .env.agent, create .venv, install dependencies,
# register a Windows Service, and enable auto-start.
# =========================
$ServiceName = "PrintFarmAgent"
$ServiceDisplayName = "PrintFarm Agent"
$ServiceDescription = "PrintFarm LAN agent that polls commands and syncs printer status."

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

function Assert-Admin {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        throw "Run PowerShell as Administrator."
    }
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

    throw "Python 3 was not found in PATH. Install Python 3.11 or 3.12 x64 with Add Python to PATH enabled."
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
    Write-Step "Installing agent and Windows Service dependencies"
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip"
    }

    & ".\.venv\Scripts\python.exe" -m pip install flask pyyaml sqlalchemy paho-mqtt pywin32
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python packages"
    }
}

function Write-AgentEnv {
    if ([string]::IsNullOrWhiteSpace($AgentServerUrl)) {
        throw "AgentServerUrl is empty"
    }
    if ([string]::IsNullOrWhiteSpace($AgentToken)) {
        throw "AgentToken is empty"
    }

    $resolvedAgentId = $AgentId
    if ([string]::IsNullOrWhiteSpace($resolvedAgentId)) {
        $resolvedAgentId = $env:COMPUTERNAME
    }

    $envLines = @(
        "PRINTFARM_SERVER_URL=$AgentServerUrl"
        "PRINTFARM_AGENT_ID=$resolvedAgentId"
        "PRINTFARM_AGENT_TOKEN=$AgentToken"
        "PRINTFARM_AGENT_POLL_INTERVAL_SEC=$AgentPollIntervalSec"
        "PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC=$AgentStatusPushIntervalSec"
        "PRINTFARM_AGENT_COMMAND_WORKERS=$AgentCommandWorkers"
    )

    Set-Content -Path ".env.agent" -Value $envLines -Encoding UTF8
}

function Ensure-AgentFiles {
    foreach ($requiredPath in @("agent.py", "agent_service.py", "printers.yaml")) {
        if (-not (Test-Path $requiredPath)) {
            throw ("Missing required file: {0}" -f $requiredPath)
        }
    }
}

function Invoke-ServiceScript {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & ".\.venv\Scripts\python.exe" ".\agent_service.py" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("agent_service.py failed: {0}" -f ($Arguments -join " "))
    }
}

function Set-ServiceRecovery {
    Write-Step "Configuring auto-start and recovery"

    & sc.exe description $ServiceName $ServiceDescription | Out-Null
    & sc.exe config $ServiceName start= auto | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
    & sc.exe failureflag $ServiceName 1 | Out-Null

    $serviceRegPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
    if (Test-Path $serviceRegPath) {
        New-ItemProperty -Path $serviceRegPath -Name DelayedAutostart -PropertyType DWord -Value 1 -Force | Out-Null
    }
}

function Install-OrUpdateService {
    Write-AgentEnv

    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne "Stopped") {
            Write-Step "Stopping existing service"
            Invoke-ServiceScript -Arguments @("stop")
        }

        Write-Step "Updating Windows Service"
        Invoke-ServiceScript -Arguments @("--startup", "auto", "update")
    }
    else {
        Write-Step "Installing Windows Service"
        Invoke-ServiceScript -Arguments @("--startup", "auto", "install")
    }

    Set-ServiceRecovery

    Write-Step "Starting service"
    Invoke-ServiceScript -Arguments @("start")
}

function Remove-ServiceSafe {
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Step "Service $ServiceName is not installed"
        return
    }

    if ($existing.Status -ne "Stopped") {
        Write-Step "Stopping service"
        Invoke-ServiceScript -Arguments @("stop")
    }

    Write-Step "Removing service"
    Invoke-ServiceScript -Arguments @("remove")
}

function Show-ServiceStatus {
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Service $ServiceName is not installed." -ForegroundColor Yellow
        return
    }

    $existing | Format-List Name, DisplayName, Status, StartType

    $logPath = Join-Path $PWD "logs\agent-service.log"
    if (Test-Path $logPath) {
        Write-Host ""
        Write-Host "Recent log lines: $logPath" -ForegroundColor Cyan
        Get-Content -Path $logPath -Tail 20
    }
}

Assert-Admin
Set-Location -Path $PSScriptRoot
Ensure-AgentFiles

switch ($Action) {
    "Install" {
        Ensure-Venv
        Ensure-Packages
        Install-OrUpdateService
    }
    "Update" {
        Ensure-Venv
        Ensure-Packages
        Install-OrUpdateService
    }
    "Uninstall" {
        Ensure-Venv
        Ensure-Packages
        Remove-ServiceSafe
    }
    "Start" {
        Ensure-Venv
        Ensure-Packages
        Write-AgentEnv
        Write-Step "Starting service"
        Invoke-ServiceScript -Arguments @("start")
    }
    "Stop" {
        Ensure-Venv
        Ensure-Packages
        Write-Step "Stopping service"
        Invoke-ServiceScript -Arguments @("stop")
    }
    "Restart" {
        Ensure-Venv
        Ensure-Packages
        Write-AgentEnv
        Write-Step "Restarting service"
        Invoke-ServiceScript -Arguments @("restart")
    }
    "Status" {
        Show-ServiceStatus
    }
}

Write-Step "Done"
