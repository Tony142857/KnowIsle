"""种子 / 演示数据（§20.4）：演示账号、专业名单、示例课程章节树 + 演示社区内容。

社区内容（帖子/评论/点赞/采纳/精华/悬赏托管）为静态定义的演示文案，
面向计算机专业学生，保证首页动态流、四板块、搜索、标签云/精华区开箱即有内容；
判重键为「标题 + 作者」，计分全部 users.score 与 score_logs 双写，账实一致。

注意：冷启动资料内容为团队真实课件/笔记（W3 起成员各自上传），
本脚本不得用于生成伪装的"用户资料"（不创建 documents/resources）。

用法（容器内）：
  docker compose -f deploy/docker-compose.yml exec app python scripts/seed_demo.py
  # 精确删除本脚本创建的演示数据后重建（需显式确认）：
  ALLOW_SEED_RESET=1 python scripts/seed_demo.py --reset

幂等：不加 --reset 时纯追加，账号/专业/课程/章节/帖子已存在则跳过，可反复执行。
管理员账号由 scripts/create_admin.py 负责，本脚本不创建 admin。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.engine.url import make_url

from app.community.bounty import SCORE_BOUNTY_ESCROW
from app.community.comments import SCORE_ANSWER_ACCEPTED, decide_accept_score
from app.community.experience import SCORE_POST_FEATURED, build_experience_content
from app.config import get_settings
from app.identity.growth import level_for_score
from app.storage.db import SessionLocal
from app.storage.models import (
    AiQuota,
    AuthIdentity,
    Chapter,
    Comment,
    Course,
    CreditLog,
    Document,
    Favorite,
    Follow,
    Major,
    Notification,
    Post,
    QaLog,
    Report,
    Resource,
    ReviewRecord,
    ScoreLog,
    Semester,
    User,
    Vote,
)
from scripts.init_majors import import_majors, parse_csv

MAJORS_CSV = Path(__file__).resolve().parent / "majors.example.csv"

# 仅允许指向本地 / 容器内数据库，防止误操作生产库
ALLOWED_DB_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}

DEMO_EMAIL_DOMAIN = "demo.stu.example.edu.cn"

# 演示账号：学号 2026xxxx 段 + @demo.stu.example.edu.cn 邮箱 + 「演示-」昵称前缀，
# 与真实用户及 E2E 临时账号（99000xxx / 202699xx 段）明显区隔。
# 年级各异是经验长廊「届别过滤」演示的需要（真实感优先于学号段与年级的一致性）。
DEMO_USERS = [
    {"student_no": "20260001", "nickname": "演示-张三", "real_name": "张三（演示）",
     "role": "student", "grade": "2023级", "email": f"zhangsan@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260101", "nickname": "演示-李四", "real_name": "李四（演示）",
     "role": "reviewer", "grade": "2024级", "email": f"lisi@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260102", "nickname": "演示-王五", "real_name": "王五（演示）",
     "role": "reviewer", "grade": "2025级", "email": f"wangwu@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260201", "nickname": "演示-赵六", "real_name": "赵六（演示）",
     "role": "builder", "grade": "2026级", "email": f"zhaoliu@{DEMO_EMAIL_DOMAIN}"},
]

# 示例公共课程（scope=public / status=active）+ 章节树（每门 3~5 章）
DEMO_COURSES = [
    {"name": "数据结构", "major": "计算机科学与技术",
     "description": "演示课程：数据结构基础章节树",
     "chapters": ["绪论与复杂度分析", "线性表", "栈与队列", "树与二叉树", "图"]},
    {"name": "计算机网络", "major": "网络工程",
     "description": "演示课程：计算机网络分层章节树",
     "chapters": ["物理层与数据链路层", "网络层", "传输层", "应用层"]},
    {"name": "机器学习导论", "major": "人工智能",
     "description": "演示课程：机器学习入门章节树",
     "chapters": ["监督学习基础", "线性模型", "神经网络入门"]},
]

# ---------------------------------------------------------------------------
# 演示社区内容：帖子 / 评论 / 点赞 / 采纳 / 精华 / 悬赏托管
#
# 全部静态定义（离线可演示、结果确定），判重键为「标题 + 作者」。
# AI 首答/AI 摘要直接预填 DB 字段：状态推导（community.posts.derive_*）以
# DB 字段非空为 done，与 Redis 键无关，故无需写 knowisle:ai_answer:* 等键。
# 计分口径与线上一致：悬赏托管 -N（bounty_escrow）、问答采纳 +15（answer_accepted，
# 自问自答不计分）、精华 +30（post_featured），users.score 与 score_logs 同事务双写，
# 保证 settle_scores 每日以 score_logs 重建榜单时账实一致（Redis 榜不落种子，
# 由每日对账以 SQL 为权威重建）。
# ---------------------------------------------------------------------------

AI_ANSWER_DISCLAIMER = "以下为 AI 基于社区资料的参考回答，欢迎同学补充纠正。"

# 帖子：board / 关联课程（示例公共课程名）/ 作者（演示学号）/ 标签 / 预填 AI 内容 / 点赞
DEMO_POSTS = [
    {
        "board": "qa", "course": "数据结构", "author": "20260201",
        "title": "二叉树遍历的递归与非递归写法哪个好？考试会考哪种",
        "content": (
            "复习到「树与二叉树」一章，书上先序/中序/后序遍历给的都是递归写法，课件里又补充了"
            "用栈实现的非递归版本。想请教大家：\n"
            "1. 实际写代码和期末考试中更推荐哪种写法？\n"
            "2. 非递归的后序遍历总是写错，有没有好记的思路？\n"
            "3. 两种写法在时间/空间复杂度上有区别吗？"
        ),
        "tags": ["数据结构", "二叉树", "递归", "期末复习"],
        "view_count": 156,
        "ai_first_answer": (
            f"{AI_ANSWER_DISCLAIMER}\n\n"
            "**复杂度对比**：两种写法时间复杂度都是 O(n)（每个节点访问一次），空间复杂度都是 O(h)，"
            "h 为树高——递归用的是调用栈（隐式），非递归用的是自己维护的栈（显式）。"
            "最坏情况（树退化成链）空间都是 O(n)，平衡树时是 O(log n)。\n\n"
            "**怎么选**：日常写代码优先递归，可读性好、不易出错；非递归的价值在于遍历深度很大时"
            "避免栈溢出（如 DOM 树、文件目录树），以及面试/考试中对栈结构的考察。\n\n"
            "**备考建议**：递归三种序必须达到默写熟练度；非递归先序和中序各背一个栈模板即可；"
            "后序推荐「按 根-右-左 遍历再反转结果」的技巧，等价于先序的镜像，比双栈法好记。\n\n"
            "（本课程公共库资料尚在建设中，以上结合课程大纲给出通用参考。）"
            "欢迎同学补充自己的刷题和考试经验！"
        ),
        "votes": [("20260001", 1), ("20260101", 1), ("20260102", 1)],
    },
    {
        "board": "qa", "course": "计算机网络", "author": "20260101",
        "title": "TCP 三次握手为什么不能是两次？",
        "content": (
            "传输层一章讲到 TCP 连接建立，为什么是三次握手而不是两次？"
            "老师课上提到一句「防止已失效的连接请求突然又传送到服务端」，没太听懂，"
            "能不能结合具体例子解释一下？另外 SYN 泛洪攻击和握手次数有关系吗？"
        ),
        "tags": ["计算机网络", "TCP", "传输层"],
        "view_count": 203,
        "ai_first_answer": (
            f"{AI_ANSWER_DISCLAIMER}\n\n"
            "**两次握手的问题**：无法完成「双向确认」。TCP 是全双工的，双方都要确认对方的"
            "初始序列号（ISN）和收发能力。两次握手只能让服务端确认客户端的 ISN，"
            "客户端无法确认服务端的 ISN。\n\n"
            "**历史失效连接的经典例子**：客户端发出的某个 SYN 因网络拥塞迟到，"
            "若两次握手即建立连接，服务端收到这个「幽灵 SYN」会直接分配资源等待数据，"
            "而客户端早已不认这个连接，资源被白白占用；三次握手中客户端可以用 RST "
            "拒绝这种失效连接，服务端随即释放资源。\n\n"
            "**与 SYN Flood 的关系**：攻击者大量发送 SYN 却不回应第三次握手的 ACK，"
            "使服务端的半连接队列被占满。常见防御有 SYN Cookie、调大 backlog、缩短超时重传。"
            "握手次数本身不是漏洞根源，而是「先分配资源再确认」的设计代价。\n\n"
            "（本课程公共库资料尚在建设中，以上结合课程大纲给出通用参考。）"
            "欢迎同学补充实验抓包观察到的情况！"
        ),
        "votes": [("20260001", 1), ("20260102", 1), ("20260201", 1)],
    },
    {
        "board": "qa", "course": "机器学习导论", "author": "20260102",
        "title": "过拟合怎么判断和处理？训练集准确率很高但测试集很差怎么办",
        "content": (
            "做课程实验时发现模型在训练集上准确率 99%，测试集只有 70% 左右，这应该是过拟合吧？"
            "想请教：\n"
            "1. 除了看训练/测试误差的差距，还有什么判断方法？\n"
            "2. 常用的处理手段有哪些，各自适合什么场景？\n"
            "3. 正则化系数一般怎么调？"
        ),
        "tags": ["机器学习", "过拟合", "正则化"],
        "view_count": 128,
        "ai_first_answer": (
            f"{AI_ANSWER_DISCLAIMER}\n\n"
            "**判断方法**：最直观的是画学习曲线——训练误差与验证误差之间存在明显 gap "
            "即过拟合；也可以做 k 折交叉验证，各折分数方差大也提示过拟合。"
            "你描述的「训练 99% / 测试 70%」是典型过拟合现象（也请先确认训练/测试集同分布、"
            "划分时做了分层抽样）。\n\n"
            "**处理手段（按尝试成本排序）**：① 更多数据或数据增强；② 正则化（L2 最常用，"
            "L1 可带来稀疏性）；③ 早停（early stopping，验证集指标连续多轮不涨就停）；"
            "④ 降低模型复杂度（更少的层/参数、更浅的树）；神经网络场景还常用 dropout。\n\n"
            "**调参建议**：正则化系数按对数尺度搜索（如 1e-4 到 1e1），以验证集指标为准，"
            "不要在测试集上调参。\n\n"
            "（本课程公共库资料尚在建设中，以上结合课程大纲给出通用参考。）"
            "欢迎同学补充实验里的实际调参经验！"
        ),
        "votes": [("20260001", 1), ("20260101", 1)],
    },
    {
        "board": "discuss", "course": "数据结构", "author": "20260201",
        "title": "期末复习：数据结构各章的时间分配怎么安排比较合理？",
        "content": (
            "还有三周期末考试，树和图感觉内容最多，线性表和栈队列相对简单。"
            "大家复习时是怎么分配时间的？有没有过来人分享一下各章的重点和常考题型？"
            "目前计划：绪论复杂度 1 天、线性表+栈队列 2 天、树与二叉树 4 天、图 4 天、"
            "剩下时间刷历年题，求拍砖。"
        ),
        "tags": ["期末复习", "数据结构", "学习方法"],
        "view_count": 87,
        "votes": [("20260001", 1), ("20260102", 1)],
    },
    {
        "board": "discuss", "course": "计算机网络", "author": "20260101",
        "title": "计网实验抓包作业，大家都用什么工具？Wireshark 过滤表达式求分享",
        "content": (
            "传输层实验要求抓 TCP 三次握手的包并截图分析。Wireshark 装好了一抓一大把包，"
            "看得眼花。求几个常用过滤表达式（比如只看 TCP、只看指定端口/指定主机的），"
            "或者有更轻量的替代工具也行。"
        ),
        "tags": ["实验", "Wireshark", "抓包"],
        "view_count": 64,
        "votes": [("20260001", 1), ("20260201", 1), ("20260102", -1)],
    },
    {
        "board": "experience", "course": None, "author": "20260001",
        "title": "从专业前 10% 到夏令营优秀营员：我的保研时间线与踩坑记录",
        "content": build_experience_content({
            "背景": (
                "2023 级计算机科学与技术专业，前六学期排名 9/102，无论文，"
                "有一段校级大创和蓝桥杯省二。最终拿到两所 985 夏令营优秀营员，预推免上岸。"
            ),
            "时间线": (
                "大三上 9-12 月：稳住 GPA，刷六级到 550+；\n"
                "1-2 月寒假：整理项目经历，准备简历和个人陈述初稿；\n"
                "3-4 月：关注目标院校通知，联系导师（邮件附成绩单和简历）；\n"
                "5-6 月：投递夏令营，建议 8-12 所分梯队；\n"
                "7 月：参营（机试 + 面试）；\n"
                "8-9 月：预推免与补录。"
            ),
            "经验要点": (
                "GPA 是硬门槛，大三上绝不能掉；简历只写能讲清楚的项目，面试官一定会深挖；"
                "机试刷 LeetCode 热题 100 足以覆盖大部分院校；联系导师宜早不宜迟，"
                "邮件要具体到导师近两年的论文方向；「为什么选我们学校」几乎必问，提前准备。"
            ),
            "避坑提示": (
                "不要等到 5 月才准备材料，成绩单盖章和推荐信都要提前；海投但不乱投，"
                "入营时间冲突时优先 tier 匹配的；面试不会的问题坦诚说不会，硬编会被追问到崩；"
                "优秀营员≠拟录取，务必看清目标院校政策是否还要走预推免。"
            ),
        }),
        "tags": ["保研", "夏令营", "经验"],
        "view_count": 312,
        "status": "featured",
        "ai_summary": (
            "2023 级计科学生（排名 9/102）保研复盘：大三上稳 GPA、寒假备材料、"
            "5-6 月分梯队投递夏令营、7 月参营。关键结论：GPA 是硬门槛，简历项目必须经得起"
            "深挖，联系导师要早且邮件具体到研究方向。核心避坑：优秀营员不等于拟录取，"
            "需确认院校预推免政策。"
        ),
        "votes": [("20260101", 1), ("20260102", 1), ("20260201", 1)],
    },
    {
        "board": "experience", "course": None, "author": "20260101",
        "title": "六级 425 到 580：三个月备考复盘（听力是性价比最高的提分点）",
        "content": build_experience_content({
            "背景": (
                "2024 级，首考 425 飘过，三个月后排位 580（听力 210、阅读 200、写译 170）。"
                "英语基础一般，词汇量起步约 4000。"
            ),
            "时间线": (
                "第 1-4 周：每天 50 个新词 + 精听 1 篇真题听力（听写 + 跟读）；\n"
                "第 5-8 周：真题分模块突破，阅读每两天一套、听力每天一套；\n"
                "第 9-12 周：每周两套完整模拟，严格计时，作文每周写两篇并找人互改。"
            ),
            "经验要点": (
                "听力提分性价比最高：精听比泛听有效得多，一篇听写到全对再换下一篇；"
                "阅读先看题干再定位原文，不要通读全文；写作背框架和句型，不要背全文模板；"
                "翻译积累中国文化类高频表达。"
            ),
            "避坑提示": (
                "不要只刷题不复盘，错题不总结等于白做；听力必须每天保持，断一周就回退；"
                "考前一周按正式考试时间做模拟，调整生物钟；单词 app 的「认识」标准太松，"
                "以真题中能否反应出词义为准。"
            ),
        }),
        "tags": ["六级", "英语", "备考"],
        "view_count": 178,
        "ai_summary": (
            "2024 级学生六级三个月 425→580 复盘：前四周打词汇和精听基础，中期分模块刷真题，"
            "最后整套模拟。关键结论：听力精听（听写+跟读）是最高性价比提分点，阅读先题后文，"
            "写作背框架不背全文。核心避坑：只刷题不复盘等于白做。"
        ),
        "votes": [("20260001", 1), ("20260201", 1)],
    },
    {
        "board": "experience", "course": None, "author": "20260102",
        "title": "后端开发实习面试复盘：四轮技术面我都踩了哪些坑",
        "content": build_experience_content({
            "背景": (
                "2025 级，大三下投递日常实习，某互联网中厂 Java 后端岗，"
                "四轮技术面 + HR 面，最终拿到 offer。项目只有一个课程设计魔改的秒杀系统。"
            ),
            "时间线": (
                "3 月初：改简历、把项目重新部署跑通并整理难点；\n"
                "3 月中：投递 20 余家，笔试 6 场；\n"
                "3 月底-4 月：密集面试，每次面完当晚复盘记录；\n"
                "4 月底：收到 2 个 offer，对比后入职。"
            ),
            "经验要点": (
                "计网/操作系统八股要结合项目讲，单背概念会被追问穿；Redis 和 MySQL 几乎每轮必问"
                "（缓存穿透/雪崩、索引和事务隔离级别）；算法题多是 LeetCode 中等难度，"
                "热题 100 + 剑指 Offer 够用；项目讲清「为什么这么设计」比「用了什么技术」重要。"
            ),
            "避坑提示": (
                "不要夸大项目，面试官会顺着你的描述往深挖；答不上来的题先讲思路，"
                "沉默是大忌；反问环节准备 1-2 个有质量的问题（团队技术栈、实习生培养）；"
                "面试录音复盘非常有效，能发现很多表达问题。"
            ),
        }),
        "tags": ["实习", "面试", "后端"],
        "view_count": 145,
        "ai_summary": (
            "2025 级学生后端实习求职复盘：3 月备简历投递，4 月四轮技术面后拿 offer。"
            "关键结论：八股要结合项目讲，Redis/MySQL 高频必问，项目设计动机比技术堆砌重要。"
            "核心避坑：不夸大项目、答不上来先讲思路、坚持当晚复盘。"
        ),
        "votes": [("20260001", 1), ("20260201", 1)],
    },
    {
        "board": "bounty", "course": "机器学习导论", "author": "20260001",
        "title": "求一份机器学习导论实验报告模板（含数据预处理与结果分析章节）",
        "content": (
            "课程实验二要交实验报告，老师只给了评分点没有模板。求一份结构完整的实验报告模板，"
            "最好包含：实验目的、环境与数据、数据预处理、模型与参数、结果分析、心得。"
            "采纳即结算 30 贡献分，谢谢！"
        ),
        "tags": ["实验报告", "机器学习", "求资料"],
        "bounty_score": 30,
        "view_count": 52,
        "votes": [("20260102", 1), ("20260201", 1)],
    },
]

# 评论：键为帖子标题；parent 为同帖评论列表下标（楼中楼）；accepted 为帖主采纳；
# votes 为给该评论点赞的演示学号（+1）。bounty 帖的评论为求援响应（未采纳不结算）。
DEMO_COMMENTS = {
    "二叉树遍历的递归与非递归写法哪个好？考试会考哪种": [
        {"author": "20260101", "accepted": True, "votes": ["20260001", "20260102"],
         "content": (
             "考试角度说两句：递归写法是送分题，必考，要练到默写级别的熟练度；"
             "非递归里先序和中序的栈模板各背一个就够，后序推荐「根-右-左再反转」的技巧，"
             "比双栈法好记。工程上只有遍历深度可能很大时（比如 DOM 树）才需要非递归，"
             "平时递归完全够用。"
         )},
        {"author": "20260102",
         "content": (
             "补充一个复杂度结论：两种写法时间都是 O(n)，空间都是 O(h)，h 是树高。"
             "最坏情况（树退化成链）都是 O(n)，平衡树时是 O(log n)。考试如果让分析复杂度，"
             "把「隐式栈 vs 显式栈」说清楚会有加分。"
         )},
        {"author": "20260201", "parent": 0,
         "content": "后序反转法确实好记！顺便确认下，非递归中序遍历是在「出栈」时访问节点对吧？"},
    ],
    "TCP 三次握手为什么不能是两次？": [
        {"author": "20260102", "accepted": True, "votes": ["20260001", "20260201"],
         "content": (
             "关键在「防止历史连接请求造成错误」：想象一个旧 SYN 因网络拥塞迟到，"
             "如果两次握手就建立连接，服务端会直接分配资源等数据，而客户端早就不认了，"
             "资源白白挂着。三次握手让客户端有机会用 RST 拒绝这种幽灵连接。"
             "另外两次握手无法让双方确认彼此的初始序列号（ISN），三次是最少的双向确认次数。"
         )},
        {"author": "20260001",
         "content": (
             "SYN Flood 就是钻半连接的空子：攻击者发大量 SYN 但不回 ACK，"
             "服务端半连接队列被占满就拒绝正常连接了。防御手段有 SYN Cookie、"
             "调大 backlog、缩短超时重传时间。"
         )},
        {"author": "20260101", "parent": 0,
         "content": "懂了！所以第三次握手在 TFO（快速打开）里还能顺带携带数据，这块考试会涉及吗？"},
    ],
    "过拟合怎么判断和处理？训练集准确率很高但测试集很差怎么办": [
        {"author": "20260001", "accepted": True, "votes": ["20260101", "20260201"],
         "content": (
             "判断：画学习曲线最直观——训练误差和验证误差之间 gap 很大就是过拟合；"
             "也可以看 k 折交叉验证的方差。处理按成本排序：先加数据/数据增强，"
             "再正则化（L2 最常用），再早停，最后考虑换更小的模型。"
             "调正则系数用对数尺度搜（1e-4 到 1e1），盯验证集指标，别在测试集上调。"
         )},
        {"author": "20260201",
         "content": "神经网络场景还可以用 dropout，实验里 p=0.5 起步很常见。BatchNorm 也有一定正则效果。"},
        {"author": "20260101",
         "content": (
             "训练 99% 测试 70% 是典型的过拟合没跑了。顺便确认下训练集和测试集是不是同分布，"
             "划分时有没有分层抽样？数据集类别不均衡的话 99% 也可能是假象。"
         )},
        {"author": "20260102", "parent": 0,
         "content": "谢谢！确实 gap 很大。早停的话一般看连续多少轮验证集不涨就停比较合适？"},
    ],
    "求一份机器学习导论实验报告模板（含数据预处理与结果分析章节）": [
        {"author": "20260102",
         "content": (
             "我这有去年实验一的报告结构，评分点和今年实验二差不多，章节是：实验目的 / "
             "环境与数据 / 预处理（缺失值处理 + 归一化）/ 模型与超参 / 结果与可视化 / 心得。"
             "需要的话我整理成 Markdown 传公共库。"
         )},
        {"author": "20260201",
         "content": "同求。预处理那一节最好带上 train/test 划分比例的说明，去年助教的扣分点就扣在这。"},
    ],
}


def plan_score_effects() -> list[dict]:
    """从种子内容定义推导全部计分事件（纯函数）：{student_no, delta, reason, ref_type, ref_key}。

    与 seed_posts 写库共用同一口径，供单元测试做账实配平校验（bounty_escrow /
    answer_accepted / post_featured 的金额、归属、防自问自答）。
    """
    posts_by_title = {p["title"]: p for p in DEMO_POSTS}
    effects: list[dict] = []
    for spec in DEMO_POSTS:
        if spec["board"] == "bounty" and spec.get("bounty_score"):
            effects.append({
                "student_no": spec["author"], "delta": -spec["bounty_score"],
                "reason": SCORE_BOUNTY_ESCROW, "ref_type": "post", "ref_key": spec["title"],
            })
        if spec.get("status") == "featured":
            effects.append({
                "student_no": spec["author"], "delta": SCORE_POST_FEATURED,
                "reason": "post_featured", "ref_type": "post", "ref_key": spec["title"],
            })
    for title, comments in DEMO_COMMENTS.items():
        post = posts_by_title[title]
        for idx, cspec in enumerate(comments):
            # 自问自答不计分（与 community.comments.decide_accept_score 同口径）
            if post["board"] == "qa" and cspec.get("accepted") and cspec["author"] != post["author"]:
                effects.append({
                    "student_no": cspec["author"], "delta": SCORE_ANSWER_ACCEPTED,
                    "reason": "answer_accepted", "ref_type": "comment", "ref_key": (title, idx),
                })
    return effects

# --reset 删除演示账号前的依赖检查：任一表存在引用则跳过删除并告警（绝不强删业务数据）
_USER_DEPENDENCIES = [
    (Post, Post.author_id, "posts"),
    (Comment, Comment.author_id, "comments"),
    (Document, Document.owner_id, "documents"),
    (Resource, Resource.uploader_id, "resources"),
    (ReviewRecord, ReviewRecord.reviewer_id, "review_records"),
    (Vote, Vote.user_id, "votes"),
    (Favorite, Favorite.user_id, "favorites"),
    (Follow, Follow.user_id, "follows"),
    (Report, Report.reporter_id, "reports"),
    (Notification, Notification.user_id, "notifications"),
    (ScoreLog, ScoreLog.user_id, "score_logs"),
    (CreditLog, CreditLog.user_id, "credit_logs"),
    (QaLog, QaLog.user_id, "qa_logs"),
    (AiQuota, AiQuota.user_id, "ai_quotas"),
    (Semester, Semester.owner_id, "semesters"),
    (Course, Course.owner_id, "courses(owner)"),
]

# --reset 删除演示课程前的依赖检查
_COURSE_DEPENDENCIES = [
    (Document, Document.course_id, "documents"),
    (Resource, Resource.course_id, "resources"),
    (Post, Post.course_id, "posts"),
]


def check_db_target() -> None:
    """安全检查：DATABASE_URL 必须指向本地/容器库，否则拒绝执行。"""
    host = make_url(get_settings().database_url).host
    if host not in ALLOWED_DB_HOSTS:
        sys.exit(f"拒绝执行：DATABASE_URL 主机「{host}」不在本地/容器白名单 {sorted(ALLOWED_DB_HOSTS)}")


def confirm_reset() -> None:
    """--reset 二次确认：ALLOW_SEED_RESET=1 或交互输入 RESET。"""
    if os.environ.get("ALLOW_SEED_RESET") == "1":
        return
    if sys.stdin.isatty():
        answer = input("即将删除全部演示数据（演示账号 + 示例课程章节树）后重建，输入 RESET 确认: ")
        if answer.strip() == "RESET":
            return
    sys.exit("已取消：--reset 需要设置 ALLOW_SEED_RESET=1（或交互终端输入 RESET）")


async def _dependency_count(db, user_id: int) -> dict[str, int]:
    refs = {}
    for model, col, label in _USER_DEPENDENCIES:
        count = (await db.execute(select(func.count()).select_from(model).where(col == user_id))).scalar_one()
        if count:
            refs[label] = count
    return refs


async def seed_users(db, majors: dict[str, int]) -> tuple[int, int, list[str]]:
    """创建演示账号（含 email_fallback 身份绑定），返回 (新增, 跳过, 警告)。

    既有账号同步年级：届别过滤演示要求作者年级各异，旧版种子创建的账号
    （统一 2026 级）在重跑时按 DEMO_USERS 口径更新 grade。
    """
    provider = get_settings().auth_provider
    default_major_id = majors.get(DEMO_COURSES[0]["major"])
    created = skipped = 0
    warnings: list[str] = []
    for spec in DEMO_USERS:
        user = (
            await db.execute(select(User).where(User.student_no == spec["student_no"]))
        ).scalar_one_or_none()
        if user is not None:
            skipped += 1
            if user.grade != spec["grade"]:
                user.grade = spec["grade"]
            continue
        nickname_taken = (
            await db.execute(select(User.id).where(User.nickname == spec["nickname"]))
        ).scalar_one_or_none()
        if nickname_taken is not None:
            warnings.append(f"昵称「{spec['nickname']}」已被占用，跳过学号 {spec['student_no']}")
            continue
        user = User(
            student_no=spec["student_no"], real_name=spec["real_name"],
            nickname=spec["nickname"], role=spec["role"],
            major_id=default_major_id, grade=spec["grade"],
        )
        db.add(user)
        await db.flush()
        db.add(AuthIdentity(
            user_id=user.id, provider=provider, external_id=spec["student_no"],
            raw_profile={"email": spec["email"], "registered_via": "seed_demo"},
        ))
        created += 1
    return created, skipped, warnings


async def seed_courses(db, majors: dict[str, int]) -> tuple[int, int, int, int, list[str]]:
    """创建示例公共课程与章节树，返回 (课程新增, 课程跳过, 章节新增, 章节跳过, 警告)。"""
    c_created = c_skipped = ch_created = ch_skipped = 0
    warnings: list[str] = []
    for spec in DEMO_COURSES:
        major_id = majors.get(spec["major"])
        if major_id is None:
            warnings.append(f"专业「{spec['major']}」不存在，跳过课程「{spec['name']}」")
            continue
        course = (
            await db.execute(
                select(Course).where(
                    Course.scope == "public", Course.name == spec["name"],
                    Course.major_id == major_id,
                )
            )
        ).scalar_one_or_none()
        if course is None:
            course = Course(
                scope="public", status="active", name=spec["name"],
                description=spec["description"], major_id=major_id,
            )
            db.add(course)
            await db.flush()
            c_created += 1
        else:
            c_skipped += 1
        existing_titles = set(
            (await db.execute(
                select(Chapter.title).where(
                    Chapter.course_id == course.id, Chapter.parent_id.is_(None))
            )).scalars().all()
        )
        for idx, title in enumerate(spec["chapters"], start=1):
            if title in existing_titles:
                ch_skipped += 1
                continue
            db.add(Chapter(course_id=course.id, title=title, order_idx=idx))
            existing_titles.add(title)
            ch_created += 1
    return c_created, c_skipped, ch_created, ch_skipped, warnings


def _apply_seed_score(db, user: User, delta: int, reason: str, ref_type: str, ref_id: int) -> None:
    """种子计分（与 identity.growth.grant_score 同口径）：score_logs 明细 + users.score
    同事务双写 + 等级阈值检查（只升不降），调用方负责 commit。
    不写 Redis 榜与站内通知：settle_scores 每日以 SQL 为权威重建榜单，
    演示数据落 SQL 即可保证对账一致，避免演示环境产生额外副作用。"""
    db.add(ScoreLog(user_id=user.id, delta=delta, reason=reason, ref_type=ref_type, ref_id=ref_id))
    user.score += delta
    if (new_level := level_for_score(user.score)) > user.level:
        user.level = new_level


async def seed_posts(db) -> tuple[int, int, int, int, list[str]]:
    """灌入演示社区内容（判重键：标题 + 作者），返回 (帖子新增, 帖子跳过, 评论新增, 点赞新增, 警告)。

    帖子已存在则整帖跳过（其评论/点赞/计分随之跳过），保证重复执行全部 skip；
    计分事件与帖子同事务写入，与 plan_score_effects() 的纯函数口径一致。
    """
    demo_nos = [u["student_no"] for u in DEMO_USERS]
    users = {
        u.student_no: u
        for u in (await db.execute(select(User).where(User.student_no.in_(demo_nos)))).scalars().all()
    }
    if len(users) < len(demo_nos):
        return 0, 0, 0, 0, ["演示账号不完整，跳过社区内容种子"]
    demo_names = [c["name"] for c in DEMO_COURSES]
    courses = {
        c.name: c
        for c in (await db.execute(
            select(Course).where(Course.scope == "public", Course.name.in_(demo_names))
        )).scalars().all()
    }
    p_created = p_skipped = cm_created = v_created = 0
    warnings: list[str] = []
    for spec in DEMO_POSTS:
        author = users[spec["author"]]
        exists = (
            await db.execute(
                select(Post.id).where(Post.title == spec["title"], Post.author_id == author.id)
            )
        ).scalar_one_or_none()
        if exists is not None:
            p_skipped += 1
            continue
        course = courses.get(spec["course"]) if spec.get("course") else None
        if spec["board"] == "qa" and course is None:
            warnings.append(f"课程「{spec['course']}」不存在，跳过帖子「{spec['title']}」")
            continue
        post = Post(
            author_id=author.id, board=spec["board"],
            major_id=course.major_id if course else author.major_id,
            course_id=course.id if course else None,
            title=spec["title"], content=spec["content"],
            tags=spec.get("tags") or None,
            bounty_score=spec.get("bounty_score", 0),
            ai_first_answer=spec.get("ai_first_answer"),
            ai_summary=spec.get("ai_summary"),
            view_count=spec.get("view_count", 0),
            status=spec.get("status", "normal"),
        )
        db.add(post)
        await db.flush()
        p_created += 1
        if post.board == "bounty" and post.bounty_score:
            # 悬赏托管：与 api/posts.create_post 同口径，发帖即扣帖主贡献分
            _apply_seed_score(db, author, -post.bounty_score, SCORE_BOUNTY_ESCROW, "post", post.id)
        if post.status == "featured":
            # 精华标记：与 api/admin.set_featured 同口径（管理员/共建者标记语义，
            # 种子里无操作者账号，按「管理员标记」只留 score_logs 明细，不写 audit_logs）
            _apply_seed_score(db, author, SCORE_POST_FEATURED, "post_featured", "post", post.id)
        created_comments: list[Comment] = []
        for cspec in DEMO_COMMENTS.get(post.title, []):
            comment = Comment(
                post_id=post.id,
                author_id=users[cspec["author"]].id,
                parent_id=(
                    created_comments[cspec["parent"]].id if cspec.get("parent") is not None else None
                ),
                content=cspec["content"],
                is_accepted=bool(cspec.get("accepted")),
            )
            db.add(comment)
            await db.flush()
            created_comments.append(comment)
            cm_created += 1
            if cspec.get("accepted"):
                # 采纳：与 api/posts.accept_comment 同口径（自问自答 0 分）
                post.accepted_comment_id = comment.id
                granted = decide_accept_score(post.author_id, comment.author_id)
                if granted:
                    _apply_seed_score(
                        db, users[cspec["author"]], granted, "answer_accepted", "comment", comment.id
                    )
            for voter_no in cspec.get("votes", []):
                db.add(Vote(user_id=users[voter_no].id, target_type="comment",
                            target_id=comment.id, value=1))
                v_created += 1
        for voter_no, value in spec.get("votes", []):
            db.add(Vote(user_id=users[voter_no].id, target_type="post",
                        target_id=post.id, value=value))
            v_created += 1
    return p_created, p_skipped, cm_created, v_created, warnings


async def reset_demo_content(db, demo_user_ids: list[int]) -> dict[str, int]:
    """精确删除种子社区内容：votes → score_logs（先回滚 users.score）→ comments → posts。

    判据与 seed_posts 判重键一致（演示作者 + 种子标题）。计分先按明细反向冲正
    users.score 再删 score_logs，删行后账实依然一致；种子帖下若有非种子评论/
    点赞（真实用户互动），随帖一并清除并冲正其计分明细，避免残留悬挂引用。
    """
    stats = {"posts": 0, "comments": 0, "votes": 0, "score_logs": 0}
    titles = [p["title"] for p in DEMO_POSTS]
    post_ids = list(
        (await db.execute(
            select(Post.id).where(Post.author_id.in_(demo_user_ids), Post.title.in_(titles))
        )).scalars().all()
    )
    if not post_ids:
        return stats
    comment_ids = list(
        (await db.execute(select(Comment.id).where(Comment.post_id.in_(post_ids)))).scalars().all()
    )
    stats["votes"] = (
        await db.execute(
            delete(Vote).where(or_(
                and_(Vote.target_type == "post", Vote.target_id.in_(post_ids)),
                and_(Vote.target_type == "comment", Vote.target_id.in_(comment_ids or [-1])),
            ))
        )
    ).rowcount
    score_rows = (
        await db.execute(
            select(ScoreLog).where(or_(
                and_(ScoreLog.ref_type == "post", ScoreLog.ref_id.in_(post_ids)),
                and_(ScoreLog.ref_type == "comment", ScoreLog.ref_id.in_(comment_ids or [-1])),
            ))
        )
    ).scalars().all()
    for row in score_rows:
        user = await db.get(User, row.user_id)
        if user is not None:
            user.score -= row.delta
            user.level = level_for_score(user.score)
    if score_rows:
        stats["score_logs"] = (
            await db.execute(delete(ScoreLog).where(ScoreLog.id.in_([r.id for r in score_rows])))
        ).rowcount
    if comment_ids:
        stats["comments"] = (
            await db.execute(delete(Comment).where(Comment.id.in_(comment_ids)))
        ).rowcount
    stats["posts"] = (
        await db.execute(delete(Post).where(Post.id.in_(post_ids)))
    ).rowcount
    return stats


async def reset_demo(db, majors: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """精确删除本脚本创建的演示数据：种子社区内容 + 固定学号段的演示账号 + 示例公共课程。

    专业名单属 init_majors.py 管辖，不在此删除。先清社区内容（votes → score_logs →
    comments → posts），再做账号/课程删除；任一演示数据被其余业务数据引用
    （资料/审核记录等）则跳过该项并告警，绝不级联强删。
    返回 (警告, 社区内容删除统计)。
    """
    warnings: list[str] = []
    demo_nos = [u["student_no"] for u in DEMO_USERS]
    users = (await db.execute(select(User).where(User.student_no.in_(demo_nos)))).scalars().all()
    content_stats = await reset_demo_content(db, [u.id for u in users])
    for user in users:
        refs = await _dependency_count(db, user.id)
        if refs:
            warnings.append(f"账号 {user.student_no}（{user.nickname}）存在业务引用 {refs}，跳过删除")
            continue
        await db.execute(delete(AuthIdentity).where(AuthIdentity.user_id == user.id))
        await db.delete(user)
    demo_names = [c["name"] for c in DEMO_COURSES]
    courses = (
        await db.execute(
            select(Course).where(
                Course.scope == "public", Course.name.in_(demo_names),
                Course.major_id.in_(majors.values()),
            )
        )
    ).scalars().all()
    for course in courses:
        refs = {}
        for model, col, label in _COURSE_DEPENDENCIES:
            count = (
                await db.execute(select(func.count()).select_from(model).where(col == course.id))
            ).scalar_one()
            if count:
                refs[label] = count
        if refs:
            warnings.append(f"课程「{course.name}」(id={course.id}) 存在业务引用 {refs}，跳过删除")
            continue
        await db.execute(delete(Chapter).where(Chapter.course_id == course.id))
        await db.delete(course)
    return warnings, content_stats


async def run(reset: bool) -> None:
    check_db_target()
    if reset:
        confirm_reset()
    items = parse_csv(MAJORS_CSV)
    m_created, m_skipped = await import_majors(items)

    async with SessionLocal() as db:
        majors = dict(
            (await db.execute(select(Major.name, Major.id))).all()
        )
        warnings: list[str] = []
        content_stats: dict[str, int] | None = None
        if reset:
            w, content_stats = await reset_demo(db, majors)
            warnings += w
            await db.commit()
        u_created, u_skipped, w = await seed_users(db, majors)
        warnings += w
        c_created, c_skipped, ch_created, ch_skipped, w = await seed_courses(db, majors)
        warnings += w
        p_created, p_skipped, cm_created, v_created, w = await seed_posts(db)
        warnings += w
        await db.commit()

    print(f"专业名单：新增 {m_created}，跳过 {m_skipped}")
    print(f"演示账号：新增 {u_created}，跳过 {u_skipped}")
    print(f"示例课程：新增 {c_created}，跳过 {c_skipped}")
    print(f"课程章节：新增 {ch_created}，跳过 {ch_skipped}")
    print(f"演示帖子：新增 {p_created}，跳过 {p_skipped}")
    print(f"演示评论：新增 {cm_created}")
    print(f"演示点赞：新增 {v_created}")
    if content_stats is not None:
        print(f"reset 清理：帖子 {content_stats['posts']}，评论 {content_stats['comments']}，"
              f"点赞 {content_stats['votes']}，计分明细 {content_stats['score_logs']}")
    for warning in warnings:
        print(f"警告：{warning}")

    role_names = {"student": "学生", "reviewer": "协审员", "builder": "共建者", "admin": "管理员"}
    print("\n演示账号清单（邮箱验证码通道登录，验证码见 EMAIL_CODE_ECHO / 邮件日志）：")
    print(f"{'学号':<10}{'昵称':<12}{'角色':<8}邮箱")
    for spec in DEMO_USERS:
        print(f"{spec['student_no']:<10}{spec['nickname']:<12}"
              f"{role_names[spec['role']]:<8}{spec['email']}")
    print("\n提示：管理员账号请使用 scripts/create_admin.py 单独创建。")


def main() -> None:
    parser = argparse.ArgumentParser(description="演示数据种子脚本（幂等；--reset 精确删除后重建）")
    parser.add_argument("--reset", action="store_true",
                        help="删除本脚本创建的演示数据后重建（需 ALLOW_SEED_RESET=1 或交互确认）")
    args = parser.parse_args()
    asyncio.run(run(args.reset))


if __name__ == "__main__":
    main()
