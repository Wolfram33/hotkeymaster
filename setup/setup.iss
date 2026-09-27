; Installer für Hotkey-Master (Inno Setup 6).
; Gebaut wird über build.ps1, das die Version aus hotkey-master.py übergibt:
;   ISCC.exe /DMyAppVersion=1.6.0 setup\setup.iss
; Alle Pfade sind relativ zu diesem Ordner.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "Hotkey Master"
#define MyAppPublisher "Wolfram Consult GmbH & Co. KG"
#define MyAppURL "https://github.com/Wolfram33/hotkeymaster"
#define MyAppExeName "hotkey-master.exe"

[Setup]
; AppId nie ändern – daran erkennt Windows Updates bestehender Installationen
AppId={{6E734CE2-6359-4C5E-ABA0-536904F11BFC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases/latest
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
; Installation ohne Administratorrechte (pro Benutzer)
PrivilegesRequired=lowest
; Laufendes Programm vor dem Update beenden
CloseApplications=yes
OutputDir=..\dist
OutputBaseFilename=Hotkey-Master-Setup
SetupIconFile=..\icon.ico
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Programmordner aus build.ps1 (Nuitka --standalone, enthält auch LICENSE und NOTICE)
Source: "..\dist\Hotkey-Master\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Aus 1.6.0/1.6.1 (onefile) übrig gebliebene Lizenzkopien
Type: files; Name: "{app}\LICENSE.txt"
Type: files; Name: "{app}\NOTICE.txt"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
