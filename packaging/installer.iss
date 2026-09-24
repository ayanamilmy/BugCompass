; Inno Setup 脚本 — 生成 Windows 安装包。
;
;   1) 先用 bugcompass.spec 构建 dist\BugCompass\（见 spec 内注释）
;   2) 安装 Inno Setup 6（https://jrsoftware.org/isinfo.php）
;   3) iscc packaging\installer.iss
;   4) 产物：packaging\Output\BugCompassSetup-<版本>.exe
;
; 注意：升级版本时同步修改下面的 MyAppVersion。

#define MyAppName "BugCompass"
#define MyAppVersion "0.5.0"
#define MyAppPublisher "BugCompass contributors"
#define MyAppExeName "BugCompass.exe"

[Setup]
AppId={{8E7B6C1A-52D4-4B1E-9C3F-BugCompass01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=BugCompassSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
; 允许每用户安装（写入 %LOCALAPPDATA%），避免要求管理员权限
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\BugCompass\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 用户数据（工作区、设置、日志）在 %APPDATA%\BugCompass，默认保留，由用户自行删除。
Type: files; Name: "{app}\*.log"
