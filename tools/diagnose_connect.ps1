# 拼多多桥接助手 —— 连接诊断（换机器/新装后跑一遍）
# 用法：右键"使用 PowerShell 运行"，或
#   powershell -NoProfile -ExecutionPolicy Bypass -File diagnose_connect.ps1
$ErrorActionPreference = 'SilentlyContinue'
$installDirs = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\PddBridgeAgent')
)

function Line($text) { Write-Host $text }
function Head($text) { Write-Host ""; Write-Host "===== $text =====" -ForegroundColor Cyan }

Head "1. 桥接进程"
$procs = Get-Process PddBridgeAgent, LocalSeatGateway, PddAdsorbWindow
if ($procs) { $procs | Select-Object ProcessName, Id, StartTime | Format-Table -AutoSize | Out-String | Write-Host }
else { Line "没有桥接进程在跑（PddBridgeAgent 未启动）" }

Head "2. 配置（令牌只显示长度）"
$cfgPath = $null
foreach ($dir in $installDirs) {
    $candidate = Join-Path $dir 'bridge_config.json'
    if (Test-Path -LiteralPath $candidate) { $cfgPath = $candidate; break }
}
if (-not $cfgPath) {
    Line "找不到 bridge_config.json（安装目录：$($installDirs -join ', ')）"
} else {
    Line "配置：$cfgPath"
    $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Line ("server_url        : " + $cfg.server_url)
    Line ("agent_token       : " + $(if ($cfg.agent_token) { "已配置（长度 $($cfg.agent_token.Length)）" } else { "**空 —— 这是连不上的头号原因**" }))
    Line ("agent_id          : " + $cfg.agent_id)
    Line ("device_id         : " + $cfg.device_id)
    Line ("agent_name        : " + $cfg.agent_name)
    Line ("data_source       : " + $cfg.data_source)
    Line ("recv_via_dll      : " + $cfg.recv_via_dll + "   recv_engine=" + $cfg.recv_engine)
    Line ("cdp_port          : " + $cfg.cdp_port)
}

Head "3. 到中心"
$server = if ($cfg.server_url) { $cfg.server_url } else { 'http://47.112.219.14:18765' }
$uri = [Uri]$server
$port = if ($uri.Port -gt 0) { $uri.Port } else { 80 }
Line "目标：$server（$($uri.Host):$port）"
$dns = Resolve-DnsName $uri.Host -Type A -ErrorAction SilentlyContinue
if ($dns) { Line ("DNS  : " + (($dns | Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress) -join ', ')) }
else { Line "DNS  : 解析失败（域名写错或没有外网）" }
$tcp = Test-NetConnection -ComputerName $uri.Host -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
Line "TCP  : $(if ($tcp) { '通' } else { '不通（防火墙/网络/中心没起来）' })"
try {
    $resp = Invoke-WebRequest -Uri $server -TimeoutSec 8 -UseBasicParsing -Method Head
    Line ("HTTP : " + [int]$resp.StatusCode + "  服务器时间=" + $resp.Headers['Date'])
} catch {
    Line ("HTTP : 失败 " + $_.Exception.Message)
}

Head "4. PDD 工作台 / CDP"
$wb = Get-CimInstance Win32_Process -Filter "Name='PddWorkbench.exe'"
if (-not $wb) { Line "PddWorkbench.exe 没在跑（客户端没启动）" }
else {
    foreach ($p in $wb) {
        Line ("PddWorkbench PID=" + $p.ProcessId + "  路径=" + $p.ExecutablePath)
        Line ("  版本=" + (Get-Item -LiteralPath $p.ExecutablePath).VersionInfo.FileVersion)
        Line ("  调试端口参数=" + $(if ($p.CommandLine -match 'remote-debugging-port=(\d+)') { $matches[1] } else { '**命令行里没有 --remote-debugging-port，桥接会一直扫不到会话**' }))
    }
}
foreach ($p in @(57165) + @($cfg.cdp_port)) {
    if (-not $p) { continue }
    $alive = Test-NetConnection -ComputerName 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue
    Line "CDP 端口 $p : $(if ($alive) { '通' } else { '不通' })"
}

Head "5. 日志尾部（重点看这些关键字）"
foreach ($dir in $installDirs) {
    $log = Join-Path $dir 'logs\bridge-pipeline.log'
    if (-not (Test-Path -LiteralPath $log)) { continue }
    Line "日志：$log"
    $keys = '注册|连不上|agent_token|采集告警|原生接收|CDP|未发现|会话已挂|注入|未绑定'
    Get-Content -LiteralPath $log -Tail 400 -Encoding UTF8 |
        Where-Object { $_ -match $keys } |
        Select-Object -Last 25 | ForEach-Object { Line ("  " + $_) }
}

Head "完成"
Line "把上面整段输出发回给技术支持即可定位。"
