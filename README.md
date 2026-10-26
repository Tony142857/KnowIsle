# 知屿 KnowIsle · 智能课程知识社区

> 以学生社区为驱动、以结构化 RAG-AI 为学习引擎的大学课程知识平台。
> 详细设计见《知屿-智能课程知识社区-项目文档.md》（v3.11.0）。
> 进度跟踪见 [docs/开发进度与目标.md](docs/开发进度与目标.md)（每迭代更新）。

技术栈：FastAPI + Jinja2/Alpine.js/HTMX/Tailwind · PostgreSQL 16 · Redis 7 · SeaweedFS（S3） · Chroma · ARQ · Docker Compose

## 当前状态

**v1.4 现代知识平台前端全面升级**（2026-10-26，feature 分支，前端与质量门槛为主，业务 API 与 RAG 算法零变更）：**语义设计令牌 v4**（surface/text/border/status 与 CSS 变量统一映射，全站 slate/red 等色板工具类清零，正文/辅助文字/焦点达到 WCAG AA，翡翠绿 favicon）· **全站页面骨架**（19 个页面统一页头/内容宽度/Tab/筛选/空错态，资源/帖子/板块/搜索接入共享页头）· **组件体系与去重**（`_components.html` 8 个宏 + `render.js` 模板克隆渲染，课程卡/章节节点/评论节点/个人库课程卡消灭 JS 字符串 DOM，管理后台改为同一份响应式数据结构）· **内容优先排版**（display/title/card-title/body/caption/num/code 字级，46rem 阅读宽度、数字表格对齐、代码与长 URL 防溢出）· **Motion One 10.18.0 本地按需动效**（150~250ms，页头/卡片/Tab/弹窗/引用高亮，reduced-motion 全降级）· **按需加载与无障碍**（marked/DOMPurify/highlight 仅 5 个 Markdown 页面加载、ECharts 仅运营看板 Tab 动态加载、44px 目标、360~1440px、200% 缩放、forced-colors、键盘 Tab、axe 无 critical/serious）· **容器化浏览器质量门槛**（Playwright + axe-core 覆盖 19 页面 × 5 视口（匿名/学生/协审/共建/管理员 5 类角色），控制台/外联/CSP/截图）· 修复草稿双实现、聊天异常引用空数组、新课程共享卡片初始化、个人中心未配置 Key 空值、首次问答额度预检唯一键锁，以及 Gunicorn SSE 默认 30 秒超时问题；验收 Ruff 全绿 + pytest **644 passed + 1 skipped**（覆盖率 86%）+ Node 前端契约 12/12 + 浏览器测试 3/3；镜像 knowisle-app:1.4.0；git tag v1.4
**v1.3 交互可用性与评测驱动 RAG 优化**（2026-10-25，feature 分支）：**全站交互可达性**（统一 idle/loading/success/empty/error 与 `aria-busy`/`role=alert`、可关闭 toast、跳正文与 `aria-current`；登录/发码原生 form + 安全站内 `next` 回跳；课程资源搜索分页与 URL 可恢复 Tab/章节、付费下载余额确认、原文返回/块内搜索；个人库拖拽上传 + 真实进度 + 轮询恢复 + 失败重传 + 投稿阶段/驳回理由；问答 SSE 停止/重试/保留问题/额度范围、复习防旧响应与证据跳转；发帖 Markdown 预览/计数/课程预选/`ZY.drafts` 本地草稿与离开保护、QA 改采/自响应禁采纳、资源/帖子/评论举报入口、通知锚点；个人中心中文原因与加载更多；协审 409 退避/裁决确认/改派保留、终审懒加载与管理 Tab URL 化）· **后端配套**（Office 投稿阶段预览 + 幂等重试、终审/直审副作用可恢复、审核/社交行锁与唯一冲突幂等、`documents.parse_error` 安全文案、个人库审核上下文、经验帖背景服务端校验、AI 首答/摘要重试、检索 trace）· **评测驱动 RAG**（72 条黄金集 3 课程/4 格式/三 scope/tune-frozen；`章节 > 小节 > 正文` 进入 embedding/Prompt、`TARGET_MIN` 生效、长文二级切分与硬上限；粗召回全局补充与可解释 fallback、单路降级；unigram/bigram 对照 + RRF 网格；低词面覆盖语义证据更高相似度门槛、无答案拒答、AI 首答证据快照、复习本章约束与习题校验；`retrieval_metrics` 观测与 Bad Case 分类）· 迁移⑨（解析错误/AI 证据/检索指标，正反向验证）· 容器内 Ruff 全绿 + pytest **630 passed**（覆盖率 86%）+ Node 前端契约 12 项；真实 PG 并发集成与 Chroma 全量重建（23 文档/82 chunks/58+24 向量）通过；离线评测 frozen Recall@5 0.8125、nDCG@5 0.811（基线 0.542/0.496），不可回答拒答率 1.0、权限泄漏 0；镜像 knowisle-app:1.3.0；git tag v1.3
**v1.2 前端视觉精致化**（2026-10-11，feature 分支，纯模板/静态资源层、不动后端）：**设计体系 v3**（设计令牌 v3——text-title/text-card-title 字级阶梯 + gap-section 间距令牌；main.css 新增 .card-tinted 渐变重点卡 / .card-accent-left 精华边条 / .btn 按下压缩 / .btn-loading 内置 spinner / .d-80·150·300 延迟工具类）· **图标与插画体系**（`_icons.html` 图标字典宏 64 枚集中管理，▲▼★☆✓ Unicode 图标全站清零、投票/收藏/评分改 SVG pill；`_illustrations.html` 四个岛屿系空状态插画；`_page_header.html` 页头宏替换全站 9 处复制粘贴页头）· **全站 24 模板逐页精雕**（专业/课程卡图标徽标化、个人中心成长看板渐变头卡、帖子卡元信息图标化、评论树降噪、管理后台中文表头去 uppercase + 指标卡品牌色分层 + 窄屏卡片降级、搜索结果卡类型图标、个人库文件类型图标、登录页移动端品牌头条）· **微交互**（列表 stagger 入场、toast 进度条 + 滑出退场、confirmModal danger 变体）· **一致性清账**（emerald-/amber- 硬编码清零、内联 style 属性清零、评分星星 aria 语义修正、标签云频次 aria-label）· 容器内 ruff + pytest 538 例全绿，镜像 knowisle-app:1.2.0，git tag v1.2
**v1.1 优化版本**（2026-10-10，feature 分支）：**前端全面升级「翡翠绿·海岛」设计语言**（设计令牌 v2：brand 翡翠绿十档 + accent 暖沙十档 + 卡片阴影/圆角/动效令牌；main.css 公共组件类 .btn/.badge/.card/.input-base/.empty-state/.skeleton；全站 24 模板改版——导航药丸高亮、首页 Hero 海浪 + 数字滚动、登录/404 重设计、列表卡片化 + 相对时间 ZY.relTime、消灭整页 reload 与"加载中…"文字态改骨架屏）· **交互统一与可访问性**（全站 fetch 收口 ZY.api 统一错误透传带状态码、confirmModal 替代原生 confirm/prompt、ZY.bindModalA11y 模态框焦点圈定/Esc/焦点还原、:focus-visible 品牌色焦点环、prefers-reduced-motion 降级）· **后端逻辑完善**（AI 额度原子扣减行锁、个人库上传 MD5 部分唯一索引幂等、投票/评分/下载计数竞态收口、RRF 显式权重 0.65/0.35 + mixed 双路粗召回 + BM25 Redis 版本戳跨进程失效、feed 按 approved_at 上架时间排序、AI 首答 HTTPException 早退、上传 chapter_id 显式 422）· 新增迁移⑦⑧（uq_documents_owner_md5_personal、resources.approved_at 回填）· 修复两处真实 bug（ZY.api 401 误跳登录页清空表单、时间炸弹测试）· 测试 519→538 例全绿，镜像 knowisle-app:1.1.0，git tag v1.1
**v1.0 答辩演示定稿**（2026-10-02，M3 里程碑达成）：**前端体系 v1.0**（设计令牌统一 tailwind.config + main.css、全站 17 模板零内联脚本——页面 JS 外置 `static/js/pages/*.js`、vendor 新增 highlight.js 代码高亮、全局 toast、毛玻璃导航与页脚改版、列表摘要剥离 Markdown 标记）· **安全收尾**（nginx CSP 正式启用——script-src 含 'unsafe-eval' 为 Alpine 标准构建必需、logout 改 POST、Dockerfile 非 root 运行 uid=999）· seed_demo 扩展（9 篇演示帖：预填 AI 首答 + 采纳计分 / 结构化经验帖 + 预填 AI 摘要 + 精华 / 悬赏托管，积分双写配平）· 客户端打包资产完善（NSIS 修复 + 版本 1.0.0 + 一键打包清单，实际打包按 Owner 决定不执行）· 云端部署按 Owner 决定搁置（标注后期增补，v1.0 以本地可交付演示为目标）· 新增 [docs/接口契约.md](docs/接口契约.md)（82 接口逐条契约，代码为准）与 [docs/演示剧本与检查清单.md](docs/演示剧本与检查清单.md)（四幕剧本 + 断网兜底）· 演示主线 E2E 19 项真实环境实跑通过，测试 503→519 例全绿，镜像 knowisle-app:1.0.0
（此前 v0.9 交付完善，2026-10-02）：复习工具落地（`POST /api/review/outline` 复习大纲 + `POST /api/review/quiz` 考点习题，复用问答双通道/额度/落库口径，新页面 `/library/review`；期末串讲留二期）· 章节树 AI 校验落地（解析后附加层，结论写 worker 日志、异常降级不阻塞主流程）· 协审口径修正（多数决只计当前有效指派人、预览授权收紧 co_review 单阶段）· seed_demo.py 演示种子脚本（4 演示账号 + 3 门公共课程章节树，幂等 + --reset 保护）· 前端体系化改版（CDN 全部本地化钉版本断网可演示、Markdown 渲染统一 + DOMPurify 白名单、移动端汉堡导航与响应式补齐、首页改版 + GET /api/feed）· 合规修复（悬挂 tag 重打、SQLAlchemy `.any()`→`any_()`、死代码/死依赖清理），测试 469→503 例全绿，镜像 knowisle-app:0.9.1
（此前 v0.9，2026-10-01）：测试加固（pytest-cov 入工具链，测试 189→469 例全绿；核心模块检索/审核/积分行覆盖率 100%，整体 43%→83%，超额达成 §18.1「核心 ≥80%、整体 ≥60%」）✅ · 安全审计（SECRET_KEY 弱默认值拒绝启动、验证码回显默认关闭 + 日志脱敏 + 连续 5 次错码作废 + 发码 IP 限流、自定义 Key base_url SSRF 防护、Session Cookie Secure 配置项、JWT TTL 默认收紧 7 天→2 小时、兑换/下载积分加行锁、ILIKE 通配符转义、nginx 安全响应头 + /api/auth 与 /api/chat 限流）✅ · 性能优化（第 6 个迁移 e5f2b8c41d09 补 8 个索引、BM25 索引按课程缓存 + 打分卸载事件循环、platform_config 30s TTL 缓存、Chroma 集合 ID 缓存 + 连接复用、预览/下载改真流式 StreamingResponse、帖子列表聚合限定本页、会话续期 GETEX 单往返）✅ · 桌面客户端打包资产就绪（PyInstaller spec + NSIS 完整脚本 + 打包文档，实际打包留 v1.0）
（此前 v0.8：全站搜索（帖子/资源 ILIKE、标题命中优先于正文再按时间排序、板块/课程/专业过滤、/search 页 + 导航搜索框）· 信用阶梯处罚自动执行（<80 行为冷却 429 含剩余秒数 / <60 禁言 403 含截止时间 / <40 冻结 401，回升自动解除 + 禁言到期惰性解除，users.muted_until 新列迁移 d1e5f7a29c03）· 举报处理（四类目标幂等举报 + 管理端 open→processing→resolved/dismissed 状态机 + report_result 通知 + report_handle 审计）· 运营看板（/api/admin/dashboard 四分组 + ECharts 7 天趋势））
（此前 v0.7：经验长廊（结构化模板发帖 + AI 摘要异步生成、标签云/届别过滤/精华区、精华标记作者 +30 每帖一次）· 资料求援（悬赏托管/评论响应/采纳结算，禁自采纳与结算后改采）· 四板块全部开放、posts.ai_summary 新列 · 检查加固：采纳/精华/悬赏三处读-判-写加行锁封堵并发透支与重复结算）
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

