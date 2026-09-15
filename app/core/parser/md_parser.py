"""Markdown 解析器（markdown + 正则）：标题层级直接映射章节树（模块 A1）。"""

from app.core.parser.base import BaseParser


class MarkdownParser(BaseParser):
    file_type = "markdown"

    # TODO(v0.3)
