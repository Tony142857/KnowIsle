"""三级审核状态机（模块 B2，状态流转见 §6.3）。

precheck → co_review（≥2 人多数决，48h 限时）→ final（管理员终审）→ done。
通过后：复制派生 public 副本（新 Document + 重编号 chunks + chunks_public 向量）、
积分结算、通知投稿人；个人库原件不动。驳回必附理由，可修改重投（复用 rejected
resource 重新进入 precheck）。

本模块所有函数只改 session 不 commit，由 API / worker 层统一提交；
ARQ 任务入队（预览转换 / 章节摘要回填）也放在 API 层，本模块不依赖 workers 运行时。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import get_embedding
from app.identity.growth import SCORE_UPLOAD_APPROVED, grant_score
from app.moderation.assign import assign_reviewers
from app.storage.models import (
    Chunk,
    Document,
    Notification,
    Resource,
    ReviewRecord,
    ReviewTask,
    User,
)
from app.storage.vector_store import COLLECTION_PUBLIC, get_vector_store
from app.workers.parse_worker import _EMBED_BATCH, build_chunk_meta


def make_public_chunk_id(doc_id: int, seq: int) -> str:
    """派生 public 副本的 chunk 业务 ID（与 semantic_splitter 的 pub_d 编号约定一致）。"""
    return f"pub_d{doc_id}_{seq:05d}"


def _notify(
    session: AsyncSession,
    user_id: int,
    type_: str,
    title: str,
    body: str = "",
    link: str = "",
) -> None:
    """站内通知直接落库（send_notification ARQ 任务为 v0.6 占位，不调用）。"""
    session.add(
        Notification(
            user_id=user_id, type=type_, title=title, body=body or None, link=link or None
        )
    )


async def create_submission(
    session: AsyncSession,
    user: User,
    doc: Document,
    course,
    chapter,
    title: str,
    description: str | None,
) -> tuple[Resource, ReviewTask, bool]:
    """投稿建档：新建 Resource + precheck 阶段 ReviewTask，返回 (resource, task, is_resubmit)。

    同一文档重复投稿：审核中 → ValueError("duplicate")；已上架 → ValueError("approved")；
    已驳回 → 复用原 resource（更新元信息、状态回 pending）重走审核，is_resubmit=True。
    """
    existing = (
        await session.execute(select(Resource).where(Resource.document_id == doc.id))
    ).scalars().first()
    chapter_id = chapter.id if chapter is not None else None
    if existing is not None:
        if existing.review_status in ("pending", "co_reviewing"):
            raise ValueError("duplicate")
        if existing.review_status == "approved":
            raise ValueError("approved")
        existing.title = title
        existing.description = description
        existing.course_id = course.id
        existing.chapter_id = chapter_id
        existing.review_status = "pending"
        task = ReviewTask(resource_id=existing.id, stage="precheck")
        session.add(task)
        await session.flush()
        return existing, task, True
    resource = Resource(
        document_id=doc.id,
        course_id=course.id,
        uploader_id=user.id,
        title=title,
        description=description,
        chapter_id=chapter_id,
    )
    session.add(resource)
    await session.flush()
    task = ReviewTask(resource_id=resource.id, stage="precheck")
    session.add(task)
    await session.flush()
    return resource, task, False


async def apply_precheck_result(
    session: AsyncSession, task: ReviewTask, result: dict
) -> None:
    """应用预检结果：硬失败直接驳回；否则推进协审并指派/通知协审员。"""
    task.precheck_result = result
    resource = await session.get(Resource, task.resource_id)
    if result.get("hard_fail"):
        await _reject(session, task, result.get("reason") or "预检未通过")
        return
    assignees = await assign_reviewers(session, resource)
    task.assignee_ids = assignees
    resource.review_status = "co_reviewing"
    if not assignees:
        # 无可用协审员时跳过协审直送管理员终审兜底，避免任务卡死
        task.stage = "final"
        return
    task.stage = "co_review"
    for user_id in assignees:
        _notify(session, user_id, "review_assign", f"新协审任务：{resource.title}", link="/review")


def decide_co_outcome(verdicts: list[str], needed: int) -> str | None:
    """协审多数决（纯函数）：人数未齐返回 None；驳回票过半则 reject，否则 approve。"""
    if len(verdicts) < needed:
        return None
    rejects = verdicts.count("reject")
    return "reject" if rejects > needed / 2 else "approve"


async def submit_co_verdict(
    session: AsyncSession,
    user: User,
    task: ReviewTask,
    verdict: str,
    comment: str | None,
) -> str:
    """提交协审意见，返回 "recorded" / "advanced_final" / "rejected"。

    ValueError 约定：stage（非协审阶段）/ not_assigned（未指派）→ 调用方映射 404；
    already（重复提交）/ comment_required（驳回无理由）→ 映射 422。
    """
    if task.stage != "co_review":
        raise ValueError("stage")
    if user.id not in (task.assignee_ids or []):
        raise ValueError("not_assigned")
    existing = (
        await session.execute(
            select(ReviewRecord).where(
                ReviewRecord.task_id == task.id,
                ReviewRecord.reviewer_id == user.id,
                ReviewRecord.stage == "co_review",
            )
        )
    ).scalars().first()
    if existing is not None:
        raise ValueError("already")
    if verdict == "reject" and not (comment or "").strip():
        raise ValueError("comment_required")
    session.add(
        ReviewRecord(
            task_id=task.id, reviewer_id=user.id,
            stage="co_review", verdict=verdict, comment=comment,
        )
    )
    await session.flush()

    records = (
        await session.execute(
            select(ReviewRecord)
            .where(ReviewRecord.task_id == task.id, ReviewRecord.stage == "co_review")
            .order_by(ReviewRecord.id)
        )
    ).scalars().all()
    outcome = decide_co_outcome(
        [r.verdict for r in records], needed=len(task.assignee_ids or [])
    )
    if outcome is None:
        return "recorded"
    if outcome == "reject":
        reason = "；".join(
            r.comment for r in records if r.verdict == "reject" and r.comment
        )
        await _reject(session, task, reason or "协审驳回")
        return "rejected"
    task.stage = "final"
    return "advanced_final"


async def final_verdict(
    session: AsyncSession,
    admin: User,
    task: ReviewTask,
    verdict: str,
    comment: str | None,
) -> tuple[int, int | None]:
    """管理员终审，返回 (score_granted, new_doc_id)。

    approve → 复制派生 public 副本并结算积分，new_doc_id 为派生文档 id（供调用方
    判断 file_type 后入队 convert_preview / backfill_chapter_summaries）；
    reject → (0, None)。ValueError 约定同 submit_co_verdict。
    """
    if task.stage != "final":
        raise ValueError("stage")
    if verdict == "reject" and not (comment or "").strip():
        raise ValueError("comment_required")
    session.add(
        ReviewRecord(
            task_id=task.id, reviewer_id=admin.id,
            stage="final", verdict=verdict, comment=comment,
        )
    )
    if verdict == "reject":
        await _reject(session, task, comment)
        return 0, None
    new_doc_id = await _approve(session, task)
    return SCORE_UPLOAD_APPROVED, new_doc_id


async def _reject(session: AsyncSession, task: ReviewTask, reason: str) -> None:
    """驳回：资源回 rejected、任务终结、通知投稿人（附理由，可修改重投）。"""
    resource = await session.get(Resource, task.resource_id)
    resource.review_status = "rejected"
    task.stage = "done"
    task.finished_at = datetime.now(UTC)
    _notify(
        session, resource.uploader_id, "review_result",
        f"投稿未通过：{resource.title}", body=reason, link="/library",
    )


async def _approve(session: AsyncSession, task: ReviewTask) -> int:
    """终审通过：复制派生 public 副本并结算积分，返回派生文档 id。

    - 新 Document：course_id=目标公共课程、scope='public'、status='parsed'，
      storage_key 复用原对象不重复上传，个人库原件不动
    - chunks 复制：按源 Chunk.id 排序从 1 编号 pub_d{新doc_id}_{seq:05d}，
      chapter_id 统一为投稿挂载章节
    - 向量重新 embed 写入 chunks_public（metadata 复用 parse_worker.build_chunk_meta 格式）
    """
    resource = await session.get(Resource, task.resource_id)
    src_doc = await session.get(Document, resource.document_id)
    resource.review_status = "approved"
    task.stage = "done"
    task.finished_at = datetime.now(UTC)

    new_doc = Document(
        course_id=resource.course_id,
        owner_id=resource.uploader_id,
        scope="public",
        file_name=src_doc.file_name,
        file_type=src_doc.file_type,
        storage_key=src_doc.storage_key,
        md5=src_doc.md5,
        status="parsed",
    )
    session.add(new_doc)
    await session.flush()

    src_chunks = (
        await session.execute(
            select(Chunk).where(Chunk.document_id == src_doc.id).order_by(Chunk.id)
        )
    ).scalars().all()
    derived = [
        {
            "chunk_id": make_public_chunk_id(new_doc.id, seq),
            "chapter_id": resource.chapter_id,
            "page_num": src.page_num,
        }
        for seq, src in enumerate(src_chunks, start=1)
    ]
    if derived:
        embedding = get_embedding()
        vectors: list[list[float]] = []
        for i in range(0, len(src_chunks), _EMBED_BATCH):
            vectors.extend(
                await embedding.embed([c.content for c in src_chunks[i : i + _EMBED_BATCH]])
            )
        store = get_vector_store()
        collection = await store.get_or_create_collection(COLLECTION_PUBLIC)
        await store.upsert(
            collection,
            ids=[d["chunk_id"] for d in derived],
            embeddings=vectors,
            metadatas=[build_chunk_meta(new_doc, d) for d in derived],
            documents=[c.content for c in src_chunks],
        )
        session.add_all(
            Chunk(
                chunk_id=d["chunk_id"],
                document_id=new_doc.id,
                course_id=new_doc.course_id,
                chapter_id=resource.chapter_id,
                owner_id=new_doc.owner_id,
                scope="public",
                section=src.section,
                page_num=src.page_num,
                file_type=new_doc.file_type,
                content=src.content,
                embedding_id=d["chunk_id"],
            )
            for d, src in zip(derived, src_chunks, strict=True)
        )

    await grant_score(
        session, resource.uploader_id, SCORE_UPLOAD_APPROVED,
        "upload_approved", "resource", resource.id, course_id=resource.course_id,
    )
    _notify(
        session, resource.uploader_id, "review_result",
        f"投稿已上架：{resource.title}", link=f"/resources/{resource.id}",
    )
    return new_doc.id


def check_direct_stage(stage: str) -> None:
    """管理员直审的阶段校验（纯函数）：co_review / final 放行；
    precheck → ValueError("precheck")；done → ValueError("done")。"""
    if stage == "precheck":
        raise ValueError("precheck")
    if stage == "done":
        raise ValueError("done")


async def direct_verdict(
    session: AsyncSession,
    admin: User,
    task: ReviewTask,
    verdict: str,
    comment: str | None,
) -> tuple[int, int | None]:
    """管理员直审（v0.5）：协审中/待终审任务由管理员直接裁决，行为与终审完全一致。

    非 final 阶段先推进到 final 再复用 final_verdict（终审记录由 final_verdict 写入，
    不重复插）。ValueError 约定：precheck / done 见 check_direct_stage，其余同 final_verdict。
    """
    check_direct_stage(task.stage)
    task.stage = "final"
    return await final_verdict(session, admin, task, verdict, comment)
