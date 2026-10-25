"""结构感知切块：标题边界、短段合并、确定性句读/字符二级切分。

数据库原文只放在 content；章节路径派生到 embedding_text / prompt_text，不污染原文。
同一文档、章节映射和参数产生相同顺序与稳定业务 ID。
"""

import re

from app.core.context import embedding_text, prompt_text
from app.core.parser.base import ParsedBlock

TARGET_MIN = 400
TARGET_MAX = 600
OVERLAP = 60
MERGE_MIN = 120

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?\n])")


def make_chunk_id(scope: str, document_id: int, seq: int) -> str:
    prefix = "per" if scope == "personal" else "pub"
    return f"{prefix}_d{document_id}_{seq:05d}"


def _split_sentences(text: str) -> list[str]:
    return [part for part in _SENT_SPLIT_RE.split(text) if part.strip()]


def _hard_windows(text: str, target_max: int, overlap: int) -> list[str]:
    if len(text) <= target_max:
        return [text] if text.strip() else []
    if not 0 <= overlap < target_max:
        raise ValueError("overlap 必须小于硬上限且非负")
    step = target_max - overlap
    return [text[start : start + target_max] for start in range(0, len(text), step)]


def _split_long_text(text: str, target_max: int = TARGET_MAX, overlap: int = OVERLAP) -> list[str]:
    """先按句读组装；超长单句与 overlap 后余量均由硬窗口兜底。"""
    if not 0 <= overlap < target_max:
        raise ValueError("overlap 必须小于硬上限且非负")
    sentences = _split_sentences(text) or [text]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > target_max:
            if current.strip():
                pieces.append(current)
                current = ""
            pieces.extend(_hard_windows(sentence, target_max, overlap))
            continue
        candidate = current + sentence
        if current and len(candidate) > target_max:
            pieces.append(current)
            tail = current[-overlap:] if overlap else ""
            if len(tail) + len(sentence) > target_max:
                tail = ""
            current = tail + sentence
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces


def _segment_blocks(
    blocks: list[ParsedBlock], chapter_ids: list[int | None] | None
) -> list[dict]:
    segments: list[dict] = []
    current: dict | None = None
    chapter_title: str | None = None
    section: str | None = None
    for i, block in enumerate(blocks):
        chapter_id = chapter_ids[i] if chapter_ids else None
        if block.level >= 1 and block.title:
            if current and current["content"].strip():
                segments.append(current)
            current = None
            if block.level == 1:
                chapter_title = block.title.strip()
                section = None
            else:
                section = block.title.strip()
        content = block.content.strip()
        if not content:
            continue
        boundary = (
            current is None
            or current["chapter_id"] != chapter_id
            or current["chapter_title"] != chapter_title
            or current["section"] != section
        )
        if boundary:
            if current and current["content"].strip():
                segments.append(current)
            current = {
                "content": "",
                "section": section,
                "chapter_title": chapter_title,
                "page_num": block.page_num,
                "chapter_id": chapter_id,
            }
        current["content"] += ("\n" if current["content"] else "") + content
    if current and current["content"].strip():
        segments.append(current)
    return segments


def split_blocks(
    blocks: list[ParsedBlock],
    document_id: int,
    scope: str,
    chapter_ids: list[int | None] | None = None,
) -> list[dict]:
    if chapter_ids is not None and len(chapter_ids) != len(blocks):
        raise ValueError("chapter_ids 与 blocks 长度不一致")
    segments = _segment_blocks(blocks, chapter_ids)

    chunks: list[dict] = []
    for segment in segments:
        parts = (
            [segment["content"]]
            if len(segment["content"]) <= TARGET_MAX
            else _split_long_text(segment["content"])
        )
        for part in parts:
            chunks.append({**segment, "content": part})

    # 同一结构路径内连续短段合并；前块过短时也吸收后块，TARGET_MIN 因此实际生效。
    merged: list[dict] = []
    for chunk in chunks:
        previous = merged[-1] if merged else None
        same_path = previous and all(
            previous[key] == chunk[key] for key in ("chapter_id", "chapter_title", "section")
        )
        separator_len = 1 if previous else 0
        should_merge = same_path and (
            len(chunk["content"]) < MERGE_MIN
            or len(previous["content"]) < TARGET_MIN
        )
        if should_merge and len(previous["content"]) + separator_len + len(chunk["content"]) <= TARGET_MAX:
            previous["content"] += "\n" + chunk["content"]
            previous["page_num"] = previous["page_num"] or chunk["page_num"]
        else:
            merged.append(chunk)

    result: list[dict] = []
    for seq, chunk in enumerate(merged, start=1):
        item = {
            "chunk_id": make_chunk_id(scope, document_id, seq),
            "content": chunk["content"],
            "section": chunk["section"],
            "chapter_title": chunk["chapter_title"],
            "page_num": chunk["page_num"],
            "chapter_id": chunk["chapter_id"],
        }
        item["embedding_text"] = embedding_text(item)
        item["prompt_text"] = prompt_text(item)
        result.append(item)
    return result
