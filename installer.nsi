; keyhive-proxy NSIS installer script
; Build: makensis /DVERSION=v0.1.0 installer.nsi

!ifndef VERSION
  !define VERSION "v0.0.0"
!endif

!define APPNAME "keyhive-proxy"
!define APPDIR "$LOCALAPPDATA\keyhive-proxy"

Name "keyhive-proxy ${VERSION}"
OutFile "keyhive-proxy-setup-${VERSION}.exe"
InstallDir "${APPDIR}"
RequestExecutionLevel user
SetCompressor lzma

Page directory
Page instfiles

Section "Install"
  SetOutPath "$INSTDIR"
  File "dist\keyhive-proxy.exe"

  ; Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\keyhive-proxy"
  CreateShortcut "$SMPROGRAMS\keyhive-proxy\keyhive-proxy.lnk" "$INSTDIR\keyhive-proxy.exe"

  ; Uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Add/Remove Programs entry
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\keyhive-proxy" \
    "DisplayName" "keyhive-proxy ${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\keyhive-proxy" \
    "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\keyhive-proxy" \
    "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\keyhive-proxy" \
    "Publisher" "KeyHive Garden"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\keyhive-proxy.exe"
  Delete "$INSTDIR\uninstall.exe"
  Delete "$SMPROGRAMS\keyhive-proxy\keyhive-proxy.lnk"
  RMDir "$SMPROGRAMS\keyhive-proxy"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\keyhive-proxy"
SectionEnd
