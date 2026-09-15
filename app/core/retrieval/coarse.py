"""章节级粗召回（模块 A2 第一级）。

章节摘要向量相似度召回 Top 3-5 章节，把范围从整门课缩小到几章；
章节摘要为空（summary_vector_id 未回填）时降级为全课程范围精排（§8.3）。
"""

# TODO(v0.3): async def coarse_recall(query_vector, course_id, scope, top_k=5) -> list[Chapter]
