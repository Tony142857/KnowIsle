# 知屿桌面客户端 PyInstaller 打包规格（§15.2.1，v0.9 就绪）。
# 用法（项目根目录，需先 pip install pyinstaller pywebview）：
#   pyinstaller --noconfirm client/zhiyu.spec
# 产物：client/dist/知屿/（one-folder 模式，供 NSIS 打包）

from pathlib import Path

CLIENT_DIR = Path(SPECPATH)  # noqa: F821  client/ 目录

a = Analysis(
    [str(CLIENT_DIR / "main.py")],
    pathex=[str(CLIENT_DIR)],
    binaries=[],
    datas=[(str(CLIENT_DIR / "settings.json"), ".")],
    hiddenimports=["webview"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "sentence_transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="知屿",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # --windowed：无控制台窗口
    icon=str(CLIENT_DIR / "assets" / "icon.ico") if (CLIENT_DIR / "assets" / "icon.ico").exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="知屿",
)
