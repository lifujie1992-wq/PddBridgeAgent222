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

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Left: Integer;
begin
  Result := '';
  NeedsRestart := False;
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

function InitializeUninstall(): Boolean;
begin
  KillBridgeProcesses();
  Result := True;
end;
