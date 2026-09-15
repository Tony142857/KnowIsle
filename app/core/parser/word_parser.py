"""Word 解析器（python-docx）：段落与标题样式直接映射章节树（模块 A1）。"""

from app.core.parser.base import BaseParser


class WordParser(BaseParser):
    file_type = "word"

    # TODO(v0.3)
