"""检索与生成共用的结构上下文，正文原文始终保持独立。"""

from typing import Any

INDEX_VERSION = "rag-structure-v2"


def structure_path(chapter_title: str | None, section: str | None) -> str:
    parts = [part.strip() for part in (chapter_title, section) if part and part.strip()]
    return " > ".join([*parts, "正文"]) if parts else "正文"


def embedding_text(chunk: Any, chapter_title: str | None = None) -> str:
    chapter = chapter_title or _value(chunk, "chapter_title") or _value(chunk, "chapter")
    path = structure_path(chapter, _value(chunk, "section"))
    return f"{path}\n{_value(chunk, 'content') or ''}".strip()


def prompt_text(chunk: Any, chapter_title: str | None = None) -> str:
    chunk_id = _value(chunk, "chunk_id")
    return f"[{chunk_id}]\n{embedding_text(chunk, chapter_title)}"


def _value(chunk: Any, key: str) -> Any:
    return chunk.get(key) if isinstance(chunk, dict) else getattr(chunk, key, None)
