# Build the PDD agent and its dedicated local seat gateway.
param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "build-dist-release")
)

# PyInstaller writes normal progress to stderr. Check LASTEXITCODE explicitly.
$ErrorActionPreference = "Continue"
$python = Join-Path $env:LocalAppData "Programs\Python\Python310\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python 3.10 not found: $python"
}

$agentDist = Join-Path $OutputRoot "agent"
$gatewayDist = Join-Path $OutputRoot "gateway"
$adsorbDist = Join-Path $OutputRoot "adsorb"
$workRoot = Join-Path $PSScriptRoot "build-work-release"
$specRoot = Join-Path $PSScriptRoot "build-spec-release"

& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name PddBridgeAgent `
    --distpath $agentDist `
    --workpath (Join-Path $workRoot "agent") `
    --specpath (Join-Path $specRoot "agent") `
    --paths $PSScriptRoot `
    --hidden-import bridge `
    --hidden-import bridge.agent `
    --hidden-import bridge.channel `
    --hidden-import bridge.client `
    --hidden-import bridge.command_journal `
    --hidden-import bridge.config `
    --hidden-import bridge.parser `
    --hidden-import bridge.pdd_context `
    --hidden-import bridge.watcher `
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

# The main executable expects both helpers beside PddBridgeAgent.exe.
$agentBundle = Join-Path $agentDist "PddBridgeAgent"
$gatewayExe = Join-Path $gatewayDist "LocalSeatGateway\LocalSeatGateway.exe"
$gatewayInternal = Join-Path $gatewayDist "LocalSeatGateway\_gateway_internal"
$adsorbExe = Join-Path $adsorbDist "PddAdsorbWindow\PddAdsorbWindow.exe"
$adsorbInternal = Join-Path $adsorbDist "PddAdsorbWindow\_adsorb_internal"
$jumpHelper = Join-Path $PSScriptRoot "PddJumpHelper.exe"
if (-not (Test-Path -LiteralPath $jumpHelper)) { throw "PddJumpHelper.exe not found: $jumpHelper" }
Copy-Item -Force $gatewayExe $agentBundle
Copy-Item -Recurse -Force $gatewayInternal $agentBundle
Copy-Item -Force $adsorbExe $agentBundle
Copy-Item -Recurse -Force $adsorbInternal $agentBundle
Copy-Item -Force $jumpHelper $agentBundle

# 原生通道（v0.7）: 发送/接收 DLL 与注入器随包分发；开发机路径缺失时不阻塞构建
$nativeDll = "D:\temp\pdd-send-hook\out\pdd_send_v3.dll"
$nativeInjector = "D:\temp\pdd-send-hook\out\injector.exe"
if ((Test-Path -LiteralPath $nativeDll) -and (Test-Path -LiteralPath $nativeInjector)) {
    Copy-Item -Force $nativeDll $agentBundle
    Copy-Item -Force $nativeInjector $agentBundle
    Write-Host "Native:  pdd_send_v3.dll + injector.exe (copied into agent bundle)"
} else {
    Write-Warning "native dll/injector not found, bundle ships without native channel"
}

$missing = @()
if (-not (Test-Path (Join-Path $agentBundle "LocalSeatGateway.exe"))) { $missing += "LocalSeatGateway.exe" }
if (-not (Test-Path (Join-Path $agentBundle "_gateway_internal"))) { $missing += "_gateway_internal" }
if (-not (Test-Path (Join-Path $agentBundle "PddAdsorbWindow.exe"))) { $missing += "PddAdsorbWindow.exe" }
if (-not (Test-Path (Join-Path $agentBundle "_adsorb_internal"))) { $missing += "_adsorb_internal" }
if (-not (Test-Path (Join-Path $agentBundle "PddJumpHelper.exe"))) { $missing += "PddJumpHelper.exe" }
if ($missing.Count -gt 0) { throw "agent bundle missing: $($missing -join ', ')" }

Write-Host "Agent:   $agentBundle"
Write-Host "Gateway: $gatewayExe (copied into agent bundle)"
Write-Host "Adsorb:  $adsorbExe (copied into agent bundle)"
Write-Host "Jump:    $jumpHelper (copied into agent bundle)"
