"""PDF 解析器（pdfplumber 首选 / PyMuPDF 兜底）：按字体大小识别多级标题（模块 A1）。

标题规则：统计全文字号分布取正文字号（出现次数最多者），
明显大于正文（≥1.25 倍）且较短的行视为标题，按字号分 1-2 级（≥1.5 倍为一级）。
pdfplumber 提取为空时用 PyMuPDF(fitz) 兜底纯文本（不做标题识别）。
扫描版 PDF 完全无文字层时抛 ParseError，由上层标记 failed（OCR 兜底见 §22 风险表）。
"""

import io
from collections import Counter

import fitz  # PyMuPDF
import pdfplumber

from app.core.parser.base import BaseParser, ParsedBlock, ParseError

_TITLE_RATIO_L1 = 1.5  # ≥ 正文字号 1.5 倍 → 一级标题
_TITLE_RATIO_L2 = 1.25  # ≥ 正文字号 1.25 倍 → 二级标题
_TITLE_MAX_LEN = 40  # 标题行长度上限（超出按正文处理）


def _extract_lines(page) -> list[tuple[str, float]]:
    """从 pdfplumber 页面对象提取 (行文本, 行最大字号) 列表（按 y 坐标聚行）。"""
    chars = page.chars
    if not chars:
        return []
    lines: dict[int, list[dict]] = {}
    for ch in chars:
        key = round(ch["top"] / 3)  # 3pt 容差内视为同一行
        lines.setdefault(key, []).append(ch)
    result = []
    for key in sorted(lines):
        row = sorted(lines[key], key=lambda c: c["x0"])
        text = "".join(c["text"] for c in row).strip()
        if text:
            result.append((text, max(float(c["size"]) for c in row)))
    return result


class PdfParser(BaseParser):
    file_type = "pdf_textbook"

    def parse(self, data: bytes) -> list[ParsedBlock]:
        blocks = self._parse_pdfplumber(data)
        if not any(b.content.strip() for b in blocks):
            blocks = self._parse_fitz(data)
        if not any(b.content.strip() for b in blocks):
            raise ParseError("PDF 无文字层（疑似扫描件），解析失败")
        return blocks

    def _parse_pdfplumber(self, data: bytes) -> list[ParsedBlock]:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [_extract_lines(page) for page in pdf.pages]
        sizes = [size for lines in pages for text, size in lines if len(text) > _TITLE_MAX_LEN]
        if not sizes:
            sizes = [size for lines in pages for _, size in lines]
        if not sizes:
            return []
        body_size = Counter(round(s, 1) for s in sizes).most_common(1)[0][0]
        blocks: list[ParsedBlock] = []
        for page_num, lines in enumerate(pages, start=1):
            for text, size in lines:
                if len(text) <= _TITLE_MAX_LEN and size >= body_size * _TITLE_RATIO_L2:
                    level = 1 if size >= body_size * _TITLE_RATIO_L1 else 2
                    blocks.append(
                        ParsedBlock(title=text, content="", level=level, page_num=page_num)
                    )
                else:
                    blocks.append(
                        ParsedBlock(title=None, content=text, level=0, page_num=page_num)
                    )
        return blocks

    def _parse_fitz(self, data: bytes) -> list[ParsedBlock]:
        blocks: list[ParsedBlock] = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page_num, page in enumerate(doc, start=1):
                text = page.get_text().strip()
                if text:
                    blocks.append(
                        ParsedBlock(title=None, content=text, level=0, page_num=page_num)
                    )
        return blocks