# 4.5 灌演示数据（可选，v0.9 交付完善起）：4 演示账号 + 3 门公共课程章节树，幂等可重复执行
docker compose -f deploy/docker-compose.yml exec app python scripts/seed_demo.py

# 5. 登录：浏览器打开 http://localhost:8080/login ，输入学号 + 上一步绑定的邮箱；
#    开发期为假通道；仅在 .env 显式设置 EMAIL_CODE_ECHO=true 时，验证码才回显在页面
#    并写入 app 日志。默认值为 false，生产环境不得开启回显。

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

# 11. 全站搜索与治理（v0.8 起）：导航栏搜索框或 http://localhost:8080/search
#    全站搜索（帖子/资源分组结果，支持板块/课程/专业过滤）；管理后台
#    http://localhost:8080/admin 新增「举报处理」「运营看板」（ECharts 7 天趋势）两 Tab；
#    信用低于阈值自动阶梯处罚（<80 行为冷却 / <60 禁言 / <40 冻结，信用回升自动解除）

# 12. v1.3 交互与可恢复性体验：任意列表失败可重试、上传刷新后继续轮询、
#    投稿页显示预检/协审/终审阶段与驳回理由、Office 审核前可预览、
#    发帖草稿刷新不丢、问答失败保留问题可重试、资源/帖子/评论可举报

