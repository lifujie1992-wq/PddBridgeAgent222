; PddBridgeAgent 0.8.10.0
;   默认中心：http://47.112.219.14:18765
; 关键点：
;   1) DefaultDirName 与老版本一致（{localappdata}\Programs\PddBridgeAgent），同事直接原地升级；
;   2) [Files] 排除 bridge_config.json / data / logs / 队列，升级不动同事的工位令牌和本地数据；
;   3) 安装前杀掉正在跑的 4 个进程，否则 dll/exe 被占用会覆盖失败；
;   4) 注入过 pdd_send_v6.dll 的工作台会被 RestartManager 关掉，装完自动重开
;      （RestartApplications=yes）—— 否则客户装完不手动重开，所有回复都发不出去。

#define MyAppChinese "90科技拼多多桥接助手"
#define MyAppVersion "0.8.10.0"
#ifndef MySource
#define MySource AddBackslash(SourcePath) + "..\build-dist-release\agent\PddBridgeAgent"
#endif
#ifndef MyOutput
#define MyOutput AddBackslash(SourcePath) + "..\dist"
#endif

[Setup]
AppId={{C4D6C4D1-2F7A-4553-BB06-8C246B4E4E92}
AppName={#MyAppChinese}
AppVersion={#MyAppVersion}
AppVerName={#MyAppChinese} {#MyAppVersion}
AppPublisher=90科技
DefaultDirName={localappdata}\Programs\PddBridgeAgent
DefaultGroupName={#MyAppChinese}
AllowNoIcons=yes
OutputDir={#MyOutput}
OutputBaseFilename=90科技拼多多桥接助手-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=yes
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
FinishedLabel=安装完成。「{#MyAppChinese}」已在你的电脑上。%n%n拼多多商家工作台会被自动重新打开；如果没打开，请手动双击它再试。
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
Source: "{#MySource}\pdd_send_v*.dll"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "{#MySource}\injector.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "{#MySource}\*"; DestDir: "{app}"; \
  Excludes: "bridge_config.json,pdd_adsorb_config.json,bridge_queue_*.jsonl,bridge_commands_pdd.json,*.cursors.json,*.bak,data\*,logs\*,state\*,runtime\*,pdd_send_v*.dll,injector.exe"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"
Name: "{group}\卸载 {#MyAppChinese}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; Tasks: desktopicon
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
  SetArrayLength(Result, 8);
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

{ 注入过 pdd_send_v6.dll 的工作台会被 RestartManager 关掉，而它没注册
  RegisterApplicationRestart，RestartApplications=yes 也拉不起来。
  所以：装之前先记下工作台的 exe 路径，装完再把它拉起来。 }
var
  WorkbenchPath: String;

function QueryWorkbenchPath(): String;
var
  TempFile, Cmd: String;
  ResultCode: Integer;
  Lines: TArrayOfString;
begin
  Result := '';
  TempFile := ExpandConstant('{tmp}\pdd_workbench_path.txt');
  DeleteFile(TempFile);
  Cmd := '$p = Get-Process PddWorkbench -ErrorAction SilentlyContinue |' +
         'Where-Object { $_.Path } | Select-Object -First 1;' +
         'if ($p) { $p.Path | Set-Content -Encoding ASCII ' + Sq(TempFile) + ' };' +
         'exit 0';
  Exec(PsExe(), '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if LoadStringsFromFile(TempFile, Lines) and (GetArrayLength(Lines) > 0) then
    Result := Trim(Lines[0]);
end;

procedure RestartWorkbench();
var
  Path: String;
  ErrorCode: Integer;
begin
  Path := WorkbenchPath;
  if (Path = '') or (not FileExists(Path)) then
    Exit;
  if ShellExec('open', Path, '', ExtractFileDir(Path), SW_SHOWNORMAL, ewNoWait, ErrorCode) then
    Log('workbench restarted: ' + Path)
  else
    Log('workbench restart failed: ' + Path);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Left: Integer;
begin
  Result := '';
  NeedsRestart := False;
  WorkbenchPath := QueryWorkbenchPath();
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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RestartWorkbench();
end;

function InitializeUninstall(): Boolean;
begin
  KillBridgeProcesses();
  Result := True;
end;
