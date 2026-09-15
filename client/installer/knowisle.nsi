; 知屿 KnowIsle NSIS 安装包脚本（§15.2.1，占位骨架，v0.9 完善）
; 用法：makensis client\installer\knowisle.nsi
; 前置：PyInstaller 已产出 client\dist\知屿\

!define APP_NAME "知屿"
!define APP_VERSION "0.1.0"
!define INSTALL_DIR "$PROGRAMFILES64\${APP_NAME}"

Name "${APP_NAME}"
OutFile "knowisle-setup-${APP_VERSION}.exe"
InstallDir "${INSTALL_DIR}"

Section "Install"
  SetOutPath "$INSTDIR"
  ; TODO(v0.9): File /r "..\dist\知屿\*.*"
  ; TODO(v0.9): 开始菜单入口、卸载程序、WebView2 Runtime 检测与联网安装引导
SectionEnd
