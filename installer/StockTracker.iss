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
  #define RunValue "StockTrackerE2ETest"
#else
  #define AppName "Stock Tracker"
  ; Keep this GUID fixed forever: it is how Windows recognises upgrades of the same app.
  #define AppGuid "{{09AC96D6-3F59-4D47-BCE8-143E43C6101A}"
  #define OutputName "StockTracker-Setup-" + AppVersion
  #define RunValue "StockTracker"
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
; Off by default. The app's Settings window shows and changes the same setting.
Name: "startup"; Description: "Start Stock Tracker in the background when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#RunValue}";   ValueData: """{app}\{#AppExe}"" --background"; Tasks: startup; Flags: uninsdeletevalue

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
// The app keeps running in the tray after its window is closed, so it has to be asked to exit
// before its files can be replaced or removed. It listens for this named event (see
// src\single_instance.py). Older versions don't create it: OpenEvent then returns 0 and nothing happens.
function OpenEventW(dwDesiredAccess: Cardinal; bInheritHandle: Boolean; lpName: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: THandle): Boolean;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

procedure AskRunningAppToQuit;
var
  Event: THandle;
begin
  Event := OpenEventW($0002, False, 'Local\StockTrackerSystem.Quit');   // EVENT_MODIFY_STATE
  if Event <> 0 then
  begin
    SetEvent(Event);
    CloseHandle(Event);
    Sleep(2000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  AskRunningAppToQuit;
  Result := '';
end;

function InitializeUninstall: Boolean;
begin
  AskRunningAppToQuit;
  Result := True;
end;

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
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  // The app can add its own start-with-Windows entry from Settings; never leave it pointing at a deleted file.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#RunValue}');
#ifndef TestBuild
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
#endif
end;
