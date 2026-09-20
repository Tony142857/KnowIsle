"""上传接口的纯逻辑单元测试（CI 无 DB/Redis，仅测扩展名映射契约）。"""

from app.api.documents import _EXT_TO_FILE_TYPE, _LEGACY_OFFICE_EXT


def test_legacy_office_extensions_rejected():
    # .doc/.ppt 为 OLE2 二进制格式，python-docx/pptx 无法解析，上传时必须明确拒绝
    assert ".doc" in _LEGACY_OFFICE_EXT and ".ppt" in _LEGACY_OFFICE_EXT
    assert ".doc" not in _EXT_TO_FILE_TYPE and ".ppt" not in _EXT_TO_FILE_TYPE


def test_supported_extensions_mapping():
    assert _EXT_TO_FILE_TYPE[".pdf"] == "pdf_textbook"
    assert _EXT_TO_FILE_TYPE[".pptx"] == "ppt"
    assert _EXT_TO_FILE_TYPE[".docx"] == "word"
    assert _EXT_TO_FILE_TYPE[".md"] == "markdown"
    assert _EXT_TO_FILE_TYPE[".markdown"] == "markdown"
