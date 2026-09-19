"""文档解析器分发入口（模块 A1）：按 file_type 选择解析器。"""

from app.core.parser.base import BaseParser, ParsedBlock, ParseError
from app.core.parser.md_parser import MarkdownParser
from app.core.parser.pdf_parser import PdfParser
from app.core.parser.ppt_parser import PptParser
from app.core.parser.word_parser import WordParser

_PARSERS: dict[str, BaseParser] = {
    p.file_type: p for p in (PdfParser(), PptParser(), WordParser(), MarkdownParser())
}


def parse_by_file_type(file_type: str, data: bytes) -> list[ParsedBlock]:
    """按 documents.file_type 分发解析（pdf_textbook/ppt/word/markdown），同步实现。"""
    parser = _PARSERS.get(file_type)
    if parser is None:
        raise ParseError(f"不支持的文件类型: {file_type}")
    return parser.parse(data)


__all__ = [
    "BaseParser",
    "MarkdownParser",
    "ParseError",
    "ParsedBlock",
    "PdfParser",
    "PptParser",
    "WordParser",
    "parse_by_file_type",
]
