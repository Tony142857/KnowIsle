"""RAG 问答主流水线（模块 A3）：双库 scope 路由 → 粗召回 → 精排 → 生成 → 溯源。

检索范围路由（§5.3）：
- personal：仅个人库（owner_id 过滤）
- public：仅公共库（scope='public' 过滤）
- mixed：双库并行检索，个人库结果加权 1.2 后 RRF 融合
"""

# TODO(v0.3): async def retrieve(query, user_id, course_id,
#                                scope: Literal["personal", "public", "mixed"]) -> list[Chunk]
# TODO(v0.3): async def answer_question(...) -> AsyncIterator[SSE 事件]
#             额度检查(429) → 检索 → 组装 Prompt → 流式生成 → 引用映射 → qa_logs 落库
