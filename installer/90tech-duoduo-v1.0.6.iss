; 90科技多多助手 1.0.0（基于 0.7.6 稳定版）
;   默认中心：http://47.112.219.14:18765
;   1) 全新 AppId + 独立安装目录（Programs\DuoDuoHelper），不与旧版桥接助手互相覆盖；
;   2) 升级安装保留 bridge_config.json / 队列 / 日志（不动工位令牌与聊天缓存）；
;   3) 安装前按「安装目录下的 exe」+「4 个已知进程名」双重清理，避免文件被占用；
;   4) 0.7.6 不注入任何 DLL，所以安装不会关闭拼多多工作台。

#define MyAppChinese "90科技多多助手"
#define MyAppVersion "1.0.6"
#ifndef MySource
#define MySource AddBackslash(SourcePath) + "..\build-dist-release\agent\PddBridgeAgent"
#endif
#ifndef MyOutput
#define MyOutput AddBackslash(SourcePath) + "..\dist"
#endif

[Setup]
AppId={{A3A60142-06D6-4269-936A-99BCB9F8409D}
AppName={#MyAppChinese}
AppVersion={#MyAppVersion}
AppVerName={#MyAppChinese} {#MyAppVersion}
AppPublisher=90科技
DefaultDirName={localappdata}\Programs\DuoDuoHelper
DefaultGroupName={#MyAppChinese}
AllowNoIcons=yes
OutputDir={#MyOutput}
OutputBaseFilename=90科技多多助手-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\release_assets\pdd_90.ico
UninstallDisplayIcon={app}\PddBridgeAgent.exe
DisableDirPage=no
DisableProgramGroupPage=yes

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 %1
WelcomeLabel1=欢迎使用「{#MyAppChinese}」安装向导
WelcomeLabel2=这会把「{#MyAppChinese} {#MyAppVersion}」装到你的电脑上。%n%n升级安装时：会自动关闭正在运行的桥接程序，并保留你原有的工位配置（bridge_config.json）、聊天缓存和日志 —— 不用重新填令牌。%n%n点「下一步」继续。
SelectDirDesc=桥接助手装在哪里？
SelectDirLabel3=请选择安装目录。%n%n老用户请保持原目录不变（直接覆盖升级）。
SelectDirBrowseLabel=点「下一步」使用默认目录，或点「浏览」换一个。
SelectTasksDesc=要顺便做哪些事？
SelectTasksLabel2=勾选你需要的附加任务，然后点「下一步」：
ReadyLabel1=准备就绪，可以开始安装了。
ReadyLabel2a=点「安装」开始；想改设置就点「上一步」。
InstallingLabel=正在安装，请稍候…
FinishedLabel=安装完成。「{#MyAppChinese}」已在你的电脑上，桌面快捷方式可以直接启动。
ClickNext=下一步(&N) >
ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消
ConfirmUninstall=确定要卸载「{#MyAppChinese}」吗？%n%n（本机的配置、聊天缓存和日志不会被删除）
UninstallAppFullTitle=卸载 %1
ExitSetupTitle=退出安装
ExitSetupMessage=安装还没完成。现在退出将不会安装「{#MyAppChinese}」。%n%n确定要退出吗？

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务（可多选）："
Name: "autostart"; Description: "开机自动启动（登录后自动吸附浮窗）"; GroupDescription: "附加任务（可多选）："
Name: "startnow"; Description: "安装完成后立即启动"; GroupDescription: "附加任务（可多选）："

[Files]
; 注入到 PddWorkbench 里的 DLL 会被工作台占用，普通覆盖会失败。
; 对这类文件用 restartreplace：本次先跳过、安排到下次重启替换，不阻断安装。
Source: "{#MySource}\*"; DestDir: "{app}"; \
  Excludes: "bridge_config.json,pdd_adsorb_config.json,bridge_queue_*.jsonl,bridge_commands_pdd.json,*.cursors.json,*.bak,data\*,logs\*,state\*,runtime\*"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; IconFilename: "{app}\PddBridgeAgent.exe"
Name: "{group}\卸载 {#MyAppChinese}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; IconFilename: "{app}\PddBridgeAgent.exe"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; Tasks: autostart

[Run]
Filename: "{app}\PddBridgeAgent.exe"; Description: "立即启动「{#MyAppChinese}」"; Flags: nowait postinstall skipifsilent; Tasks: startnow

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM PddBridgeAgent.exe /F /T"; RunOnceId: "KillAgent"; Flags: runhidden
Filename: "{sys}\taskkill.exe"; Parameters: "/IM LocalSeatGateway.exe /F /T"; RunOnceId: "KillGateway"; Flags: runhidden
Filename: "{sys}\taskkill.exe"; Parameters: "/IM PddAdsorbWindow.exe /F /T"; RunOnceId: "KillAdsorb"; Flags: runhidden
Filename: "{sys}\taskkill.exe"; Parameters: "/IM PddJumpHelper.exe /F /T"; RunOnceId: "KillJump"; Flags: runhidden

[Code]
{ ============================================================================
  安装前清理：把所有"历史版本"的进程都退掉
  ----------------------------------------------------------------------------
  老做法只按名字 taskkill 四个 exe，踩过这些坑：
    1) 早期版本进程名不同（PddSeatGateway/PddBridgeAgent-win 等）→ 漏杀
    2) 有人从源码目录直接跑（python.exe 在安装目录里）→ 漏杀，文件照样被占用
    3) 改名/换壳的 exe → 漏杀，安装到一半覆盖失败
  新做法：用 PowerShell 枚举"可执行文件路径位于安装目录下"的进程并按 PID 结束，
  不管它叫什么名字；再按已知进程名补杀一轮；最后双重复查，杀不干净就明确拦下。
  （不用 wmic：Win11 24H2 起已被系统移除，用它会静默失效。）
  ============================================================================ }

{ PowerShell 全路径：安装程序是提权进程，别指望 PATH 里有它 }

function PsExe(): String;
var
  P: String;
begin
  // 安装程序是 32 位，sys 常量会指向 SysWOW64；32 位 PowerShell 读不到
  // 64 位进程的 Path（枚举结果为空），必须用 sysnative 走真正的 System32
  P := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
  if FileExists(P) then
    Result := P
  else
    Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function Sq(const S: String): String;
begin
  { 包成 PowerShell 单引号字面量 }
  Result := '''' + S + '''';
end;

function TargetNameList(): TArrayOfString;
begin
  SetArrayLength(Result, 10);
  Result[8] := 'PddAdsorbWindow-win.exe';
  Result[9] := 'PddAdsorbWindow_x64.exe';
  Result[0] := 'PddBridgeAgent.exe';
  Result[1] := 'LocalSeatGateway.exe';
  Result[2] := 'PddAdsorbWindow.exe';
  Result[3] := 'PddJumpHelper.exe';
  Result[4] := 'PddSeatGateway.exe';
  Result[5] := 'PddBridgeAgent-win.exe';
  Result[6] := 'PddBridgeAgent_x64.exe';
  Result[7] := 'injector.exe';
end;

{ 结束所有"exe 位于安装目录下"的进程，返回 PowerShell 是否执行成功 }
function KillUnderAppDir(): Boolean;
var
  Cmd: String;
  ResultCode: Integer;
begin
  { 注意：Cmd 里绝不能出现双引号，否则会提前截断 Exec 的 -Command 参数 }
  Cmd := '$app = ' + Sq(ExpandConstant('{app}')) + ';' +
         'Get-Process -ErrorAction SilentlyContinue |' +
         'Where-Object { $_.Path -and $_.Path.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) } |' +
         'Stop-Process -Force -ErrorAction SilentlyContinue;' +
         'exit 0';
  Exec(PsExe(),
       '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := ResultCode = 0;
end;

{ 按已知进程名补杀（覆盖 exe 已删除但进程还在、或路径读不到的情况） }
function KillByName(): Integer;
var
  Names: TArrayOfString;
  I, ResultCode: Integer;
  One: String;
begin
  Result := 0;
  Names := TargetNameList();
  for I := 0 to GetArrayLength(Names) - 1 do
  begin
    One := Names[I];
    if One = '' then
      continue;
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM ' + One + ' /F /T', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if ResultCode = 0 then
      Result := Result + 1;
  end;
end;

{ 复查：安装目录下还剩几个进程（PowerShell 直接算出来写进临时文件） }
function CountUnderAppDir(): Integer;
var
  Cmd, TempFile: String;
  ResultCode: Integer;
  Lines: TArrayOfString;
begin
  Result := 0;
  TempFile := ExpandConstant('{tmp}\pdd_left.txt');
  DeleteFile(TempFile);
  Cmd := '$app = ' + Sq(ExpandConstant('{app}')) + ';' +
         '$n = @(Get-Process -ErrorAction SilentlyContinue | ' +
         'Where-Object { $_.Path -and $_.Path.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) }).Count;' +
         '$n.ToString() | Set-Content -Encoding ASCII ' + Sq(TempFile) + ';' +
         'exit 0';
  Exec(PsExe(),
       '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if LoadStringsFromFile(TempFile, Lines) and (GetArrayLength(Lines) > 0) then
  begin
    try
      Result := StrToInt(Trim(Lines[0]));
    except
      Result := 0;
    end;
  end;
end;

{ 复查：按名字还剩几个（tasklist 双保险） }
function CountByName(): Integer;
var
  TempFile, Line: String;
  ResultCode, I, NameIdx: Integer;
  Lines, Names: TArrayOfString;
begin
  Result := 0;
  TempFile := ExpandConstant('{tmp}\pdd_tasklist.txt');
  DeleteFile(TempFile);
  Exec(ExpandConstant('{sys}\cmd.exe'),
       '/c tasklist /FO CSV /NH > "' + TempFile + '"', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  if not LoadStringsFromFile(TempFile, Lines) then
    Exit;
  Names := TargetNameList();
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lowercase(Lines[I]);
    for NameIdx := 0 to GetArrayLength(Names) - 1 do
    begin
      if (Names[NameIdx] <> '') and (Pos(Lowercase(Names[NameIdx]), Line) > 0) then
      begin
        Result := Result + 1;
        Break;
      end;
    end;
  end;
end;

function RemainingTargets(): Integer;
begin
  Result := CountUnderAppDir() + CountByName();
end;

{ 杀掉桥接全家桶并确认真的退干净 }
function KillBridgeProcesses(): Boolean;
var
  Attempt: Integer;
begin
  Result := False;
  for Attempt := 1 to 4 do
  begin
    KillUnderAppDir();
    KillByName();
    if Attempt = 1 then
      Sleep(1500)     { 进程退出 + 文件句柄释放 }
    else
      Sleep(800);
    if RemainingTargets() = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;



{ 安装前清理旧版的自启动项：Startup 快捷方式 + HKCU Run。
  不删的话客户下次开机又会从旧目录拉起老版本，两个工位同时在线会重复回复。 }
function CleanOldAutostart(): Boolean;
var
  Cmd: String;
  ResultCode: Integer;
begin
  Cmd := '$targets = ' + Sq('PddBridgeAgent.exe|LocalSeatGateway.exe|PddAdsorbWindow.exe|PddJumpHelper.exe') + ';' +
         '$sh = New-Object -ComObject WScript.Shell;' +
         '$startup = Join-Path $env:APPDATA ' + Sq('Microsoft\Windows\Start Menu\Programs\Startup') + ';' +
         'Get-ChildItem $startup -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {' +
         '  try { $t = $sh.CreateShortcut($_.FullName).TargetPath } catch { $t = ' + Sq('') + ' };' +
         '  if ($t -match $targets) { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue } };' +
         '$run = ' + Sq('HKCU:\Software\Microsoft\Windows\CurrentVersion\Run') + ';' +
         'Get-ItemProperty $run -ErrorAction SilentlyContinue | ForEach-Object {' +
         '  $_.PSObject.Properties | Where-Object { $_.Name -notlike ' + Sq('PS*') + ' } | ForEach-Object {' +
         '    if ($_.Value -match $targets) {' +
         '      Remove-ItemProperty -Path $run -Name $_.Name -Force -ErrorAction SilentlyContinue } } };' +
         'exit 0';
  Exec(PsExe(), '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := ResultCode = 0;
end;

{ 首次安装时把旧版工位令牌/配置搬过来，客户不用重新填。
   只挑 token 有效、最近修改的那份，并把 DLL 直发强行关掉（1.0.0 不注入）。 }
function ImportExistingConfig(): Boolean;
var
  Cmd: String;
  ResultCode: Integer;
begin
  Cmd := '$dst = ' + Sq(ExpandConstant('{app}\bridge_config.json')) + ';' +
         'if (Test-Path $dst) { exit 0 };' +
         '$cands = @();' +
         '$prog = Join-Path $env:LOCALAPPDATA ' + Sq('Programs') + ';' +
         'Get-ChildItem $prog -Directory -ErrorAction SilentlyContinue |' +
         '  Where-Object { $_.Name -like ' + Sq('PddBridgeAgent*') + ' -or $_.Name -like ' + Sq('DuoDuoHelper*') + ' } |' +
         '  ForEach-Object { $p = Join-Path $_.FullName ' + Sq('bridge_config.json') + ';' +
         '    if (Test-Path $p) { $cands += (Get-Item $p) } };' +
         '$cands = $cands | Sort-Object LastWriteTime -Descending;' +
         'foreach ($c in $cands) {' +
         '  try { $j = Get-Content $c.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue };' +
         '  if (-not $j.agent_token -or $j.agent_token -like ' + Sq('change-me*') + ') { continue };' +
         '  $j.send_via_dll = $false; $j.recv_via_dll = $false;' +
         '  if (-not $j.server_url) { $j.server_url = ' + Sq('http://47.112.219.14:18765') + ' };' +
         '  $rt = Get-ChildItem (Join-Path $env:PUBLIC ' + Sq('PddPortableRuntime') + ') -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending;' +
         '  if ($rt) { $logs = Join-Path $rt[0].FullName ' + Sq('plugins\logs') + ';' +
         '    if ((Test-Path $logs) -and (-not $j.tanyu_log_dir -or -not (Test-Path $j.tanyu_log_dir))) { $j.tanyu_log_dir = $logs } };' +
         '  ($j | ConvertTo-Json -Depth 6) | Set-Content -Path $dst -Encoding UTF8;' +
         '  Write-Output (' + Sq('imported ') + ' + $c.FullName);' +
         '  exit 0 };' +
         'exit 0';
  Exec(PsExe(), '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := ResultCode = 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Left: Integer;
begin
  Result := '';
  NeedsRestart := False;
  CleanOldAutostart();
  if not KillBridgeProcesses() then
  begin
    Left := RemainingTargets();
    Result := '还有 ' + IntToStr(Left) + ' 个桥接相关进程没能退出。' + #13#10#13#10 +
              '常见原因：' + #13#10 +
              '  · 它们以管理员权限运行，本安装包权限不够' + #13#10 +
              '  · 有人从源码目录直接跑着桥接（python.exe）' + #13#10#13#10 +
              '请先手动退出桥接助手（右下角托盘 → 退出），或结束这些进程后重试。' + #13#10 +
              '安装目录：' + ExpandConstant('{app}');
  end;
end;

{ 1.0.6：升级前把 data\ 整目录复制一份（data.bak-<新版本>-<时间>）。
  现场 1.0.4→1.0.5 升级后本地会话被清空且无任何备份。 }
function BackupDataDir(): Boolean;
var
  Cmd: String;
  ResultCode: Integer;
begin
  Cmd := '$d = ' + Sq(ExpandConstant('{app}\data')) + ';' +
         'if (Test-Path $d) {' +
         '  $b = $d + ' + Sq('.bak-{#MyAppVersion}-') + ' + (Get-Date -Format ' + Sq('yyyyMMdd-HHmmss') + ');' +
         '  Copy-Item -Path $d -Destination $b -Recurse -Force -ErrorAction SilentlyContinue };' +
         'exit 0';
  Exec(PsExe(), '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := ResultCode = 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    BackupDataDir();
  if CurStep = ssPostInstall then
    ImportExistingConfig();
end;

function InitializeUninstall(): Boolean;
begin
  KillBridgeProcesses();
  Result := True;
end;
