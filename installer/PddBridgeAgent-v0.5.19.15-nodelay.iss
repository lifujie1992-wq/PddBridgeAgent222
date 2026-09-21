#define MyAppChinese "拼多多桥接助手"
#define MyAppVersion "0.5.19.15"
#define MySource "D:/temp/PddBridgeAgent-v0.5.14-source/build-dist-051915-nodelay/agent/PddBridgeAgent"
#define MyDockConfig "D:/temp/PddBridgeAgent-v0.5.14-source/pdd_adsorb_config.json"

[Setup]
AppId={{C4D6C4D1-2F7A-4553-BB06-8C246B4E4E92}
AppName={#MyAppChinese}
AppVersion={#MyAppVersion}
AppVerName={#MyAppChinese} v{#MyAppVersion}
AppPublisher=PddBridge
DefaultDirName={localappdata}/Programs/PddBridgeAgent
DefaultGroupName={#MyAppChinese}
UninstallDisplayIcon={app}/PddBridgeAgent.exe
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir=D:/temp/delivery
OutputBaseFilename=PddBridgeAgent-v0.5.19.15-nodelay
DisableDirPage=no

[Messages]
SelectDirDesc=选择安装位置
SelectDirLabel3=请先退出旧桥接程序及浮窗，再选择现有安装目录升级；保留 bridge_config.json、data 和待确认队列。
SelectDirBrowseLabel=若继续，请点击“下一步”。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "{#MyDockConfig}"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#MySource}/*"; DestDir: "{app}"; Excludes: "bridge_config.json,pdd_adsorb_config.json,logs\*,data\*,state\*,runtime\*,*.jsonl,*.cursors.json,*.bak"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"
Name: "{group}\卸载 {#MyAppChinese}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PddBridgeAgent.exe"; Description: "立即启动 {#MyAppChinese}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\PddAdsorbWindow.exe"; Parameters: "--stop"; RunOnceId: "StopAdsorb"; Flags: runhidden
