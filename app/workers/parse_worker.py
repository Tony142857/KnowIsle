"""解析与向量化任务（模块 A1）：解析 → 目录树 → 语义切块 → 向量化 → Office 转 PDF 预览。

上传即返回，页面轮询 /api/documents/{id}/status 更新进度；
失败任务自动重试 3 次后标记 failed 并通知上传者。
Office 转 PDF 使用镜像内 LibreOffice（soffice --headless --convert-to pdf）。
"""


async def parse_document(ctx: dict, document_id: int) -> None:
    # TODO(v0.3): 对象存储取件 → parser 解析 → tree_builder → semantic_splitter
    #             → embeddings → Chroma 写入（按 scope 分 Collection）→ preview_key 回填
    raise NotImplementedError(f"解析流水线尚未实现（v0.3 迭代交付）: document_id={document_id}")
