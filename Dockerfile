# syntax=docker/dockerfile:1
# 知屿应用镜像：app（Gunicorn+Uvicorn）与 worker（ARQ）共用同一镜像，仅启动命令不同（§15.2）
# 基础镜像按 digest 钉死（= 当前已验证的 linux/amd64 python:3.12-slim 内容），保证团队构建逐字节一致
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

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

COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt \
 && pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2" \
 && pip install -r requirements-ml.txt

# 预下载本地 Embedding 模型 bge-small-zh-v1.5（构建期经 hf-mirror 镜像站拉取，
# 烘焙进镜像后运行时可完全离线加载，见 compose 中 HF_HUB_OFFLINE=1）
RUN HF_ENDPOINT=https://hf-mirror.com python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"

COPY . .

EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000"]
