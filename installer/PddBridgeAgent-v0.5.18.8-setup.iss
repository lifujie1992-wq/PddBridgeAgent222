#define MyAppChinese "拼多多桥接助手"
#define MyAppVersion "0.5.18.8"
#define MySource "D:/temp/PddBridgeAgent-v0.5.14-source/build-dist-release/agent/PddBridgeAgent"

[Setup]
AppId={{C4D6C4D1-2F7A-4553-BB06-8C246B4E4E92}
AppName={#MyAppChinese}
AppVersion={#MyAppVersion}
AppVerName={#MyAppChinese} v{#MyAppVersion}
AppPublisher=PddBridge
DefaultDirName={localappdata}/Programs/PddBridgeAgent
DefaultGroupName={#MyAppChinese}
UninstallDisplayName={#MyAppChinese}
UninstallDisplayIcon={app}/PddBridgeAgent.exe
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir=D:/temp/delivery
OutputBaseFilename=PddBridgeAgent-v0.5.18.8-setup

[Messages]
SelectDirDesc=选择安装位置
SelectDirLabel3=升级用户请选择现有部署目录，以保留 bridge_config.json 与 data/。
SelectDirBrowseLabel=若继续，请点击“下一步”。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "{#MySource}/*"; DestDir: "{app}"; Excludes: "bridge_config.json"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; Comment: "启动拼多多桥接助手"
Name: "{group}\聚合接待浮窗"; Filename: "{app}\PddBridgeAgent.exe"; Comment: "浮窗可在桥接助手面板一键唤醒"
Name: "{group}\卸载 {#MyAppChinese}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppChinese}"; Filename: "{app}\PddBridgeAgent.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PddBridgeAgent.exe"; Description: "立即启动 {#MyAppChinese}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\PddAdsorbWindow.exe"; Parameters: "--stop"; RunOnceId: "StopAdsorb"; Flags: runhidden
