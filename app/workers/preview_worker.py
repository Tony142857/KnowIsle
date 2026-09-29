"""Office→PDF 预览转换任务（模块 B2 配套）：soffice headless 转换并回填 preview_key。

soffice（LibreOffice）仅在应用镜像内可用（Dockerfile 已含），本地环境不可直接运行；
预览与下载一律由 app 流式转发对象存储字节，不用预签名 URL。
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from app.storage import object_store
from app.storage.db import SessionLocal
from app.storage.models import Document

logger = logging.getLogger(__name__)

_SOFFICE_TIMEOUT = 300  # 单文件转换超时（秒）
_SUFFIX = {"word": ".docx", "ppt": ".pptx"}


async def convert_preview(ctx: dict, document_id: int) -> None:
    """ARQ 任务：原始 Office 文件 → soffice 转 PDF → 对象存储 → 回填 doc.preview_key。

    非 word/ppt 文档无需转换，直接返回（pdf_textbook/markdown 预览用原文件）。
    """
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.file_type not in _SUFFIX:
            return
        data = await object_store.get_object(doc.storage_key)
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / f"source{_SUFFIX[doc.file_type]}"
            src.write_bytes(data)
            proc = await asyncio.create_subprocess_exec(
                "soffice", "--headless", "--convert-to", "pdf",
                "--outdir", tmpdir, str(src),
            )
            await asyncio.wait_for(proc.communicate(), timeout=_SOFFICE_TIMEOUT)
            pdf_path = src.with_suffix(".pdf")
            if proc.returncode != 0 or not pdf_path.exists():
                raise RuntimeError(
                    f"soffice 预览转换失败: document_id={doc.id} returncode={proc.returncode}"
                )
            pdf = pdf_path.read_bytes()
        key = f"preview/{doc.id}/{doc.md5}.pdf"
        await object_store.put_object(pdf, key, "application/pdf")
        doc.preview_key = key
        await session.commit()
    logger.info("预览转换完成 document_id=%s preview_key=%s", document_id, key)
