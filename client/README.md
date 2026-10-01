# 知屿桌面客户端（pywebview 薄壳）

壳只负责窗口与加载服务端页面，业务逻辑全在服务端（§6.4 / §15.2.1）。
`settings.json` 配置服务端地址与窗口尺寸，打包后仍可手工编辑（与 exe 同级）。

## 本地运行（开发）

```bash
pip install pywebview
python client/main.py
```

## 打包 exe 与 NSIS 安装包（v0.9 就绪，答辩前在打包机上执行）

前置（打包机一次性准备，联网）：

```bash
pip install pyinstaller pywebview
# 安装 NSIS：https://nsis.sourceforge.io/Download
```

步骤（项目根目录执行）：

```bash
# 1. 产出 one-folder 应用目录 client/dist/知屿/
pyinstaller --noconfirm client/zhiyu.spec

# 2. 产出安装包 client/installer/knowisle-setup-0.9.0.exe
makensis client\installer\knowisle.nsi
```

安装包行为：复制应用到 `$PROGRAMFILES64\知屿`、创建开始菜单/桌面快捷方式与卸载程序、
注册「应用与功能」条目、检测 WebView2 Runtime（缺失时引导打开微软官方下载页，不内置下载器）。

## 答辩演示兜底

- 打包/壳异常时直接用浏览器访问同一服务端地址（`settings.json` 的 `server_url`），演示不中断。
- 答辩用机提前安装验证一遍安装包（§21 演示检查清单）。
