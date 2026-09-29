"""资料接口（§12.1）：上传个人库（异步解析）、解析状态轮询、溯源视图、投稿公共库。"""

import hashlib
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user
from app.moderation.workflow import create_submission
from app.storage import object_store
from app.storage.db import get_db
from app.storage.models import Chapter, Chunk, Course, Document, User
from app.workers.pool import get_arq_pool

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 单文件 ≤ 50MB

_EXT_TO_FILE_TYPE = {
    ".pdf": "pdf_textbook",
    ".pptx": "ppt",
    ".docx": "word",
    ".md": "markdown",
    ".markdown": "markdown",
}

# 旧版 Office 二进制格式（OLE2）python-docx/pptx 无法解析，上传即明确拒绝
_LEGACY_OFFICE_EXT = {".doc", ".ppt"}


def _safe_filename(name: str) -> str:
    """文件名安全化：保留中英文/数字/.-_，其余替换为下划线。"""
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", name).strip("._")
    return cleaned or "unnamed"


async def _get_owned_document(
    db: AsyncSession, document_id: int, user: User
) -> Document:
    """文档归属检查：仅本人/管理员可见，越权 404（§3.2）。"""
    doc = await db.get(Document, document_id)
    if doc is None or (doc.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404, detail="Not Found")
    return doc


@router.post("", status_code=202, include_in_schema=False)  # 兼容无尾斜杠（nginx 反代下 307 会丢失端口）
@router.post("/", status_code=202)
async def upload_document(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile,
    course_id: Annotated[int, Form()],
    chapter_id: Annotated[int | None, Form()] = None,
):
    """上传资料到个人库：存对象存储 → 建档 → ARQ 入队异步解析（202 即返，轮询状态）。

    chapter_id 为 v0.4 人工挂载章节预留参数；v0.3 章节由 tree_builder 自动构建。
    """
    course = await db.get(Course, course_id)
    if course is None or course.scope != "personal" or course.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not Found")

    original_name = file.filename or "unnamed"
    suffix = "." + original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if suffix in _LEGACY_OFFICE_EXT:
        raise HTTPException(
            status_code=422,
            detail=f"旧版 Office 二进制格式（{suffix}）暂不支持，请另存为 .docx/.pptx 后上传",
        )
    file_type = _EXT_TO_FILE_TYPE.get(suffix)
    if file_type is None:
        raise HTTPException(status_code=422, detail=f"不支持的文件类型: {suffix or '无扩展名'}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="空文件")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=422, detail="文件超过 50MB 限制")

    md5 = hashlib.md5(data).hexdigest()
    existing = (
        await db.execute(
            select(Document).where(Document.owner_id == user.id, Document.md5 == md5)
        )
    ).scalars().first()
    if existing is not None:
        if existing.status == "failed":
            # 解析失败的文档允许重传重试：重置状态并重新入队（同一文件不重复占用存储）
            existing.status = "parsing"
            await db.commit()
            pool = await get_arq_pool()
            await pool.enqueue_job("parse_document", existing.id)
            return {
                "document_id": existing.id,
                "status": "parsing",
                "status_url": f"/api/documents/{existing.id}/status",
                "dedup": True,
                "retry": True,
            }
        return {"document_id": existing.id, "status": existing.status, "dedup": True}

    storage_key = f"personal/{user.id}/{md5}/{_safe_filename(original_name)}"
    await object_store.put_object(
        data, storage_key, file.content_type or "application/octet-stream"
    )
    doc = Document(
        course_id=course.id,
        owner_id=user.id,
        scope="personal",
        file_name=original_name,
        file_type=file_type,
        storage_key=storage_key,
        md5=md5,
        status="parsing",
    )
    db.add(doc)
    await db.commit()

    pool = await get_arq_pool()
    await pool.enqueue_job("parse_document", doc.id)
    return {
        "document_id": doc.id,
        "status": "parsing",
        "status_url": f"/api/documents/{doc.id}/status",
    }


@router.get("/{document_id}/status")
async def get_document_status(
    document_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """解析任务状态轮询（§A1：上传即返回，页面轮询更新进度）。"""
    doc = await _get_owned_document(db, document_id, user)
    chunk_count = (
        await db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id)
        )
    ).scalar_one()
    return {
        "document_id": doc.id,
        "file_name": doc.file_name,
        "status": doc.status,
        "chunk_count": chunk_count,
    }


@router.get("/{document_id}/chunks")
async def get_document_chunks(
    document_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """文档切块列表（溯源视图用）：public 文档任何登录用户可见，个人文档仅本人/管理员。"""
    doc = await db.get(Document, document_id)
    if doc is None or (
        doc.scope != "public" and doc.owner_id != user.id and user.role != "admin"
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    rows = (
        await db.execute(
            select(Chunk, Chapter.title)
            .outerjoin(Chapter, Chapter.id == Chunk.chapter_id)
            .where(Chunk.document_id == doc.id)
            .order_by(Chunk.id)
        )
    ).all()
    return {
        "document_id": doc.id,
        "items": [
            {
                "chunk_id": chunk.chunk_id,
                "section": chunk.section,
                "page_num": chunk.page_num,
                "content": chunk.content,
                "chapter_id": chunk.chapter_id,
                "chapter_title": chapter_title,
            }
            for chunk, chapter_title in rows
        ],
    }


class SubmitRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    course_id: int
    chapter_id: int | None = None


@router.post("/{document_id}/submit", status_code=201)
async def submit_to_public(
    document_id: int,
    payload: SubmitRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """投稿公共库（§12.1）：仅本人个人库已解析文档可投（admin 也不可代投），
    建档后 ARQ 入队自动预检；已驳回的投稿重投复用原 resource（resubmit=True）。"""
    doc = await db.get(Document, document_id)
    if doc is None or doc.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    if doc.scope != "personal":
        raise HTTPException(status_code=422, detail="仅个人库资料可投稿")
    if doc.status != "parsed":
        raise HTTPException(status_code=422, detail="解析完成后才能投稿")
    course = await db.get(Course, payload.course_id)
    if course is None or course.scope != "public" or course.status != "active":
        raise HTTPException(status_code=404, detail="Not Found")
    chapter = None
    if payload.chapter_id is not None:
        chapter = await db.get(Chapter, payload.chapter_id)
        if chapter is None or chapter.course_id != course.id:
            raise HTTPException(status_code=422, detail="章节不存在或不属于目标课程")
    try:
        resource, task, is_resubmit = await create_submission(
            db, user, doc, course, chapter, payload.title, payload.description
        )
    except ValueError as exc:
        if str(exc) == "duplicate":
            raise HTTPException(status_code=422, detail="该资料已在审核中") from exc
        if str(exc) == "approved":
            raise HTTPException(status_code=422, detail="该资料已上架") from exc
        raise
    await db.commit()
    pool = await get_arq_pool()
    await pool.enqueue_job("precheck_submission", task.id)
    return {
        "resource_id": resource.id,
        "review_status": "pending",
        "review_task_id": task.id,
        "resubmit": is_resubmit,
    }
