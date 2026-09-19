"""Markdown 解析器（markdown + 正则）：标题层级直接映射章节树（模块 A1）。

按 ATX 标题（#/##/###）切层级：标题块记录 title + level，
正文段落归入最近的标题之下，整体保留文档顺序。
"""

import re

from app.core.parser.base import BaseParser, ParsedBlock

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class MarkdownParser(BaseParser):
    file_type = "markdown"

    def parse(self, data: bytes) -> list[ParsedBlock]:
        text = data.decode("utf-8", errors="replace")
        blocks: list[ParsedBlock] = []
        pending: list[str] = []  # 当前标题下的正文行缓冲

        def flush() -> None:
            content = "\n".join(pending).strip()
            pending.clear()
            if content:
                blocks.append(ParsedBlock(title=None, content=content, level=0))

        for line in text.splitlines():
            m = _HEADING_RE.match(line)
            if m:
                flush()
                blocks.append(
                    ParsedBlock(title=m.group(2).strip(), content="", level=len(m.group(1)))
                )
            else:
                pending.append(line)
        flush()
        return blocks
