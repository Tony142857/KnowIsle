"""Word 解析器（python-docx）：段落与标题样式直接映射章节树（模块 A1）。

样式名为 Heading N / 标题 N（中英文 Word 模板）的段落识别为 N 级标题，
其余非空段落为正文。
"""

import io
import re

from docx import Document as DocxDocument

from app.core.parser.base import BaseParser, ParsedBlock

_HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*(\d+)$", re.IGNORECASE)


class WordParser(BaseParser):
    file_type = "word"

    def parse(self, data: bytes) -> list[ParsedBlock]:
        doc = DocxDocument(io.BytesIO(data))
        blocks: list[ParsedBlock] = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = (para.style.name or "").strip() if para.style else ""
            m = _HEADING_STYLE_RE.match(style_name)
            if m:
                blocks.append(ParsedBlock(title=text, content="", level=int(m.group(1))))
            else:
                blocks.append(ParsedBlock(title=None, content=text, level=0))
        return blocks
