"""全站搜索（模块 B3，v0.8）：帖子标题/正文与资源标题/描述的 ILIKE 过渡实现，
支持板块/专业/课程过滤；排序为「标题命中优先于正文命中，再按时间」的简单相关度
（SQL case_when），中文分词可后续接 zhparser 扩展（§8.6 注释），不做向量/RAG。
"""

QUERY_MAX_LENGTH = 100
EXCERPT_WIDTH = 120

SEARCH_TYPES = ("all", "post", "resource")


def validate_search_type(search_type: str) -> str:
    """搜索类型校验（纯函数）：all / post / resource，非法 → ValueError。"""
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"未知搜索类型：{search_type}")
    return search_type


def validate_query(q: str) -> str:
    """搜索词校验（纯函数）：去空白后 1~100 字符，空或超长 → ValueError。"""
    q = q.strip()
    if not q:
        raise ValueError("搜索关键词不能为空")
    if len(q) > QUERY_MAX_LENGTH:
        raise ValueError(f"搜索关键词最长 {QUERY_MAX_LENGTH} 字符")
    return q


def make_excerpt(content: str, q: str, width: int = EXCERPT_WIDTH) -> str:
    """搜索摘要（纯函数）：以首个命中（大小写不敏感）为中心截取 width 字，
    首尾截断处加省略号；无命中取前 width 字；内容不足 width 原样返回。"""
    if len(content) <= width:
        return content
    idx = content.lower().find(q.lower()) if q else -1
    if idx < 0:
        return content[:width]
    half = max((width - len(q)) // 2, 0)
    start = max(0, idx - half)
    end = min(len(content), start + width)
    start = max(0, end - width)
    return ("…" if start > 0 else "") + content[start:end] + (
        "…" if end < len(content) else ""
    )
