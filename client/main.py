"""知屿桌面客户端（pywebview 联网薄壳，§6.4 / §15.2.1）。

壳只负责窗口与加载服务端页面，业务逻辑全在服务端。
打包（v0.9 起）：pyinstaller --noconfirm client/zhiyu.spec
"""

import json
import sys
from pathlib import Path

import webview

# 冻结（PyInstaller one-folder）时 settings.json 与 exe 同级；源码运行时与 main.py 同级
_BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
SETTINGS_PATH = _BASE_DIR / "settings.json"


def main() -> None:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    webview.create_window(
        "知屿 KnowIsle",
        settings["server_url"],
        width=settings.get("width", 1280),
        height=settings.get("height", 800),
        min_size=(960, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
