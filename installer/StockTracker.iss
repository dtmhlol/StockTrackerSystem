; Inno Setup script for Stock Tracker. Build with installer\build.ps1, which passes
; the version from src\app_paths.py:  ISCC /DAppVersion=1.0.0 installer\StockTracker.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "StockTracker.exe"

; /DTestBuild makes a clearly separate installer (its own app ID, name and file name) so the
; upgrade path can be tested without touching a real installation.
#ifdef TestBuild
  #define AppName "Stock Tracker E2E TEST"
  #define AppGuid "{{FEEDFACE-0000-4000-8000-00000000E2E1}"
  #define OutputName "E2E-TEST-Setup-" + AppVersion
#else
  #define AppName "Stock Tracker"
  ; Keep this GUID fixed forever: it is how Windows recognises upgrades of the same app.
  #define AppGuid "{{09AC96D6-3F59-4D47-BCE8-143E43C6101A}"
  #define OutputName "StockTracker-Setup-" + AppVersion
#endif

[Setup]
AppId={#AppGuid}
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
OutputBaseFilename={#OutputName}
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
; The in-app updater runs this installer silently with /RELAUNCH=1, so the app reopens by itself.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchRequested

[Code]
// True when the installer was started by the in-app updater (/RELAUNCH=1).
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

// Uninstalling removes the program but keeps the stock database and the encrypted Firebase
// credentials in %LOCALAPPDATA%\StockTracker, so a reinstall or upgrade never loses data.
// Only an interactive uninstall may offer to delete them, and it defaults to NO. A silent
// uninstall (scripts, tests, IT tools) never asks and never deletes anything: a plain MsgBox
// is NOT suppressed by /SUPPRESSMSGBOXES, so without this guard a stray click could wipe data.
// Test builds compile the deletion out entirely.
#ifndef TestBuild
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    DataDir := ExpandConstant('{localappdata}\StockTracker');
    if DirExists(DataDir) then
      if SuppressibleMsgBox('Also delete your stock data and saved Firebase credentials?' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                'This cannot be undone. Choose No to keep them (recommended if you plan to reinstall).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
#endif