# 13. RAG 离线评测 / 索引重建（容器内执行，不调用外部 LLM）
docker compose -f deploy/docker-compose.yml exec app python scripts/evaluate_rag.py --grid \
  --output tests/fixtures/rag_eval/report.json
docker compose -f deploy/docker-compose.yml exec app python scripts/rebuild_rag_index.py --dry-run
# 真实重建（先备份、失败自动恢复）：
docker compose -f deploy/docker-compose.yml exec app \
  python scripts/rebuild_rag_index.py --yes --backup backups/rag/latest.json

# 13.5 v1.4 浏览器质量门槛（仅在测试容器安装依赖，不进生产镜像）
# 预览仅挂载 app 源码，保留镜像内离线模型；复用 Compose 环境与 8001 端口
MSYS_NO_PATHCONV=1 docker compose -f deploy/docker-compose.yml run -d \
  --name knowisle-v14-preview --no-deps -p 127.0.0.1:8001:8000 \
  -v "D:/desktop/ZhiYu/app:/srv/knowisle/app" app sh -c \
  "alembic upgrade head && gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 --timeout 300"
MSYS_NO_PATHCONV=1 docker run --rm --add-host=host.docker.internal:host-gateway \
  -v "D:/desktop/ZhiYu:/srv/knowisle" -w /srv/knowisle \
  mcr.microsoft.com/playwright/python@sha256:640d578aae63cfb632461d1b0aecb01414e4e020864ac3dd45a868dc0eff3078 \
  sh -c "pip install -q -r requirements-dev.txt -r requirements-browser.txt && pytest -q tests/test_v14_browser.py"
