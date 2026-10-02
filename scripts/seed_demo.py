"""种子 / 演示数据（§20.4）：仅用于补齐章节树、演示账号等非内容数据。

注意：冷启动资料内容为团队真实课件/笔记（W3 起成员各自上传），
本脚本不得用于生成伪装的"用户资料"（不创建 documents/resources/posts）。

用法（容器内）：
  docker compose -f deploy/docker-compose.yml exec app python scripts/seed_demo.py
  # 精确删除本脚本创建的演示数据后重建（需显式确认）：
  ALLOW_SEED_RESET=1 python scripts/seed_demo.py --reset

幂等：不加 --reset 时纯追加，账号/专业/课程/章节已存在则跳过，可反复执行。
管理员账号由 scripts/create_admin.py 负责，本脚本不创建 admin。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.engine.url import make_url

from app.config import get_settings
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
DEMO_GRADE = "2026级"

# 演示账号：学号 2026xxxx 段 + @demo.stu.example.edu.cn 邮箱 + 「演示-」昵称前缀，
# 与真实用户（2023 级起）及 E2E 临时账号（99000xxx / 202699xx 段）明显区隔。
DEMO_USERS = [
    {"student_no": "20260001", "nickname": "演示-张三", "real_name": "张三（演示）",
     "role": "student", "email": f"zhangsan@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260101", "nickname": "演示-李四", "real_name": "李四（演示）",
     "role": "reviewer", "email": f"lisi@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260102", "nickname": "演示-王五", "real_name": "王五（演示）",
     "role": "reviewer", "email": f"wangwu@{DEMO_EMAIL_DOMAIN}"},
    {"student_no": "20260201", "nickname": "演示-赵六", "real_name": "赵六（演示）",
     "role": "builder", "email": f"zhaoliu@{DEMO_EMAIL_DOMAIN}"},
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
    """创建演示账号（含 email_fallback 身份绑定），返回 (新增, 跳过, 警告)。"""
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
            major_id=default_major_id, grade=DEMO_GRADE,
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


async def reset_demo(db, majors: dict[str, int]) -> list[str]:
    """精确删除本脚本创建的演示数据：固定学号段的演示账号 + 固定名称的示例公共课程。

    专业名单属 init_majors.py 管辖，不在此删除。任一演示数据被业务数据引用
    （帖子/资料/审核记录等）则跳过该项并告警，绝不级联强删。
    """
    warnings: list[str] = []
    demo_nos = [u["student_no"] for u in DEMO_USERS]
    users = (await db.execute(select(User).where(User.student_no.in_(demo_nos)))).scalars().all()
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
    return warnings


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
        if reset:
            warnings += await reset_demo(db, majors)
            await db.commit()
        u_created, u_skipped, w = await seed_users(db, majors)
        warnings += w
        c_created, c_skipped, ch_created, ch_skipped, w = await seed_courses(db, majors)
        warnings += w
        await db.commit()

    print(f"专业名单：新增 {m_created}，跳过 {m_skipped}")
    print(f"演示账号：新增 {u_created}，跳过 {u_skipped}")
    print(f"示例课程：新增 {c_created}，跳过 {c_skipped}")
    print(f"课程章节：新增 {ch_created}，跳过 {ch_skipped}")
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
