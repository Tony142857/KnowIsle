"""PDF 解析器（pdfplumber 首选 / PyMuPDF 兜底）：按字体大小 + 缩进识别多级标题（模块 A1）。

扫描版 PDF 无法提取文字时预留 OCR（PaddleOCR）兜底（§22 风险表）。
"""

from app.core.parser.base import BaseParser


class PdfParser(BaseParser):
    file_type = "pdf_textbook"

    # TODO(v0.3): 字体/字号/位置提取 → 多级标题章节树 → 交 AI 校验修正误判