# 截图：tests/artifacts/v14/（临时全量）；docs/images/v14/（评审精选）
docker rm -f knowisle-v14-preview                    # 验收后移除预览容器

# 14. 停止 / 清理
docker compose -f deploy/docker-compose.yml down          # 停止
docker compose -f deploy/docker-compose.yml down -v       # 停止并删除数据卷（慎用）
```

## 目录结构

```
app/            FastAPI 应用（api/ web/ core/ community/ identity/ moderation/ workers/ storage/）
client/         桌面客户端薄壳（pywebview + NSIS 安装包）
alembic/        数据库迁移
deploy/         docker-compose.yml / nginx.conf / backup.sh / seaweedfs-s3.json
docs/           开发进度与目标.md（进度跟踪，每迭代更新）/ 接口契约.md / 演示剧本与检查清单.md
scripts/        init_majors.py / create_admin.py / seed_demo.py / evaluate_rag.py / rebuild_rag_index.py
tests/          pytest（纯函数 / API 契约 / 模板与内存替身 / 黄金集评测 / v1.4 浏览器门槛）+ frontend_v13_pages.test.js（Node 页面契约）
```

## 开发约定

- Git 工作流：`main` 保护分支 + `feature/xxx` + PR（≥1 人 review，CI 通过才可合并）
- Commit 规范：Conventional Commits（`feat:` / `fix:` / `refactor:` / `test:` / `docs:`）
- 本地检查：`pip install -r requirements-dev.txt && ruff check app tests scripts && pytest -q`；前端页面脚本：`node --test tests/frontend_v13_pages.test.js`（建议容器内运行）
- 依赖分层：`requirements.txt` 为应用与 CI 共用；`requirements-ml.txt`（torch CPU + sentence-transformers）仅由 Dockerfile 装入镜像，应用代码对其一律惰性导入；`requirements-browser.txt`（Playwright 固定版本）仅供浏览器测试容器，不进生产镜像或 CI 默认依赖
- 镜像引用固定：自建镜像按版本号命名（当前 `knowisle-app:1.4.0`，发版时手动递增，禁止 latest）；五个第三方镜像与 Dockerfile 基础镜像均以 `@sha256` digest 固定。pip/apt 依赖尚未完全锁定，不承诺异地重建逐字节一致
- 前端 CDN 依赖全部本地化到 `app/web/static/vendor/`（tailwind/htmx/alpine/echarts/marked/purify/highlight.js/Motion One 八库钉版本，静态资源可离线；实时 AI 仍需外部模型服务）；marked/DOMPurify/highlight 仅 Markdown 页面加载，Motion One 按需加载；**页面 JS 一律外置为 `static/js/pages/*.js` 并在 `{% block head %}` 内同步加载**（defer 的 Alpine 启动早于 defer 页面脚本；模板内禁止内联脚本与内联事件处理器，Jinja 注入走 x-data 属性或 `<script type="application/json" id="page-data">` 惰性块）——CSP 启用的前提，新增页面必须遵守
- 容器内跑测试务必以 root + 隔离 HOME 执行（`docker run --user root -e HOME=/root ...`），否则 pip 会落入镜像 HOME 的 user site 并污染工作区 `.local/`
- Embedding 模型 bge-small-zh-v1.5 在镜像构建时预下载（经 hf-mirror），运行时 `HF_HUB_OFFLINE=1` 离线加载
- 数据库迁移：`alembic revision --autogenerate -m "..."`，迁移随 app 容器启动自动执行

## 许可

MIT
