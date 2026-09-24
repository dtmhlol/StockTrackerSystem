; Inno Setup script for Stock Tracker. Build with installer\build.ps1, which passes
; the version from src\app_paths.py:  ISCC /DAppVersion=1.0.0 installer\StockTracker.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Stock Tracker"
#define AppExe "StockTracker.exe"

[Setup]
; Keep this GUID fixed forever: it is how Windows recognises upgrades of the same app.
AppId={{09AC96D6-3F59-4D47-BCE8-143E43C6101A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user by default (no administrator prompt); the installer
; offers "for all users" if the person running it chooses to.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=StockTracker-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\StockTracker\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Uninstalling removes the program but, by default, keeps the stock database and the
// encrypted Firebase credentials in %LOCALAPPDATA%\StockTracker, so a reinstall or
// upgrade never loses data. This offers to remove them too.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\StockTracker');
    if DirExists(DataDir) then
      if MsgBox('Also delete your stock data and saved Firebase credentials?' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                'Choose No to keep them (recommended if you plan to reinstall).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
