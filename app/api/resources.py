"""公共资源接口（§12.1）：检索、详情与预览、下载（积分校验）、评分、克隆进个人库。

预览/下载一律由 app 流式转发对象存储字节（不用预签名 URL）；
word/ppt 预览用终审后派生 public 副本的 preview_key（soffice 转换产物）；
下载扣贡献分，其中 80% 分成给上传者（v0.5，见 identity/growth.download_share）；
克隆（v0.5）把已上架资源复制成调用者个人库副本，镜像终审派生 public 副本的写法。
"""

import logging
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import any_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import get_embedding
from app.core.retrieval.fine import invalidate_bm25
from app.identity.growth import download_share, grant_score
from app.identity.rbac import get_current_user
from app.storage import object_store
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import (
    Chunk,
    Course,
    Document,
    Resource,
    ResourceRating,
    ReviewTask,
    User,
)
from app.storage.vector_store import COLLECTION_PERSONAL, get_vector_store
from app.workers.parse_worker import _EMBED_BATCH, build_chunk_meta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])


def _resource_item(resource: Resource, file_type: str, nickname: str) -> dict:
    return {
        "id": resource.id,
        "title": resource.title,
        "description": resource.description,
        "course_id": resource.course_id,
        "chapter_id": resource.chapter_id,
        "file_type": file_type,
        "uploader": {"id": resource.uploader_id, "nickname": nickname},
        "rating": float(resource.rating) if resource.rating is not None else None,
        "rating_count": resource.rating_count,
        "download_count": resource.download_count,
        "fav_count": resource.fav_count,
        "download_cost": resource.download_cost,
        "created_at": resource.created_at,
    }


