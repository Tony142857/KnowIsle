# 知屿 KnowIsle · 智能课程知识社区

> 以学生社区为驱动、以结构化 RAG-AI 为学习引擎的大学课程知识平台。
> 详细设计见《知屿-智能课程知识社区-项目文档.md》（v3.2.4）。
> 进度跟踪见 [docs/开发进度与目标.md](docs/开发进度与目标.md)（每迭代更新）。

技术栈：FastAPI + Jinja2/HTMX/Tailwind · PostgreSQL 16 · Redis 7 · SeaweedFS（S3） · Chroma · ARQ · Docker Compose

## 当前状态

**v0.3 已完成**（2026-09-19）：个人库 RAG 全链路端到端可用（M1 里程碑达成）——文档上传（异步解析）✅ · 四格式解析 + 语义切块 ✅ · 本地 bge-small-zh-v1.5 向量化（模型烘焙进镜像，运行时离线）✅ · Chroma 双 Collection + BM25/语义 RRF 融合检索 ✅ · SSE 流式问答 + 块级引用溯源 ✅ · AI 日额度与 qa_logs 落库 ✅ · 个人库页 / AI 对话页 / 原文溯源页 ✅
**v0.3 回归验证通过**（2026-09-20）：容器内 ruff + pytest 46 例全绿，v0.1~v0.3 共 60+ 项 E2E 检查（真实 DeepSeek 模型）全过；修复章节树越权可见、.doc/.ppt 假支持、失败文档无法重传、提示词引用示例格式不符等 6 处问题（详见进度文档）。
（此前 v0.2：邮箱降级认证 · Redis Session + JWT 双通道 · RBAC 四角色门控 · 专业名单导入 · 课程空间结构）
进度详情见 [docs/开发进度与目标.md](docs/开发进度与目标.md)。

## 快速开始（开发 / 演示环境）

前置：已安装并启动 Docker Desktop（Windows 11 + WSL2）。

```bash
# 1. 准备环境变量（首次）
cp .env.example .env   # 然后编辑 .env，至少填入 SECRET_KEY 与 OFFICIAL_LLM_API_KEY

# 2. 一键构建并拉起全部服务（app / worker / postgres / redis / seaweedfs / chroma / nginx）
docker compose -f deploy/docker-compose.yml up -d --build

# 3. 验证
curl http://localhost:8080/healthz        # {"status":"ok"}
# 浏览器访问 http://localhost:8080         # 首页
# SeaweedFS Filer 界面 http://localhost:8888  # 对象存储文件浏览（调试）

# 4. 初始化（首次）：创建管理员 + 导入专业名单（v0.2 起）
docker compose -f deploy/docker-compose.yml exec app python scripts/create_admin.py \
  --student-no 20230001 --nickname 管理员 --email admin@stu.example.edu.cn
docker compose -f deploy/docker-compose.yml exec app python scripts/init_majors.py scripts/majors.example.csv

# 5. 登录：浏览器打开 http://localhost:8080/login ，输入学号 + 上一步绑定的邮箱；
#    开发期为假通道，验证码直接显示在页面上（EMAIL_CODE_ECHO=true），也可在
#    `docker compose -f deploy/docker-compose.yml logs -f app` 中查看

# 6. 个人库体验（v0.3 起）：登录后进入 http://localhost:8080/library
#    新建学期/课程 → 上传课件（pdf/pptx/docx/md）→ 解析完成后点「AI 问答」
#    流式提问，回答中的引用可点击跳转原文溯源页高亮对应文本块

# 7. 停止 / 清理
docker compose -f deploy/docker-compose.yml down          # 停止
docker compose -f deploy/docker-compose.yml down -v       # 停止并删除数据卷（慎用）
```

## 目录结构

```
app/            FastAPI 应用（api/ web/ core/ community/ identity/ moderation/ workers/ storage/）
client/         桌面客户端薄壳（pywebview + NSIS 安装包）
alembic/        数据库迁移
deploy/         docker-compose.yml / nginx.conf / backup.sh / seaweedfs-s3.json
docs/           开发进度与目标.md（进度跟踪，每迭代更新）
scripts/        init_majors.py / create_admin.py / seed_demo.py
tests/          pytest（unit / integration / e2e）
```

## 开发约定

- Git 工作流：`main` 保护分支 + `feature/xxx` + PR（≥1 人 review，CI 通过才可合并）
- Commit 规范：Conventional Commits（`feat:` / `fix:` / `refactor:` / `test:` / `docs:`）
- 本地检查：`pip install -r requirements-dev.txt && ruff check app tests scripts && pytest -q`
- 依赖分层：`requirements.txt` 为应用与 CI 共用；`requirements-ml.txt`（torch CPU + sentence-transformers）仅由 Dockerfile 装入镜像，应用代码对其一律惰性导入
- 镜像版本全部钉死：自建镜像按版本号命名（`knowisle-app:0.3.0`，发版时手动递增，禁止 latest）；五个第三方镜像与 Dockerfile 基础镜像均以 `@sha256` digest 固定，团队构建逐字节一致
- Embedding 模型 bge-small-zh-v1.5 在镜像构建时预下载（经 hf-mirror），运行时 `HF_HUB_OFFLINE=1` 离线加载
- 数据库迁移：`alembic revision --autogenerate -m "..."`，迁移随 app 容器启动自动执行

## 许可

MIT
