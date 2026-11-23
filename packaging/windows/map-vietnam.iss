; Inno Setup 6 script: the Windows installer for Map Vietnam.
;
;   iscc /DAppVersion=0.1.0 packaging\windows\map-vietnam.iss
;
; run from the repository root after PyInstaller has built dist\Map Vietnam\. The version is
; [project].version in pyproject.toml, passed in on the command line (the release workflow reads it
; from there), so it is written in one place only. Output: dist\installer\Map-Vietnam-<version>-Setup.exe.
;
; Per-user install: no administrator rights, no UAC prompt. The app goes to
; %LOCALAPPDATA%\Programs\Map Vietnam; what the person downloads and adds (maps, records) is kept
; apart in %LOCALAPPDATA%\Map Vietnam (see scripts/paths.py), so updating or reinstalling never
; touches it, and uninstalling asks before deleting it.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=0.1.0 packaging\windows\map-vietnam.iss
#endif

#define AppName "Map Vietnam"
#define AppExe "Map Vietnam.exe"

[Setup]
; A fixed id, so a newer installer upgrades the installed copy instead of installing beside it.
AppId={{6F0B5D44-3C0E-4B63-9A7F-2D4E8B1C7A55}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Map Vietnam contributors
AppPublisherURL=https://github.com/CaullenOmdahl/map-vietnam
AppSupportURL=https://github.com/CaullenOmdahl/map-vietnam/blob/main/docs/troubleshooting.md
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\icons\map-vietnam.ico
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist\installer
OutputBaseFilename=Map-Vietnam-{#AppVersion}-Setup
Compression=lzma2/normal
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; Close the running app instead of failing on its files when updating.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]
// What the person added lives outside the install folder. Removing the program keeps it unless
// they say otherwise: it can be several gigabytes, and it took a long time to download.
function UserDataDir: String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(UserDataDir) then
  begin
    if UninstallSilent then
      Exit; // a silent uninstall keeps the data
    if MsgBox('Also delete your maps and records?' + #13#10 + #13#10 +
              'They are kept in ' + UserDataDir + ' and can take up several gigabytes. ' +
              'Choose No to keep them for the next time you install Map Vietnam.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(UserDataDir, True, True, True);
  end;
end;
