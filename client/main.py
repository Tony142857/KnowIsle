"""知屿桌面客户端（pywebview 联网薄壳，§6.4 / §15.2.1）。

壳只负责窗口与加载服务端页面，业务逻辑全在服务端。
打包：pyinstaller --noconfirm --windowed --name 知屿 client/main.py
"""

import json
from pathlib import Path

import webview

SETTINGS_PATH = Path(__file__).parent / "settings.json"


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
