{ 把进程列表（可执行路径, PID）导到临时文件并读回来。
  Win11 24H2 起 wmic 已被移除，所以先试 wmic，失败再走 PowerShell（更可靠）。 }
function DumpProcessList(): TArrayOfString;
var
  ResultCode: Integer;
  TempFile, Ps1, Script, AppDir: String;
begin
  SetArrayLength(Result, 0);
  TempFile := ExpandConstant('{tmp}\pdd_procs.csv');
  DeleteFile(TempFile);

  { 1) 老系统：wmic }
  Exec(ExpandConstant('{sys}\cmd.exe'),
       '/c wmic process get ExecutablePath,ProcessId /format:csv > "' + TempFile + '" 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if LoadStringsFromFile(TempFile, Result) and (GetArrayLength(Result) > 1) then
    Exit;

  { 2) Win11：PowerShell 按路径前缀过滤（前缀匹配，不用通配符，避免路径里的 [] 干扰） }
  AppDir := ExpandConstant('{app}');
  Ps1 := ExpandConstant('{tmp}\pdd_list.ps1');
  Script :=
    '$app = ' + QuotedStr(AppDir) + ';' + #13#10 +
    'Get-Process -ErrorAction SilentlyContinue |' + #13#10 +
    '  Where-Object { $_.Path -and $_.Path.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) } |' + #13#10 +
    '  ForEach-Object { $_.Path + ' + QuotedStr(',') + ' + $_.Id } |' + #13#10 +
    '  Set-Content -Encoding ASCII ' + QuotedStr(TempFile) + #13#10;
  SaveStringToFile(Ps1, Script, False);
  DeleteFile(TempFile);
  Exec('powershell.exe',
       '-NoProfile -ExecutionPolicy Bypass -File "' + Ps1 + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not LoadStringsFromFile(TempFile, Result) then
    SetArrayLength(Result, 0);
end;

