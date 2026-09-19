"""PPT 解析器（python-pptx）：每页标题、正文、备注、页码（模块 A1）。

每页幻灯片输出一个块：页标题（title placeholder，缺省取首行）+ 正文 + 备注，
page_num 为页序（从 1 开始），level=1（页即章节级天然边界）。
"""

import io

from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER

from app.core.parser.base import BaseParser, ParsedBlock


def _slide_title(slide) -> str | None:
    """页标题：优先 title placeholder，其次居中标题占位符，最后取正文首行。"""
    if slide.shapes.title is not None:
        text = slide.shapes.title.text.strip()
        if text:
            return text
    for shape in slide.placeholders:
        if shape.placeholder_format.type == PP_PLACEHOLDER.CENTER_TITLE:
            text = shape.text.strip()
            if text:
                return text
    return None


class PptParser(BaseParser):
    file_type = "ppt"

    def parse(self, data: bytes) -> list[ParsedBlock]:
        prs = Presentation(io.BytesIO(data))
        blocks: list[ParsedBlock] = []
        for idx, slide in enumerate(prs.slides, start=1):
            title = _slide_title(slide)
            title_shape_id = (
                slide.shapes.title.shape_id if slide.shapes.title is not None else None
            )
            body_lines: list[str] = []
            for shape in slide.shapes:
                if not shape.has_text_frame or shape.shape_id == title_shape_id:
                    continue
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        body_lines.append(line)
            if title is None and body_lines:
                title, body_lines = body_lines[0], body_lines[1:]
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    body_lines.append(f"备注：{notes}")
            if title is None and not body_lines:
                continue  # 空白页跳过
            blocks.append(
                ParsedBlock(
                    title=title,
                    content="\n".join(body_lines),
                    level=1,
                    page_num=idx,
                )
            )
        return blocks
