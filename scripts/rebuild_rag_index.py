"""离线重建 RAG 索引；默认 dry-run，真实写入需要 --yes。

在应用容器运行：python scripts/rebuild_rag_index.py --dry-run
备份包含 chunks 与双 Collection 的 id/vector/metadata/document；失败自动恢复。
"""

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.core.chunking.semantic_splitter import split_blocks
from app.core.context import INDEX_VERSION
from app.core.embeddings import get_embedding
from app.core.indexing import index_document_chunks
from app.core.parser import parse_by_file_type
from app.core.retrieval.fine import invalidate_bm25
from app.core.structure import tree_builder
from app.storage import object_store
from app.storage.db import SessionLocal
from app.storage.models import Chapter, Chunk, Document
from app.storage.vector_store import (
    _COLLECTIONS_PATH,
    COLLECTION_PERSONAL,
    COLLECTION_PUBLIC,
    get_vector_store,
)

logger = logging.getLogger(__name__)
_BATCH = 64
_CHUNK_FIELDS = (
    "chunk_id", "document_id", "course_id", "chapter_id", "owner_id", "scope",
    "section", "page_num", "file_type", "content", "embedding_id",
)
_CHAPTER_FIELDS = ("id", "course_id", "title", "parent_id", "order_idx", "summary_vector_id")


def _chunk_row(chunk: Chunk) -> dict:
    return {field: getattr(chunk, field) for field in _CHUNK_FIELDS}


def _chapter_row(chapter: Chapter) -> dict:
    return {field: getattr(chapter, field) for field in _CHAPTER_FIELDS}


def _cache_keys(rows: list[dict]) -> set[tuple[int, str, int]]:
    return {(row["course_id"], row["scope"], row["owner_id"]) for row in rows}


async def _invalidate_rows(rows: list[dict]) -> None:
    for course_id, scope, owner_id in sorted(_cache_keys(rows)):
        await invalidate_bm25(course_id, scope, owner_id)


async def _collection_count(store, collection: str) -> int:
    response = await store._client.get(f"{_COLLECTIONS_PATH}/{collection}/count", timeout=30)
    store._check(response, "count", collection)
    return int(response.json())


async def _read_collection(store, name: str, with_vectors: bool) -> dict:
    collection = await store.get_or_create_collection(name)
    # Chroma 1.0.15 集合详情 GET 只接受集合名（id 会 404）；子资源仍按 id
    response = await store._client.get(f"{_COLLECTIONS_PATH}/{name}", timeout=30)
    store._check(response, "metadata", name)
    metadata = response.json().get("metadata") or {}
    count = await _collection_count(store, collection)
    result = {"name": name, "metadata": metadata, "count": count, "ids": []}
    if not with_vectors or not count:
        return result
    include = ["embeddings", "metadatas", "documents"]
    response = await store._client.post(
        f"{_COLLECTIONS_PATH}/{collection}/get", json={"include": include}, timeout=120
    )
    store._check(response, "backup", name)
    data = response.json()
    if len(data.get("ids", [])) != count:
        raise RuntimeError(f"{name} 备份数量不一致：{len(data.get('ids', []))} != {count}")
    result.update(data)
    return result


async def _set_metadata(store, name: str, metadata: dict) -> None:
    # Chroma 1.0.15：GET 集合详情按名称，PUT 更新元数据按 UUID
    collection = await store.get_or_create_collection(name)
    response = await store._client.put(
        f"{_COLLECTIONS_PATH}/{collection}", json={"new_metadata": metadata}, timeout=30
    )
    store._check(response, "metadata_update", name)


async def _clear_collection(store, name: str) -> None:
    collection = await store.get_or_create_collection(name)
    response = await store._client.post(
        f"{_COLLECTIONS_PATH}/{collection}/get", json={"include": []}, timeout=60
    )
    store._check(response, "clear_scan", name)
    ids = response.json().get("ids", [])
    for start in range(0, len(ids), _BATCH):
        response = await store._client.post(
            f"{_COLLECTIONS_PATH}/{collection}/delete",
            json={"ids": ids[start : start + _BATCH]}, timeout=60,
        )
        store._check(response, "clear", name)


async def _restore_collection(store, backup: dict) -> None:
    name = backup["name"]
    await _clear_collection(store, name)
    ids = backup.get("ids", [])
    for start in range(0, len(ids), _BATCH):
        end = start + _BATCH
        await store.upsert(
            await store.get_or_create_collection(name),
            ids=ids[start:end],
            embeddings=backup["embeddings"][start:end],
            metadatas=backup["metadatas"][start:end],
            documents=backup["documents"][start:end],
        )
    await _set_metadata(store, name, backup.get("metadata") or {})
    if await _collection_count(store, await store.get_or_create_collection(name)) != len(ids):
        raise RuntimeError(f"{name} 恢复后数量核对失败")


