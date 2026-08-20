# QuantDesk one-click launcher (Windows).
# Called by start.bat / stop.bat from the repo root.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $Root "docker-compose.yml"))) {
    $Root = Get-Location
}
Set-Location $Root

$Action = if ($args.Count -ge 1) { $args[0].ToLowerInvariant() } else { "start" }
$Url = "http://127.0.0.1:18080"
$LocalApi = "http://127.0.0.1:8000"
$LocalWeb = "http://127.0.0.1:5173"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Ensure-EnvFile {
    $envPath = Join-Path $Root ".env"
    $example = Join-Path $Root ".env.example"
    if (-not (Test-Path $envPath)) {
        if (-not (Test-Path $example)) {
            throw "找不到 .env.example，请确认当前目录是 QuantDesk 项目根目录。"
        }
        Copy-Item $example $envPath
        Write-Host "已自动创建 .env（模拟盘密钥可稍后填写，实盘开关请保持关闭）。" -ForegroundColor Yellow
    }
}

function Test-DockerDaemon {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return $false
    }
    docker info 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Start-DockerDesktopIfNeeded {
    if (Test-DockerDaemon) { return $true }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }

    $desktop = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LocalAppData\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $desktop) { return $false }

    Write-Step "正在启动 Docker Desktop，请稍等（约 30–90 秒）…"
    Start-Process $desktop | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        if (Test-DockerDaemon) { return $true }
        Write-Host "." -NoNewline
    }
    Write-Host ""
    return (Test-DockerDaemon)
}

function Wait-Http($target, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $target -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) {
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Open-Browser($target) {
    try { Start-Process $target } catch {
        Write-Host "请手动打开浏览器访问 $target" -ForegroundColor Yellow
    }
}

function Start-WithDocker {
    Write-Step "使用 Docker 启动（首次会下载镜像，可能需要几分钟）"
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 启动失败。请确认 Docker Desktop 已安装并正在运行。"
    }
    Write-Step "等待服务就绪…"
    $health = "$Url/health"
    $ok = (Wait-Http $health 300) -or (Wait-Http $Url 30)
    Open-Browser $Url
    Write-Host ""
    Write-Host "QuantDesk 已启动：$Url" -ForegroundColor Green
    Write-Host "API 文档：$Url/docs"
    Write-Host "关闭方式：双击 stop.bat  或  关闭QuantDesk.bat"
    if (-not $ok) {
        Write-Host "服务仍在启动中，请稍等后刷新浏览器。" -ForegroundColor Yellow
    }
}

function Test-Python {
    return [bool]((Get-Command py -ErrorAction SilentlyContinue) -or (Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command python3 -ErrorAction SilentlyContinue))
}

function New-Venv($venvPath) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv $venvPath
        return
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv $venvPath
        return
    }
    python3 -m venv $venvPath
}

function Start-LocalDev {
    if (-not (Test-Python)) {
        throw "未检测到 Docker，也未检测到 Python。请先安装 Docker Desktop（推荐）或 Python 3.11+。"
    }

    Write-Step "未使用 Docker，改为本机模式（功能可能不完整，数据库会走 JSON 回退）"
    $venv = Join-Path $Root ".venv"
    $pythonExe = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        Write-Host "正在创建虚拟环境并安装依赖（首次较慢）…"
        New-Venv $venv
        & $pythonExe -m pip install -U pip
        & $pythonExe -m pip install -r (Join-Path $Root "requirements.txt")
    }

    $apiJob = Start-Process -FilePath $pythonExe -ArgumentList @(
        "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $Root -PassThru -WindowStyle Minimized

    $openUrl = $LocalApi
    $node = Get-Command npm -ErrorAction SilentlyContinue
    if ($node) {
        $webDir = Join-Path $Root "web"
        if (-not (Test-Path (Join-Path $webDir "node_modules"))) {
            Write-Host "正在安装前端依赖…"
            Push-Location $webDir
            npm install
            Pop-Location
        }
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory $webDir -WindowStyle Minimized
        $openUrl = $LocalWeb
        Start-Sleep -Seconds 4
    } else {
        Write-Host "未检测到 Node.js，仅启动 API。安装 Node.js 后可打开网页界面。" -ForegroundColor Yellow
    }

    Wait-Http $LocalApi 60 | Out-Null
    Open-Browser $openUrl
    Write-Host ""
    Write-Host "QuantDesk 本机模式已启动：$openUrl" -ForegroundColor Green
    Write-Host "API：$LocalApi   文档：$LocalApi/docs"
    Write-Host "关闭本窗口不会自动停服务；请在任务管理器结束 python / node，或改用 Docker 模式。"
}

function Stop-All {
    if (Test-DockerDaemon) {
        Write-Step "正在停止 Docker 服务"
        docker compose down
        Write-Host "已停止。" -ForegroundColor Green
        return
    }
    Write-Host "Docker 未运行。本机模式请手动结束 python.exe / node 进程。" -ForegroundColor Yellow
}

Ensure-EnvFile

if ($Action -eq "stop") {
    Stop-All
    exit 0
}

if (Start-DockerDesktopIfNeeded) {
    Start-WithDocker
    exit 0
}

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "已安装 Docker，但引擎未就绪。请打开 Docker Desktop 等到左下角变绿后再双击启动。" -ForegroundColor Yellow
    Start-Process "https://www.docker.com/products/docker-desktop/"
    exit 1
}

Start-LocalDev
