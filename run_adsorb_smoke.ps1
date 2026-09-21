param(
    [Parameter(Mandatory = $true)][string]$PackageRoot
)

$ErrorActionPreference = "Stop"
$serverScript = Join-Path $PSScriptRoot "smoke_pdd_adsorb_server.py"
$webRoot = Join-Path $PackageRoot "_gateway_internal\web"
$app = Join-Path $PackageRoot "PddAdsorbWindow.exe"
$logDir = Join-Path $PackageRoot "logs"
$screenPath = Join-Path $logDir "pdd_adsorb_smoke.png"
$resultPath = Join-Path $logDir "pdd_adsorb_smoke.json"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class SmokeWin32 {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lparam);
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lparam);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int length);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int length);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

    public static IntPtr FindEdgeAppWindow(int[] processIds) {
        var wanted = new HashSet<uint>();
        foreach (int processId in processIds) wanted.Add((uint)processId);
        IntPtr best = IntPtr.Zero;
        long bestArea = 0;
        EnumWindows((hwnd, unused) => {
            uint processId;
            GetWindowThreadProcessId(hwnd, out processId);
            if (!wanted.Contains(processId) || !IsWindowVisible(hwnd)) return true;
            var className = new StringBuilder(128);
            GetClassName(hwnd, className, className.Capacity);
            if (className.ToString() != "Chrome_WidgetWin_1") return true;
            Rect rect;
            if (!GetWindowRect(hwnd, out rect)) return true;
            long area = (long)(rect.Right - rect.Left) * (rect.Bottom - rect.Top);
            var title = new StringBuilder(512);
            GetWindowText(hwnd, title, title.Capacity);
            if (title.Length > 0) area += 1000000000L;
            if (area > bestArea) { bestArea = area; best = hwnd; }
            return true;
        }, IntPtr.Zero);
        return best;
    }

    public static string Title(IntPtr hwnd) {
        var text = new StringBuilder(512);
        GetWindowText(hwnd, text, text.Capacity);
        return text.ToString();
    }
}
"@

$python = Join-Path $env:LocalAppData "Programs\Python\Python310\python.exe"
$server = Start-Process -FilePath $python -ArgumentList @(
    $serverScript, "--port", "18768", "--web-root", $webRoot
) -WindowStyle Hidden -PassThru

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $runtime = Invoke-RestMethod -Uri "http://127.0.0.1:18768/api/runtime-config" -TimeoutSec 1
            if ($runtime.service -eq "pdd-local-seat-gateway") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $ready) { throw "smoke server did not start" }

    Start-Process -FilePath $app | Out-Null
    $windowHandle = [IntPtr]::Zero
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        $edgeIds = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
            Where-Object { $_.CommandLine -like "*pdd-adsorb-edge-profile*" } |
            Select-Object -ExpandProperty ProcessId)
        if ($edgeIds.Count) {
            $windowHandle = [SmokeWin32]::FindEdgeAppWindow([int[]]$edgeIds)
            if ($windowHandle -ne [IntPtr]::Zero) { break }
        }
    }
    if ($windowHandle -eq [IntPtr]::Zero) { throw "docked Edge window was not found" }
    Start-Sleep -Seconds 3
    $edgeIds = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -like "*pdd-adsorb-edge-profile*" } |
        Select-Object -ExpandProperty ProcessId)
    $renderedHandle = [SmokeWin32]::FindEdgeAppWindow([int[]]$edgeIds)
    if ($renderedHandle -ne [IntPtr]::Zero) { $windowHandle = $renderedHandle }

    $rect = New-Object SmokeWin32+Rect
    [void][SmokeWin32]::GetWindowRect($windowHandle, [ref]$rect)
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    Add-Type -AssemblyName System.Drawing
    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
        $bitmap.Save($screenPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    $windowTitle = [SmokeWin32]::Title($windowHandle)

    $stop = Start-Process -FilePath $app -ArgumentList "--stop" -WindowStyle Hidden -Wait -PassThru
    Start-Sleep -Seconds 2
    $residualController = @(Get-Process -Name PddAdsorbWindow -ErrorAction SilentlyContinue).Count
    $residualEdge = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -like "*pdd-adsorb-edge-profile*" }).Count
    $result = [ordered]@{
        window_title = $windowTitle
        width = $width
        height = $height
        screenshot = $screenPath
        stop_exit_code = $stop.ExitCode
        residual_controller = $residualController
        residual_profile_edge = $residualEdge
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $result | ConvertTo-Json
    if ($width -lt 240 -or $height -lt 320 -or $residualController -ne 0 -or $residualEdge -ne 0) {
        exit 1
    }
} finally {
    if (Test-Path -LiteralPath $app) {
        Start-Process -FilePath $app -ArgumentList "--stop" -WindowStyle Hidden -Wait | Out-Null
    }
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