async def _backup(path: Path) -> dict:
    store = get_vector_store()
    async with SessionLocal() as session:
        chunks = list((await session.execute(select(Chunk))).scalars().all())
        chapters = list((await session.execute(select(Chapter))).scalars().all())
        documents = list((await session.execute(select(Document))).scalars().all())
    payload = {
        "format": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "index_version": INDEX_VERSION,
        "documents": len(documents),
        "chapters": [_chapter_row(chapter) for chapter in chapters],
        "chunks": [_chunk_row(chunk) for chunk in chunks],
        "collections": [
            await _read_collection(store, COLLECTION_PERSONAL, True),
            await _read_collection(store, COLLECTION_PUBLIC, True),
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return payload


async def _restore_database(rows: list[dict], chapter_rows: list[dict] | None = None) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Chunk))
        if chapter_rows is not None:
            await session.execute(delete(Chapter))
            chapters = [Chapter(**{**row, "parent_id": None}) for row in chapter_rows]
            session.add_all(chapters)
            await session.flush()
            for chapter, row in zip(chapters, chapter_rows, strict=True):
                chapter.parent_id = row["parent_id"]
        session.add_all(Chunk(**row) for row in rows)
        await session.commit()


async def _audit_collections(store, rows: list[dict]) -> dict:
    """数量全量核对；每个 Collection 均匀抽取至多 20 条核对权限元数据。"""
    samples = 0
    counts = {}
    for name, scope in ((COLLECTION_PERSONAL, "personal"), (COLLECTION_PUBLIC, "public")):
        expected = {row["chunk_id"]: row for row in rows if row["scope"] == scope}
        collection = await store.get_or_create_collection(name)
        response = await store._client.post(
            f"{_COLLECTIONS_PATH}/{collection}/get",
            json={"include": ["metadatas"]},
            timeout=120,
        )
        store._check(response, "permission_audit", name)
        data = response.json()
        ids = data.get("ids", [])
        if len(ids) != len(expected) or set(ids) != set(expected):
            raise RuntimeError(f"{name} 恢复/重建后 ID 或数量核对失败")
        counts[name] = len(ids)
        metadatas = dict(zip(ids, data.get("metadatas", []), strict=True))
        step = max(1, len(ids) // 20)
        for chunk_id in sorted(ids)[::step][:20]:
            row = expected[chunk_id]
            metadata = metadatas[chunk_id] or {}
            for field in ("document_id", "course_id", "owner_id"):
                if metadata.get(field) != row[field]:
                    raise RuntimeError(f"{name}/{chunk_id} 权限元数据 {field} 不一致")
            if metadata.get("scope", scope) != scope:
                raise RuntimeError(f"{name}/{chunk_id} scope 元数据不一致")
            expected_chapter = row["chapter_id"] if row["chapter_id"] is not None else -1
            if metadata.get("chapter_id", expected_chapter) != expected_chapter:
                raise RuntimeError(f"{name}/{chunk_id} chapter_id 元数据不一致")
            samples += 1
    return {"vectors": counts, "permission_samples": samples}


async def restore(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") not in {1, 2}:
        raise ValueError("不支持的备份格式")
    store = get_vector_store()
    for collection in payload["collections"]:
        await _set_metadata(store, collection["name"], {
            **(collection.get("metadata") or {}), "state": "restoring",
        })
        await _restore_collection(store, collection)
    await _restore_database(payload["chunks"], payload.get("chapters"))
    await _invalidate_rows(payload["chunks"])
    audit = await _audit_collections(store, payload["chunks"])
    print(json.dumps({
        "restored": True,
        "chapters": len(payload.get("chapters", [])),
        "chunks": len(payload["chunks"]),
        **audit,
    }, ensure_ascii=False))


async def dry_run() -> None:
    store = get_vector_store()
    async with SessionLocal() as session:
        documents = list(
            (await session.execute(select(Document).where(Document.status == "parsed")))
            .scalars().all()
        )
        chunk_count = await session.scalar(select(func.count(Chunk.id)))
    collections = [
        await _read_collection(store, COLLECTION_PERSONAL, False),
        await _read_collection(store, COLLECTION_PUBLIC, False),
    ]
    print(json.dumps({
        "dry_run": True,
        "documents": len(documents),
        "database_chunks": chunk_count,
        "collections": [
            {"name": item["name"], "vectors": item["count"], "metadata": item["metadata"]}
            for item in collections
        ],
        "target": {"index_version": INDEX_VERSION, "embedding_model": get_settings().embedding_model},
    }, ensure_ascii=False, indent=2))


async def _restore_backup(store, backup: dict) -> None:
    for collection in backup["collections"]:
        await _set_metadata(store, collection["name"], {
            **(collection.get("metadata") or {}), "state": "restoring",
        })
        await _restore_collection(store, collection)
    await _restore_database(backup["chunks"], backup.get("chapters"))
    await _invalidate_rows(backup["chunks"])


async def rebuild(backup_path: Path) -> None:
    backup = await _backup(backup_path)
    store = get_vector_store()
    for collection in backup["collections"]:
        if collection.get("metadata", {}).get("state", "ready") != "ready":
            raise RuntimeError(f"{collection['name']} 不在 ready 状态，拒绝并发/重复重建")
    new_rows: list[dict] = []
    try:
        for collection in backup["collections"]:
            await _set_metadata(store, collection["name"], {
                **(collection.get("metadata") or {}),
                "state": "rebuilding",
                "target_index_version": INDEX_VERSION,
                "target_embedding_model": get_settings().embedding_model,
            })
        await _clear_collection(store, COLLECTION_PERSONAL)
        await _clear_collection(store, COLLECTION_PUBLIC)
        async with SessionLocal() as session:
            documents = list(
                (await session.execute(select(Document).where(Document.status == "parsed")))
                .scalars().all()
            )
            embedding = get_embedding()
            await session.execute(delete(Chunk))
            for index, doc in enumerate(documents, start=1):
                blocks = parse_by_file_type(doc.file_type, await object_store.get_object(doc.storage_key))
                chapter_ids = await tree_builder.build_tree(session, blocks, doc.course_id)
                chunks = split_blocks(blocks, doc.id, doc.scope, chapter_ids)
                if not chunks:
                    raise RuntimeError(f"document_id={doc.id} 切块为空")
                await index_document_chunks(
                    doc, chunks, embedding=embedding, store=store, batch_size=_BATCH
                )
                rows = [{
                    "chunk_id": chunk["chunk_id"], "document_id": doc.id,
                    "course_id": doc.course_id, "chapter_id": chunk["chapter_id"],
                    "owner_id": doc.owner_id, "scope": doc.scope,
                    "section": chunk["section"], "page_num": chunk["page_num"],
                    "file_type": doc.file_type, "content": chunk["content"],
                    "embedding_id": chunk["chunk_id"],
                } for chunk in chunks]
                new_rows.extend(rows)
                session.add_all(Chunk(**row) for row in rows)
                print(
                    f"progress {index}/{len(documents)} document_id={doc.id} chunks={len(chunks)}",
                    flush=True,
                )
            await session.commit()
        async with SessionLocal() as session:
            actual_chunks = await session.scalar(select(func.count(Chunk.id)))
            document_count = await session.scalar(select(func.count(Document.id)))
        if actual_chunks != len(new_rows) or document_count != backup["documents"]:
            raise RuntimeError("PG 文档/chunk 与向量数量核对失败")
        await _invalidate_rows([*backup["chunks"], *new_rows])
        audit = await _audit_collections(store, new_rows)
        metadata = {
            "index_version": INDEX_VERSION,
            "embedding_model": get_settings().embedding_model,
            "state": "ready",
            "distance_space": "l2_normalized",
        }
        for name in (COLLECTION_PERSONAL, COLLECTION_PUBLIC):
            await _set_metadata(store, name, metadata)
        print(json.dumps({
            "rebuilt": True, "backup": str(backup_path), "chunks": actual_chunks,
            "chapters": len(backup["chapters"]), **audit,
        }, ensure_ascii=False))
    except Exception:
        logger.exception("RAG 重建失败，开始恢复备份")
        await _restore_backup(store, backup)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--restore", type=Path)
    parser.add_argument("--yes", action="store_true", help="确认真实重建/恢复")
    parser.add_argument("--backup", type=Path, default=Path("backups/rag/latest.json"))
    args = parser.parse_args()
    if args.restore:
        if not args.yes:
            parser.error("--restore 需要 --yes")
        asyncio.run(restore(args.restore))
    elif args.dry_run or not args.yes:
        asyncio.run(dry_run())
    else:
        asyncio.run(rebuild(args.backup))


if __name__ == "__main__":
    main()
