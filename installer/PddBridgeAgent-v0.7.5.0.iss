; 拼多多桥接助手 —— 安装/升级包（0.7.5.0）
; 关键点：
;   1) DefaultDirName 与老版本一致（{localappdata}\Programs\PddBridgeAgent），同事直接原地升级；
;   2) [Files] 排除 bridge_config.json / data / logs / 队列，升级不动同事的工位令牌和本地数据；
;   3) 安装前杀掉正在跑的 4 个进程，否则 dll/exe 被占用会覆盖失败。

#define MyAppChinese "拼多多桥接助手"
#define MyAppVersion "0.7.5.0"
#define MySource "C:\Users\1\Desktop\PddBridgeAgent-src\PddBridgeAgent\build-dist-release\agent\PddBridgeAgent"
#define MyOutput "C:\Users\1\Desktop\dist"

[Setup]
AppId={{C4D6C4D1-2F7A-4553-BB06-8C246B4E4E92}
AppName={#MyAppChinese}
AppVersion={#MyAppVersion}
AppVerName={#MyAppChinese} {#MyAppVersion}
AppPublisher=PddBridge
DefaultDirName={localappdata}\Programs\PddBridgeAgent
DefaultGroupName={#MyAppChinese}
AllowNoIcons=yes
OutputDir={#MyOutput}
OutputBaseFilename=PddBridgeAgent-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
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
FinishedLabel=安装完成。「{#MyAppChinese}」已在你的电脑上。
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
Source: "{#MySource}\*"; DestDir: "{app}"; \
  Excludes: "bridge_config.json,pdd_adsorb_config.json,bridge_queue_*.jsonl,bridge_commands_pdd.json,*.cursors.json,*.bak,data\*,logs\*,state\*,runtime\*"; \
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
procedure KillBridgeProcesses();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM PddBridgeAgent.exe /F /T', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM LocalSeatGateway.exe /F /T', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM PddAdsorbWindow.exe /F /T', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM PddJumpHelper.exe /F /T', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillBridgeProcesses();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  KillBridgeProcesses();
  Result := True;
end;
