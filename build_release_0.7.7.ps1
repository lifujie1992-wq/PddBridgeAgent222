# 拼多多桥接助手 0.7.7.0 —— 打包脚本（与 build_release.ps1 同流程，差异：
#   1) Python 解释器可指定，默认 3.10，缺失时回落 py -3.12；
#   2) 原生通道随包分发 pdd_send_v6.dll（含发图 SendImageMsg）+ injector.exe；
#   3) PddJumpHelper.exe 缺失时从 0.7.6 交付包取一份，不让构建直接失败。
param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "build-dist-release"),
    [string]$Python = "",
    [string]$NativeOut = "D:\temp\pdd-send-hook\out",
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
    throw "没有找到装好 PyInstaller 的 Python（试过 Python310/Python312）"
}

$python = Resolve-Python $Python
Write-Host "Python:  $python"
& $python -c "import bridge, sys; sys.path.insert(0,'.'); print('build version:', __import__('bridge').__version__)"
if ($LASTEXITCODE -ne 0) { throw "读取版本号失败" }

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
    --hidden-import bridge.center_ws `
    --hidden-import bridge.command_journal `
    --hidden-import bridge.config `
    --hidden-import bridge.parser `
    --hidden-import bridge.pdd_context `
    --hidden-import bridge.pdd_recv `
    --hidden-import bridge.pdd_direct_inject `
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

$agentBundle = Join-Path $agentDist "PddBridgeAgent"
Copy-Item -Force (Join-Path $gatewayDist "LocalSeatGateway\LocalSeatGateway.exe") $agentBundle
Copy-Item -Recurse -Force (Join-Path $gatewayDist "LocalSeatGateway\_gateway_internal") $agentBundle
Copy-Item -Force (Join-Path $adsorbDist "PddAdsorbWindow\PddAdsorbWindow.exe") $agentBundle
Copy-Item -Recurse -Force (Join-Path $adsorbDist "PddAdsorbWindow\_adsorb_internal") $agentBundle

# --- 跳转助手 ---
$jump = $JumpHelper
if (-not $jump) {
    $jumpCandidates = @(
        (Join-Path $PSScriptRoot "PddJumpHelper.exe"),
        "D:\QianniuAIService\builds\pdd-0.7.6.0\staging\PddJumpHelper.exe",
        "D:\temp\delivery\PddBridgeAgent222-main\PddJumpHelper.exe"
    )
    foreach ($c in $jumpCandidates) { if (Test-Path -LiteralPath $c) { $jump = $c; break } }
}
if (-not $jump -or -not (Test-Path -LiteralPath $jump)) { throw "PddJumpHelper.exe 找不到（用 -JumpHelper 指定）" }
Copy-Item -Force $jump $agentBundle
Write-Host "Jump:    $jump"

# --- 原生通道：v5 才有接收管道；v3 仅作老发送通道兜底 ---
$nativeCopied = @()
foreach ($name in @("pdd_send_v6.dll", "injector.exe")) {
    $src = Join-Path $NativeOut $name
    if (Test-Path -LiteralPath $src) {
        Copy-Item -Force $src $agentBundle
        $nativeCopied += $name
    }
}
if (($nativeCopied -contains "pdd_send_v6.dll") -and $nativeCopied -contains "injector.exe") {
    Write-Host "Native:  $($nativeCopied -join ', ') (copied into agent bundle)"
} else {
    Write-Warning "原生 DLL/注入器不全（已复制: $($nativeCopied -join ', ')），包里将没有原生通道"
}

$missing = @()
foreach ($name in @("LocalSeatGateway.exe", "_gateway_internal", "PddAdsorbWindow.exe",
                    "_adsorb_internal", "PddJumpHelper.exe", "injector.exe")) {
    if (-not (Test-Path (Join-Path $agentBundle $name))) { $missing += $name }
}
if ($missing.Count -gt 0) { throw "agent bundle missing: $($missing -join ', ')" }

Write-Host "Agent:   $agentBundle"
Write-Host "Output:  $agentBundle (安装包源目录)"
