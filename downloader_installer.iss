; -- Inno Setup Script --
#define MyAppName "PYDownloader"
#define MyAppVersion "1.0"
#define MyAppPublisher "gitsha256"
#define MyAppExeName "PYDownloader.exe"
#define MyAppOutputDir "Output"
#define MyAppSetupFileName MyAppName + "-setup"

[Setup]
AppId={{E820B8F5-CA2C-47AF-A6B7-DLWR-B31A8DFA6D09}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={pf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir={#MyAppOutputDir}
OutputBaseFilename={#MyAppSetupFileName}
Compression=lzma
SolidCompression=yes
DisableWelcomePage=no
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "PYDownloader.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "add.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "start.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "pause.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "remove.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "download.png"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Additional icons:"
