; 知屿 KnowIsle NSIS 安装包脚本（§15.2.1，v1.0 对齐服务端契约）
; 用法（Windows，已安装 NSIS 3.x，任意工作目录均可）：makensis client\installer\knowisle.nsi
; 前置：PyInstaller 已产出 client\dist\知屿\（见 client/README.md）
; 注：文件路径一律相对脚本自身目录（${__FILEDIR__}）解析，不受 makensis 工作目录影响

!include "LogicLib.nsh"  ; ${If} 等宏

!define APP_NAME "知屿"
!define APP_VERSION "1.0.0"
!define APP_PUBLISHER "KnowIsle Team"
!define INSTALL_DIR "$PROGRAMFILES64\${APP_NAME}"
; WebView2 Runtime 在 EdgeUpdate 客户端注册表项下登记（微软官方探测方式）
!define WEBVIEW2_REG "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define WEBVIEW2_BOOTSTRAPPER "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

Name "${APP_NAME}"
OutFile "${__FILEDIR__}\knowisle-setup-${APP_VERSION}.exe"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "${__FILEDIR__}\..\dist\知屿\*.*"

  ; 开始菜单与桌面快捷方式
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\知屿.exe"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\知屿.exe"

  ; 卸载程序 + 注册表登记（控制面板「应用与功能」可见）
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$INSTDIR\uninstall.exe"

  ; WebView2 Runtime 检测：缺失时引导联网安装（不内置下载器，避免安装包体积膨胀）
  Call CheckWebView2
  Pop $0
  ${If} $0 == "missing"
    MessageBox MB_YESNO|MB_ICONEXCLAMATION \
      "未检测到 Microsoft Edge WebView2 Runtime，客户端窗口将无法渲染。$\r$\n是否现在打开官方下载页面安装？（也可稍后自行安装）" \
      IDNO +2
    ExecShell "open" "${WEBVIEW2_BOOTSTRAPPER}"
  ${EndIf}
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  DeleteRegKey HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd

; 返回值经栈传出：present / missing
Function CheckWebView2
  ReadRegStr $0 HKLM "${WEBVIEW2_REG}" "pv"
  StrCmp $0 "" 0 found
  ReadRegStr $0 HKCU "${WEBVIEW2_REG}" "pv"
  StrCmp $0 "" missing found
found:
  Push "present"
  Return
missing:
  Push "missing"
FunctionEnd
