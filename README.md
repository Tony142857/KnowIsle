# 知屿 KnowIsle · 智能课程知识社区

> 以学生社区为驱动、以结构化 RAG-AI 为学习引擎的大学课程知识平台。
> 详细设计见《知屿-智能课程知识社区-项目文档.md》（v3.2.8）。
> 进度跟踪见 [docs/开发进度与目标.md](docs/开发进度与目标.md)（每迭代更新）。

技术栈：FastAPI + Jinja2/HTMX/Tailwind · PostgreSQL 16 · Redis 7 · SeaweedFS（S3） · Chroma · ARQ · Docker Compose

## 当前状态

**v0.7 已完成**（2026-09-30）：经验长廊（结构化模板发帖：背景/时间线/经验要点/避坑提示，发帖异步生成 AI 摘要含轮询展示，标签云 + 届别过滤 + 精华区，AI 摘要折叠卡；精华标记：admin/builder 可标记、防自改 422、作者 +30 每帖一次、取消不回扣、通知 + 审计）✅ · 资料求援（发帖托管悬赏分 1~1000 校验/贡献分不足 422，评论即响应并通知帖主，采纳响应评论结算赏金 bounty_escrow→bounty_award、自响应不结算、重复采纳幂等，未采纳不结算）✅ · 板块页四 Tab 全部开放，`posts.ai_summary` 新列（迁移 9cbb7dcfd2f3）✅ · 容器内 ruff + pytest 158 例全绿、40 项真实 DeepSeek E2E 断言全过
（此前 v0.6：收藏/关注（资源/帖子收藏含 fav_count 同步、课程/用户关注，幂等开关语义）· 订阅类通知（终审上架自动通知课程关注者 new_resource）· 管理后台完整版 `/admin`（用户治理：角色任命/信用裁决限幅 0~100 + credit_logs 留痕；空间管理：专业导入/课程审批停用；平台配置：新增第 26 表 platform_config，AI 日额度/兑换汇率/协审时限在线调整 + 审计）· 个人中心新增「我的收藏/我的关注」卡片、课程页关注按钮、资源/帖子页收藏按钮）
（此前 v0.5：问答贴 + AI 首答（异步生成、带引用溯源）· 评论/点赞/采纳（自问自答不计分）· 成长体系完整版（等级升级 + Redis 实时贡献榜 + 每日对账 + 下载积分 80% 上传者分成）· AI 额度贡献分兑换 + 用户自定义 Key（Fernet 加密、qa_logs 双通道溯源）· 协审 48h 超时自动重指派 + 管理员改派/直审 · 公共资源克隆进个人库）
（此前 v0.4：公共库投稿→三级审核→上架全流程 · 自动预检（格式/MD5 查重/敏感词/AI 初评）· 资源预览/下载/评分 · 章节摘要回填启用粗召回）
（此前 v0.3：个人库 RAG 全链路端到端可用（M1 里程碑）——四格式解析 + 语义切块 · 本地 bge 向量化（模型烘焙进镜像）· 双库 RRF 融合检索 · SSE 流式问答 + 引用溯源 · 个人库三页面 · AI 日额度）
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

# 7. 公共库体验（v0.4 起）：个人库文档点「投稿到公共库」→ reviewer 账号到
#    http://localhost:8080/review 协审 → admin 到 http://localhost:8080/admin/review 终审
#    → 课程空间「资料库」Tab 查看 / 在线预览 / 下载 / 评分
#    注：reviewer 角色由管理员在管理后台「用户治理」任命（v0.6 起）：
#    http://localhost:8080/admin → 用户治理 → 角色任命

# 8. 社区体验（v0.5 起）：http://localhost:8080/boards/qa 发问答贴 → AI 首答自动生成
#    （带引用溯源）→ 同学评论/点赞 → 帖主采纳；资源详情页可「克隆到个人库」；
#    个人中心 http://localhost:8080/me ：成长看板 / AI 额度兑换 / 自定义 Key / 通知

# 9. 社区闭环体验（v0.6 起）：课程空间页「关注课程」→ 该课程新资料上架时收到
#    站内通知（new_resource）；资源/帖子详情页可收藏；个人中心「我的收藏/我的关注」
#    管理；管理员后台 http://localhost:8080/admin ：用户治理（角色任命/信用裁决）、
#    空间管理（专业导入/课程审批）、平台配置（AI 日额度/兑换汇率/协审时限在线调整）

# 10. 经验长廊与资料求援（v0.7 起）：http://localhost:8080/boards/experience 按结构化模板
#    （背景/时间线/经验要点/避坑提示）发帖，AI 摘要自动生成（真实 DeepSeek，列表折叠卡/
#    详情卡片展示）；管理员/共建者可将经验帖标记为精华（作者 +30 分）；
#    http://localhost:8080/boards/bounty 发布求资料需求并悬赏贡献分，他人评论响应、
#    帖主采纳后赏金自动转给响应者（双方收站内通知）

# 11. 停止 / 清理
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
- 镜像版本全部钉死：自建镜像按版本号命名（`knowisle-app:0.7.0`，发版时手动递增，禁止 latest）；五个第三方镜像与 Dockerfile 基础镜像均以 `@sha256` digest 固定，团队构建逐字节一致
- Embedding 模型 bge-small-zh-v1.5 在镜像构建时预下载（经 hf-mirror），运行时 `HF_HUB_OFFLINE=1` 离线加载
- 数据库迁移：`alembic revision --autogenerate -m "..."`，迁移随 app 容器启动自动执行

## 许可

MIT
