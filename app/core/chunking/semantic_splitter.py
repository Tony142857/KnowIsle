"""语义化切块（模块 A1）。

天然边界（标题/段落）优先；过长块按中文句读（。！？；\\n）切分，目标块长 400-600 字符，
相邻块保留约 60 字符 overlap，过短的相邻块合并，不使用固定长度一刀切。

chunk_id 业务 ID 由本模块生成，格式 per_d{document_id}_{seq:05d}（personal）/
pub_d{document_id}_{seq:05d}（public）：同一文档重解析结果一致（幂等），全局唯一。
"""

import re

from app.core.parser.base import ParsedBlock

TARGET_MIN = 400  # 目标块长下限
TARGET_MAX = 600  # 目标块长上限
OVERLAP = 60  # 相邻块重叠字符数（约 10%-15%）
MERGE_MIN = 120  # 短于此长度的相邻块尝试合并

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")


def make_chunk_id(scope: str, document_id: int, seq: int) -> str:
    prefix = "per" if scope == "personal" else "pub"
    return f"{prefix}_d{document_id}_{seq:05d}"


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _split_long_text(text: str, target_max: int = TARGET_MAX, overlap: int = OVERLAP) -> list[str]:
    """按句读把长文本切成 target_max 内的片段，相邻片段尾部重叠 overlap 字符。"""
    sentences = _split_sentences(text)
    if not sentences:
        return []
    pieces: list[str] = []
    current = ""
    for sent in sentences:
        if current and len(current) + len(sent) > target_max:
            pieces.append(current)
            tail = current[-overlap:] if overlap < len(current) else current
            current = tail + sent
        else:
            current += sent
    if current.strip():
        pieces.append(current)
    return pieces


def split_blocks(
    blocks: list[ParsedBlock],
    document_id: int,
    scope: str,
    chapter_ids: list[int | None] | None = None,
) -> list[dict]:
    """把解析块切成检索用 chunk。

    chapter_ids：tree_builder 为每个块解析出的章节 ID（与 blocks 等长，可为 None）；
    缺省时 chunk 不带章节归属（chapter_id=None）。

    返回 [{chunk_id, content, section, page_num, chapter_id}]，顺序即文档顺序。
    """
    if chapter_ids is not None and len(chapter_ids) != len(blocks):
        raise ValueError("chapter_ids 与 blocks 长度不一致")

    # 1) 按天然边界聚段：标题块切分上下文，正文归入最近的标题
    segments: list[dict] = []  # {content, section, page_num, chapter_id}
    current: dict | None = None
    section: str | None = None
    for i, block in enumerate(blocks):
        chapter_id = chapter_ids[i] if chapter_ids else None
        if block.level >= 1 and block.title:
            if current and current["content"].strip():
                segments.append(current)
            if block.level == 1:
                section = None
            else:
                section = block.title
            current = None
        if not block.content.strip():
            continue
        if current is None:
            current = {
                "content": "",
                "section": section,
                "page_num": block.page_num,
                "chapter_id": chapter_id,
            }
        current["content"] += ("\n" if current["content"] else "") + block.content
    if current and current["content"].strip():
        segments.append(current)

    # 2) 长段按句读切分（带 overlap），短段保留
    chunks: list[dict] = []
    seq = 0
    for seg in segments:
        parts = (
            [seg["content"]]
            if len(seg["content"]) <= TARGET_MAX
            else _split_long_text(seg["content"])
        )
        for part in parts:
            seq += 1
            chunks.append(
                {
                    "chunk_id": make_chunk_id(scope, document_id, seq),
                    "content": part,
                    "section": seg["section"],
                    "page_num": seg["page_num"],
                    "chapter_id": seg["chapter_id"],
                }
            )

    # 3) 过短相邻块向前合并（同章节同小节，且合并不超上限）
    merged: list[dict] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk["content"]) < MERGE_MIN
            and chunk["chapter_id"] == merged[-1]["chapter_id"]
            and chunk["section"] == merged[-1]["section"]
            and len(merged[-1]["content"]) + len(chunk["content"]) <= TARGET_MAX
        ):
            merged[-1]["content"] += "\n" + chunk["content"]
            merged[-1]["page_num"] = merged[-1]["page_num"] or chunk["page_num"]
        else:
            merged.append(chunk)
    # 合并后 chunk_id 按最终顺序重排，保证幂等（与内容顺序严格对应）
    for seq, chunk in enumerate(merged, start=1):
        chunk["chunk_id"] = make_chunk_id(scope, document_id, seq)
    return merged
