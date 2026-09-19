"""资料接口（§12.1）：上传个人库（异步解析）、解析状态轮询、溯源视图、投稿公共库。"""

import hashlib
import re
from typing import Annotated

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.identity.rbac import get_current_user
from app.storage import object_store
from app.storage.db import get_db
from app.storage.models import Chapter, Chunk, Course, Document, User

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 单文件 ≤ 50MB

_EXT_TO_FILE_TYPE = {
    ".pdf": "pdf_textbook",
    ".ppt": "ppt",
    ".pptx": "ppt",
    ".doc": "word",
    ".docx": "word",
    ".md": "markdown",
    ".markdown": "markdown",
}

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """模块级缓存的 ARQ 连接池（解析任务入队用）。"""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(
            RedisSettings.from_dsn(get_settings().redis_url)
        )
    return _arq_pool


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

    pool = await _get_arq_pool()
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
    """文档切块列表（溯源视图用）：仅本人/管理员可见。"""
    doc = await _get_owned_document(db, document_id, user)
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
