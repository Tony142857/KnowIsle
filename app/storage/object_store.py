"""对象存储封装（§7.2）：SeaweedFS（S3 兼容），原始文件 + 预览 PDF 产物统一存储。

SDK 使用 aioboto3（异步 S3 客户端，符合全异步生态），
endpoint 指向 compose 中的 seaweedfs 服务。
"""

import inspect
from collections.abc import AsyncIterator

import aioboto3

from app.config import get_settings


def get_s3_session() -> aioboto3.Session:
    settings = get_settings()
    return aioboto3.Session(
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
    )


async def ensure_bucket() -> None:
    """启动流程/脚本中调用：确保存储桶存在。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        resp = await s3.list_buckets()
        names = [b["Name"] for b in resp.get("Buckets", [])]
        if settings.s3_bucket not in names:
            await s3.create_bucket(Bucket=settings.s3_bucket)


async def put_object(data: bytes, key: str, content_type: str = "application/octet-stream") -> None:
    """上传对象（原始文件 / 预览 PDF 产物）。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        await s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)


async def get_object(key: str) -> bytes:
    """下载对象内容（解析流水线取原始文件用）。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        resp = await s3.get_object(Bucket=settings.s3_bucket, Key=key)
        return await resp["Body"].read()


async def object_size(key: str) -> int | None:
    """对象字节数（流式响应的 Content-Length 用）；对象不存在时 head_object 照常抛错。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        resp = await s3.head_object(Bucket=settings.s3_bucket, Key=key)
        return resp.get("ContentLength")


async def stream_object(key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """流式下载对象（在线预览/下载转发用，v0.9）：分块迭代 S3 Body，避免全量读入内存。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        resp = await s3.get_object(Bucket=settings.s3_bucket, Key=key)
        async for chunk in resp["Body"].iter_chunks(chunk_size=chunk_size):
            yield chunk


async def presigned_url(key: str, expires: int = 3600) -> str:
    """GET 预签名下载地址（在线预览 / 下载）。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        result = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=expires,
        )
        # aioboto3 新版返回 coroutine，旧版（botocore 同步实现）直接返回 str
        if inspect.isawaitable(result):
            result = await result
        return result


async def delete_object(key: str) -> None:
    """删除对象（资料下架 / 重解析清理用）。"""
    settings = get_settings()
    session = get_s3_session()
    async with session.client("s3", endpoint_url=settings.s3_endpoint_url) as s3:
        await s3.delete_object(Bucket=settings.s3_bucket, Key=key)
