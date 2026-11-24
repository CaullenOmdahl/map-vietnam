; NSIS script: the Windows installer for Map Vietnam.
;
;   makensis -DVERSION=0.1.0 -DVIVERSION=0.1.0.0 packaging/windows/map-vietnam.nsi
;
; run after PyInstaller has built dist/Map Vietnam/ (packaging/build.sh does both). makensis runs on
; Linux, macOS and Windows alike, so the installer is built natively wherever the app is. The version
; is [project].version in pyproject.toml, passed in on the command line, so it is written in one
; place only (VIVERSION is the same number padded to four parts, which Windows' file properties
; need). Output: dist/installer/Map-Vietnam-<version>-Setup.exe.
;
; Per-user install: no administrator rights, no UAC prompt. The app goes to
; %LOCALAPPDATA%\Programs\Map Vietnam; what the person downloads and adds (maps, records) is kept
; apart in %LOCALAPPDATA%\Map Vietnam (see scripts/paths.py), so updating or reinstalling never
; touches it, and uninstalling asks before deleting it.
;
; Silent install, for scripts: Setup.exe /S [/D=C:\some\folder]  (/D must come last, unquoted).

!ifndef VERSION
  !error "Pass the version: makensis -DVERSION=0.1.0 -DVIVERSION=0.1.0.0 packaging/windows/map-vietnam.nsi"
!endif
!ifndef VIVERSION
  !error "Pass the version: makensis -DVERSION=0.1.0 -DVIVERSION=0.1.0.0 packaging/windows/map-vietnam.nsi"
!endif

!define APP_NAME "Map Vietnam"
!define APP_EXE "Map Vietnam.exe"
!define APP_URL "https://github.com/CaullenOmdahl/map-vietnam"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\MapVietnam"

Unicode true
ManifestDPIAware true
RequestExecutionLevel user
SetCompressor /SOLID lzma

Name "${APP_NAME}"
OutFile "../../dist/installer/Map-Vietnam-${VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
; A newer installer finds the installed copy and replaces it instead of installing beside it.
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
BrandingText "${APP_NAME} ${VERSION}"

VIProductVersion "${VIVERSION}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "CompanyName" "Map Vietnam contributors"
VIAddVersionKey "LegalCopyright" "Apache License 2.0"

!include "MUI2.nsh"

!define MUI_ICON "../icons/map-vietnam.ico"
!define MUI_UNICON "../icons/map-vietnam.ico"
!define MUI_ABORTWARNING

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APP_NAME} now"
; The finish page's second checkbox is the usual place for "add a desktop shortcut".
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Put a shortcut on the desktop"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION DesktopShortcut

!insertmacro MUI_PAGE_LICENSE "../../LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function DesktopShortcut
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
FunctionEnd

Section "Install"
  SetShellVarContext current

  ; A running copy holds its program open, and Windows refuses to open it for writing. Ask the
  ; person to quit it rather than half-replacing its files.
  close_check:
  IfFileExists "$INSTDIR\${APP_EXE}" 0 copy_files
  ClearErrors
  FileOpen $0 "$INSTDIR\${APP_EXE}" a
  IfErrors 0 not_running
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${APP_NAME} is open. Quit it (its Quit button, or close its window), then choose Retry." /SD IDCANCEL IDRETRY close_check
    Abort "${APP_NAME} is still open."
  not_running:
  FileClose $0
  ; The previous version's program files go, so no stale file from it is left behind.
  RMDir /r "$INSTDIR\_internal"

  copy_files:
  SetOutPath "$INSTDIR"
  File /r "../../dist/${APP_NAME}/*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Map Vietnam contributors"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "${APP_URL}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "HelpLink" "${APP_URL}/blob/main/docs/troubleshooting.md"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current

  ; Only what the installer put there, never the whole folder: the person may have chosen a
  ; folder that holds other things too.
  Delete "$INSTDIR\${APP_EXE}"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"

  ; What the person added lives outside the install folder. Removing the program keeps it unless
  ; they say otherwise: it can be several gigabytes, and it took a long time to download. A silent
  ; uninstall keeps it.
  IfFileExists "$LOCALAPPDATA\${APP_NAME}\*.*" 0 done
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "Also delete your maps and records?$\r$\n$\r$\nThey are kept in $LOCALAPPDATA\${APP_NAME} and can take up several gigabytes. Choose No to keep them for the next time you install ${APP_NAME}." \
    /SD IDNO IDNO done
  RMDir /r "$LOCALAPPDATA\${APP_NAME}"
  done:
SectionEnd
