"""对象存储封装（§7.2）：SeaweedFS（S3 兼容），原始文件 + 预览 PDF 产物统一存储。

SDK 使用 aioboto3（异步 S3 客户端，符合全异步生态），
endpoint 指向 compose 中的 seaweedfs 服务。
"""

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

    # TODO(v0.3): put_object / presigned_url（预览与下载）/ delete_object 等封装
