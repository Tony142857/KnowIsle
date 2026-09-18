# 知屿 KnowIsle · 智能课程知识社区

> 以学生社区为驱动、以结构化 RAG-AI 为学习引擎的大学课程知识平台。
> 详细设计见《知屿-智能课程知识社区-项目文档.md》（v3.2.1）。
> 进度跟踪见 [docs/开发进度与目标.md](docs/开发进度与目标.md)（每迭代更新）。

技术栈：FastAPI + Jinja2/HTMX/Tailwind · PostgreSQL 16 · Redis 7 · SeaweedFS（S3） · Chroma · ARQ · Docker Compose

## 当前状态

**v0.2 已完成**（2026-09-18）：邮箱降级认证（验证码 + 首次登录建档补全）✅ · Redis Session + JWT 双通道 ✅ · RBAC 四角色门控（越权 404）✅ · 专业名单导入（脚本 + 管理接口）✅ · 课程空间（创建/申请/审批 + 章节树）✅ · 登录页 / 导航栏登录态 / 专业与课程卡片页 / 课程空间页 ✅
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

# 6. 停止 / 清理
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
- 数据库迁移：`alembic revision --autogenerate -m "..."`，迁移随 app 容器启动自动执行

## 许可

MIT
