# 90科技多多助手 1.0.0 —— 基于 0.7.6 稳定版打包
param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "build-dist-release"),
    [string]$Python = "",
    [string]$JumpHelper = ""
)
$ErrorActionPreference = "Continue"

function Resolve-Python([string]$explicit) {
    if ($explicit -and (Test-Path -LiteralPath $explicit)) { return $explicit }
    $candidates = @(
        (Join-Path $env:LocalAppData "Programs\Python\Python310\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            & $c -c "import PyInstaller" 2>$null
            if ($LASTEXITCODE -eq 0) { return $c }
        }
    }
    throw "没有找到装好 PyInstaller 的 Python"
}

$python = Resolve-Python $Python
Write-Host "Python: $python"
& $python -c "import sys; sys.path.insert(0,'.'); import bridge; print('build version:', bridge.__version__)"
if ($LASTEXITCODE -ne 0) { throw "读取版本号失败" }

$agentDist = Join-Path $OutputRoot "agent"
$gatewayDist = Join-Path $OutputRoot "gateway"
$adsorbDist = Join-Path $OutputRoot "adsorb"
$workRoot = Join-Path $PSScriptRoot "build-work-release"
$specRoot = Join-Path $PSScriptRoot "build-spec-release"

& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name PddBridgeAgent `
    --icon (Join-Path $PSScriptRoot "release_assets\pdd_90.ico") `
    --distpath $agentDist `
    --workpath (Join-Path $workRoot "agent") `
    --specpath (Join-Path $specRoot "agent") `
    --paths $PSScriptRoot `
    --hidden-import bridge `
    --hidden-import bridge.agent `
    --hidden-import bridge.channel `
    --hidden-import bridge.client `
    --hidden-import bridge.center_ws `
    --hidden-import bridge.command_journal `
    --hidden-import bridge.config `
    --hidden-import bridge.parser `
    --hidden-import bridge.pdd_context `
    --hidden-import bridge.watcher `
    --hidden-import bridge.log_dir_resolver `
    --hidden-import bridge.workbench_watchdog `
    --hidden-import bridge.inside_ipc `
    --collect-submodules websockets `
    --hidden-import bridge.gui `
    --hidden-import bridge.platforms `
    --hidden-import bridge.platforms.pdd `
    --hidden-import bridge.pddbridge_source `
    --hidden-import bridge.pddbridge `
    --hidden-import bridge.pddbridge.cdp `
    --hidden-import bridge.pddbridge.protocol `
    --add-data ((Join-Path $PSScriptRoot "bridge\pddbridge\inject.js") + ";bridge\pddbridge") `
    --hidden-import tkinter `
    (Join-Path $PSScriptRoot "run_pdd_client.py")
if ($LASTEXITCODE -ne 0) { throw "PddBridgeAgent build failed" }

& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name LocalSeatGateway `
    --contents-directory _gateway_internal `
    --distpath $gatewayDist `
    --workpath (Join-Path $workRoot "gateway") `
    --specpath (Join-Path $specRoot "gateway") `
    --paths $PSScriptRoot `
    --add-data ((Join-Path $PSScriptRoot "web") + ";web") `
    (Join-Path $PSScriptRoot "run_frontend_service.py")
if ($LASTEXITCODE -ne 0) { throw "LocalSeatGateway build failed" }

& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name PddAdsorbWindow `
    --contents-directory _adsorb_internal `
    --distpath $adsorbDist `
    --workpath (Join-Path $workRoot "adsorb") `
    --specpath (Join-Path $specRoot "adsorb") `
    --paths $PSScriptRoot `
    (Join-Path $PSScriptRoot "pdd_adsorb_window.py")
if ($LASTEXITCODE -ne 0) { throw "PddAdsorbWindow build failed" }

$agentBundle = Join-Path $agentDist "PddBridgeAgent"
Copy-Item -Force (Join-Path $gatewayDist "LocalSeatGateway\LocalSeatGateway.exe") $agentBundle
Copy-Item -Recurse -Force (Join-Path $gatewayDist "LocalSeatGateway\_gateway_internal") $agentBundle
Copy-Item -Force (Join-Path $adsorbDist "PddAdsorbWindow\PddAdsorbWindow.exe") $agentBundle
Copy-Item -Recurse -Force (Join-Path $adsorbDist "PddAdsorbWindow\_adsorb_internal") $agentBundle

$jump = $JumpHelper
if (-not $jump) {
    foreach ($c in @(
        (Join-Path $PSScriptRoot "PddJumpHelper.exe"),
        "D:\QianniuAIService\builds\pdd-0.7.6.0\staging\PddJumpHelper.exe",
        "C:\Users\Administrator\AppData\Local\Programs\PddBridgeAgent0.7.6\PddJumpHelper.exe"
    )) { if (Test-Path -LiteralPath $c) { $jump = $c; break } }
}
if (-not $jump -or -not (Test-Path -LiteralPath $jump)) { throw "PddJumpHelper.exe 找不到（用 -JumpHelper 指定）" }
Copy-Item -Force $jump $agentBundle

$missing = @()
foreach ($name in @("LocalSeatGateway.exe", "_gateway_internal", "PddAdsorbWindow.exe",
                    "_adsorb_internal", "PddJumpHelper.exe")) {
    if (-not (Test-Path (Join-Path $agentBundle $name))) { $missing += $name }
}
if ($missing.Count -gt 0) { throw "agent bundle missing: $($missing -join ', ')" }

Write-Host "Agent: $agentBundle"
Write-Host "Jump:  $jump"