def escape_like(q: str) -> str:
    """LIKE 模式串转义（纯函数）：% _ \\ 均为特殊字符，转义为字面量（配合 escape='\\'）。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _stream_headers(key: str, disposition: str) -> dict:
    """流式响应头：Content-Disposition + Content-Length（head_object 取得到就带）。"""
    headers = {"Content-Disposition": disposition}
    size = await object_store.object_size(key)
    if size is not None:
        headers["Content-Length"] = str(size)
    return headers


async def _get_public_copy(db: AsyncSession, doc: Document) -> Document | None:
    """终审通过时复制派生的 public 副本（storage_key 复用原件，凭此定位）。"""
    return (
        await db.execute(
            select(Document).where(
                Document.scope == "public", Document.storage_key == doc.storage_key
            )
        )
    ).scalars().first()


@router.get("", include_in_schema=False)  # 兼容无尾斜杠（nginx 反代下 307 会丢失端口）
@router.get("/")
async def list_resources(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    course_id: int | None = None,
    chapter_id: int | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """公共库资源检索：仅已上架，按课程/章节过滤、标题模糊搜索，created_at 倒序分页。"""
    conditions = [Resource.review_status == "approved"]
    if course_id is not None:
        conditions.append(Resource.course_id == course_id)
    if chapter_id is not None:
        conditions.append(Resource.chapter_id == chapter_id)
    if q:
        conditions.append(Resource.title.ilike(f"%{escape_like(q)}%", escape="\\"))
    total = (
        await db.execute(select(func.count()).select_from(Resource).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Resource, Document.file_type, User.nickname)
            .join(Document, Document.id == Resource.document_id)
            .join(User, User.id == Resource.uploader_id)
            .where(*conditions)
            .order_by(Resource.created_at.desc(), Resource.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return {
        "total": total,
        "items": [_resource_item(r, ft, nickname) for r, ft, nickname in rows],
    }


async def _get_visible_resource(
    db: AsyncSession, resource_id: int, user: User
) -> Resource:
    """资源可见性：approved 任何登录用户 / 本人投稿 / admin，其余 404（§3.2）。"""
    resource = await db.get(Resource, resource_id)
    if resource is None or (
        resource.review_status != "approved"
        and resource.uploader_id != user.id
        and user.role != "admin"
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    return resource


@router.get("/{resource_id}")
async def get_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """资源详情：approved / 本人投稿 / admin 可见；附预览可用性与本人评分。"""
    resource = await _get_visible_resource(db, resource_id, user)
    doc = await db.get(Document, resource.document_id)
    uploader = await db.get(User, resource.uploader_id)
    my_rating = (
        await db.execute(
            select(ResourceRating.stars).where(
                ResourceRating.resource_id == resource.id,
                ResourceRating.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    preview_available = doc.file_type in ("pdf_textbook", "markdown")
    if not preview_available:
        public_doc = await _get_public_copy(db, doc)
        preview_available = public_doc is not None and public_doc.preview_key is not None
    return {
        **_resource_item(resource, doc.file_type, uploader.nickname),
        "preview_available": preview_available,
        "my_rating": my_rating,
    }


@router.get("/{resource_id}/preview")
async def preview_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """在线预览：inline 流式返回（markdown 原文 / pdf 原文 / word·ppt 转换产物）。

    权限：approved 任何登录用户 / 本人 / admin / 该资源协审或终审阶段的被指派人。
    """
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="Not Found")
    allowed = (
        resource.review_status == "approved"
        or resource.uploader_id == user.id
        or user.role == "admin"
    )
    if not allowed:
        allowed = (
            await db.execute(
                select(ReviewTask.id)
                .where(
                    ReviewTask.resource_id == resource.id,
                    ReviewTask.stage.in_(["co_review", "final"]),
                    user.id == any_(ReviewTask.assignee_ids),
                )
                .limit(1)
            )
        ).first() is not None
    if not allowed:
        raise HTTPException(status_code=404, detail="Not Found")

    doc = await db.get(Document, resource.document_id)
    if doc.file_type in ("markdown", "pdf_textbook"):
        media_type = (
            "text/markdown; charset=utf-8"
            if doc.file_type == "markdown"
            else "application/pdf"
        )
        return StreamingResponse(
            object_store.stream_object(doc.storage_key),
            media_type=media_type,
            headers=await _stream_headers(doc.storage_key, "inline"),
        )
    public_doc = await _get_public_copy(db, doc)
    preview_key = public_doc.preview_key if public_doc is not None else None
    if not preview_key:
        raise HTTPException(status_code=409, detail="预览生成中")
    return StreamingResponse(
        object_store.stream_object(preview_key),
        media_type="application/pdf",
        headers=await _stream_headers(preview_key, "inline"),
    )


@router.get("/{resource_id}/download")
async def download_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """下载原件：仅已上架（本人/admin 同）；计分与流式返回在同一事务前完成。

    download_cost>0：扣下载者贡献分，其中 80%（download_share）分成给上传者；
    download_cost==0：他人下载给上传者「被下载 +1」奖励，同一用户对同一资源
    仅首次计入（Redis 去重），上传者每日上限 20 分（Redis 故障跳过奖励不阻塞下载）。
    """
    resource = await db.get(Resource, resource_id)
    if resource is None or resource.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    if resource.download_cost > 0:
        # 行锁串行化并发下载的扣分判定，防并发透支（镜像 posts.py 悬赏托管写法）：
        # user 已被 get_current_user 加载进身份映射，必须 populate_existing=True
        # 强制发出 SELECT ... FOR UPDATE，否则 session.get 直接命中缓存、行锁不生效；
        # grant_score 经 session.get(User, ...) 复用同一身份映射实例，锁持续有效
        locked_user = await db.get(
            User, user.id, with_for_update=True, populate_existing=True
        )
        if locked_user.score < resource.download_cost:
            raise HTTPException(status_code=422, detail="贡献分不足")
        await grant_score(
            db, user.id, -resource.download_cost, "download_cost", "resource", resource.id
        )
        if resource.uploader_id != user.id:
            share = download_share(resource.download_cost)
            if share > 0:
                await grant_score(
                    db, resource.uploader_id, share, "download_share",
                    "resource", resource.id, course_id=resource.course_id,
                )
    elif resource.uploader_id != user.id:
        try:
            redis = get_redis()
            first = await redis.sadd(f"knowisle:dled:{resource.id}", str(user.id))
            if first:
                day = datetime.now(UTC).strftime("%Y%m%d")
                cap_key = f"knowisle:dlcap:{resource.uploader_id}:{day}"
                count = await redis.incr(cap_key)
                if count == 1:
                    await redis.expire(cap_key, 172800)  # 2 天，覆盖跨日边界
                if count <= 20:
                    await grant_score(
                        db, resource.uploader_id, 1, "download_reward",
                        "resource", resource.id, course_id=resource.course_id,
                    )
        except Exception:
            logger.warning(
                "免费下载奖励跳过（Redis 不可用）resource_id=%s", resource.id, exc_info=True
            )
    resource.download_count += 1
    await db.commit()
    doc = await db.get(Document, resource.document_id)
    return StreamingResponse(
        object_store.stream_object(doc.storage_key),
        media_type="application/octet-stream",
        headers=await _stream_headers(
            doc.storage_key, f"attachment; filename*=utf-8''{quote(doc.file_name)}"
        ),
    )


class RatingRequest(BaseModel):
    stars: int = Field(ge=1, le=5)


@router.post("/{resource_id}/rating")
async def rate_resource(
    resource_id: int,
    payload: RatingRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """资源评分（1-5 星，一人一评，重复提交覆盖）；不能给自己的资源评分。"""
    resource = await db.get(Resource, resource_id)
    if resource is None or resource.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    if resource.uploader_id == user.id:
        raise HTTPException(status_code=422, detail="不能给自己的资源评分")
    existing = (
        await db.execute(
            select(ResourceRating).where(
                ResourceRating.resource_id == resource.id,
                ResourceRating.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            ResourceRating(
                user_id=user.id, resource_id=resource.id, stars=payload.stars
            )
        )
    else:
        existing.stars = payload.stars
    await db.flush()
    avg, count = (
        await db.execute(
            select(func.avg(ResourceRating.stars), func.count()).where(
                ResourceRating.resource_id == resource.id
            )
        )
    ).one()
    resource.rating = round(float(avg), 2)
    resource.rating_count = count
    await db.commit()
    return {
        "rating": float(resource.rating),
        "rating_count": resource.rating_count,
        "my_rating": payload.stars,
    }


# ---------------------------------------------------------------------------
# 资源克隆（v0.5）：公共库已上架资源 → 调用者个人库副本
# ---------------------------------------------------------------------------

CLONE_COURSE_NAME = "公共库克隆"  # 未指定 course_id 时的默认目标课程


def make_personal_chunk_id(doc_id: int, seq: int) -> str:
    """克隆副本的 chunk 业务 ID（与 workflow.make_public_chunk_id 编号约定镜像）。"""
    return f"per_d{doc_id}_{seq:05d}"


def check_clone_course(course: Course | None, user_id: int) -> None:
    """克隆目标课程归属校验（纯函数）：不存在 / 非 personal / 非本人 → ValueError("not_found")。"""
    if course is None or course.scope != "personal" or course.owner_id != user_id:
        raise ValueError("not_found")


def decide_clone_hit(existing: Document | None) -> dict | None:
    """幂等判定（纯函数）：本人已有同 storage_key 文档 → 幂等响应体，否则 None。"""
    if existing is None:
        return None
    return {"document_id": existing.id, "course_id": existing.course_id, "cloned": False}


def build_cloned_chunk(
    src: Chunk, new_doc_id: int, course_id: int, owner_id: int, seq: int
) -> dict:
    """克隆 chunk 字段映射（纯函数）：chunk_id 按 seq 重编号（保持原顺序），
    chapter_id 置空（公共课程章节对个人课程无意义），其余字段照抄源 chunk。"""
    chunk_id = make_personal_chunk_id(new_doc_id, seq)
    return {
        "chunk_id": chunk_id,
        "document_id": new_doc_id,
        "course_id": course_id,
        "chapter_id": None,
        "owner_id": owner_id,
        "scope": "personal",
        "section": src.section,
        "page_num": src.page_num,
        "file_type": src.file_type,
        "content": src.content,
        "embedding_id": chunk_id,
    }


class CloneRequest(BaseModel):
    course_id: int | None = None


@router.post("/{resource_id}/clone")
async def clone_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    payload: CloneRequest | None = None,
):
    """克隆已上架资源进本人个人库（v0.5）。

    - 资源须 review_status='approved'（否则 404）；course_id 给了须为本人
      personal 课程（否则 404），没给则取/建本人「公共库克隆」课程（semester_id=None）
    - 幂等：本人已有同 storage_key 文档 → 200 {document_id, course_id, cloned: false}
    - 否则同步复制（镜像 moderation.workflow._approve 的派生风格，方向相反）：
      新 Document（scope=personal、沿用存储键/预览键/md5）→ chunks 重编号
      per_d{新id}_{seq:05d} → 批量重 embed 写 chunks_personal（metadata 复用
      parse_worker.build_chunk_meta），commit 后 201 cloned=true
    """
    resource = await db.get(Resource, resource_id)
    if resource is None or resource.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    src_doc = await db.get(Document, resource.document_id)

    course_id = payload.course_id if payload is not None else None
    course: Course | None = None
    if course_id is not None:
        course = await db.get(Course, course_id)
        try:
            check_clone_course(course, user.id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not Found") from None

    existing = (
        await db.execute(
            select(Document).where(
                Document.owner_id == user.id,
                Document.storage_key == src_doc.storage_key,
            )
        )
    ).scalars().first()
    hit = decide_clone_hit(existing)
    if hit is not None:
        return hit

    if course is None:
        course = (
            await db.execute(
                select(Course).where(
                    Course.scope == "personal",
                    Course.owner_id == user.id,
                    Course.name == CLONE_COURSE_NAME,
                )
            )
        ).scalars().first()
        if course is None:
            course = Course(
                scope="personal",
                owner_id=user.id,
                semester_id=None,
                name=CLONE_COURSE_NAME,
                status="active",
            )
            db.add(course)
            await db.flush()

    preview_key = src_doc.preview_key
    if not preview_key:
        # word/ppt 的预览产物挂在终审派生的 public 副本上，原件无 preview_key
        public_copy = await _get_public_copy(db, src_doc)
        preview_key = public_copy.preview_key if public_copy is not None else None

    new_doc = Document(
        course_id=course.id,
        owner_id=user.id,
        scope="personal",
        file_name=src_doc.file_name,
        file_type=src_doc.file_type,
        storage_key=src_doc.storage_key,
        preview_key=preview_key,
        md5=src_doc.md5,
        status="parsed",
    )
    db.add(new_doc)
    await db.flush()

    src_chunks = (
        await db.execute(
            select(Chunk).where(Chunk.document_id == src_doc.id).order_by(Chunk.id)
        )
    ).scalars().all()
    derived = [
        build_cloned_chunk(src, new_doc.id, course.id, user.id, seq)
        for seq, src in enumerate(src_chunks, start=1)
    ]
    if derived:
        embedding = get_embedding()
        vectors: list[list[float]] = []
        for i in range(0, len(derived), _EMBED_BATCH):
            vectors.extend(
                await embedding.embed([d["content"] for d in derived[i : i + _EMBED_BATCH]])
            )
        store = get_vector_store()
        collection = await store.get_or_create_collection(COLLECTION_PERSONAL)
        await store.upsert(
            collection,
            ids=[d["chunk_id"] for d in derived],
            embeddings=vectors,
            metadatas=[build_chunk_meta(new_doc, d) for d in derived],
            documents=[d["content"] for d in derived],
        )
        db.add_all(Chunk(**d) for d in derived)
    await db.commit()
    if derived:
        # 克隆 chunks 已落库：失效目标个人课程 BM25 语料缓存（v0.9）
        invalidate_bm25(course.id, "personal", user.id)
    return JSONResponse(
        {"document_id": new_doc.id, "course_id": course.id, "cloned": True},
        status_code=201,
    )
