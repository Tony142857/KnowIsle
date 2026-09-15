"""PPT 解析器（python-pptx）：每页标题、正文、备注、页码（模块 A1）。"""

from app.core.parser.base import BaseParser


class PptParser(BaseParser):
    file_type = "ppt"

    # TODO(v0.3): 以页为单位提取标题并归入章节
