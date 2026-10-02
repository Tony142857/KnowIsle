# 知屿桌面客户端（pywebview 薄壳）

壳只负责窗口与加载服务端页面，业务逻辑全在服务端（§6.4 / §15.2.1）。
`settings.json` 配置服务端地址与窗口尺寸，打包后仍可手工编辑（与 exe 同级）。

## 服务端契约（v1.0，以此为准）

| 项 | 值 |
| --- | --- |
| 演示入口（`server_url` 默认值） | `http://localhost:8080`（nginx → app:8000） |
| 健康检查 | `GET /healthz` → `{"status":"ok"}`（判活用；客户端壳自身不做健康检查） |
| 调试直连 | `http://127.0.0.1:8000`（不经 nginx，仅本机） |
| 登录页 | `/login` |
| AI 问答页 | `/library/chat` |
| 渲染依赖 | Windows 上 pywebview 默认使用 Edge WebView2 Runtime（缺失见文末 FAQ） |

## 本地运行（开发）

```bash
pip install pywebview
python client/main.py
```

## 一键打包（v1.0，在授权打包机上执行，全程联网）

### 1. 环境前置（打包机一次性准备）

- Python 3.12（与服务端镜像 `python:3.12` 一致；3.10+ 亦可）
- NSIS 3.x：<https://nsis.sourceforge.io/Download>（安装后确认 `makensis` 在 PATH 中，或下文用全路径调用）

```bash
pip install pywebview pyinstaller
```

### 2. 产出 one-folder 应用目录（项目根目录执行）

```bash
pyinstaller --noconfirm --distpath client/dist --workpath client/build client/zhiyu.spec
```

产物：`client/dist/知屿/`（内含 `知屿.exe` 与同级的 `settings.json`）。

自检（需先启动服务端）：直接双击 `client/dist/知屿/知屿.exe`，应打开窗口并加载 `http://localhost:8080` 的登录页。

### 3. 产出 NSIS 安装包

```bat
makensis client\installer\knowisle.nsi
```

产物：`client/installer/knowisle-setup-1.0.0.exe`
（脚本内路径一律相对脚本自身目录 `${__FILEDIR__}` 解析，任意工作目录执行均可。）

安装包行为：复制应用到 `$PROGRAMFILES64\知屿`、创建开始菜单/桌面快捷方式与卸载程序、
注册「应用与功能」条目、检测 WebView2 Runtime（缺失时引导打开微软官方下载页，不内置下载器）。

## 答辩机验证清单

1. 服务端就绪：在 `deploy/` 下 `docker compose up -d`，然后
   `curl http://localhost:8080/healthz` 返回 `{"status":"ok"}`。
2. 运行 `knowisle-setup-1.0.0.exe` 完成安装，开始菜单/桌面出现「知屿」。
3. 启动「知屿」→ 打开登录页 `/login`，用演示账号登录成功。
4. 进入 AI 问答页 `/library/chat`，发起一次提问并得到响应。
5.（可选）重启答辩机后再启动一次客户端，确认无环境残留问题。

## 常见问题（FAQ）

- **窗口白屏 / 提示 WebView2 缺失**：安装包已自动检测并引导打开微软官方下载页
  （<https://go.microsoft.com/fwlink/p/?LinkId=2124703>）；也可手动搜索安装
  “Microsoft Edge WebView2 Runtime 常青版引导程序”，装完重开客户端即可。
- **壳异常（崩溃/白屏）时的兜底**：直接用浏览器访问 `http://localhost:8080`，
  业务全在服务端，演示功能无差异，不中断答辩。
- **8080 端口被占用**：改 `deploy/docker-compose.yml` 中 nginx 的 `8080:80` 映射，
  并同步修改 `settings.json` 的 `server_url`（打包后是 exe 同级的那份）。
- **想绕过 nginx 排障**：把 `settings.json` 的 `server_url` 临时改为
  `http://127.0.0.1:8000`（app 直连调试口，仅本机），定位问题后改回 8080。
