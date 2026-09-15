# syntax=docker/dockerfile:1
# 知屿应用镜像：app（Gunicorn+Uvicorn）与 worker（ARQ）共用同一镜像，仅启动命令不同（§15.2）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/knowisle

WORKDIR /srv/knowisle

# LibreOffice：Office 文档异步转 PDF 预览（workers/parse_worker，§7.1）
# fonts-noto-cjk：转换中文文档时避免缺字
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    curl \
    libreoffice-impress \
    libreoffice-writer \
    fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
# 其他机器全新构建如嫌 apt 慢，可在上面 RUN 前加一行切换 Debian 镜像源：
# RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# pip 使用清华镜像加速（仅改下载源，不改变依赖内容）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000"]
