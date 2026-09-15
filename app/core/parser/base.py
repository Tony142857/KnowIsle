"""解析器基类（模块 A1）：先提取层级结构，再按语义边界切块。

输出统一的「章节 → 小节」目录树与带元数据的文本块（元数据格式见 §8.8）。
"""

import dataclasses


@dataclasses.dataclass
class ParsedBlock:
    title: str | None  # 标题（页标题 / 章节标题）
    content: str
    level: int  # 标题层级，正文为 0
    page_num: int | None = None


class BaseParser:
    file_type: str = ""

    async def parse(self, file_path: str) -> list[ParsedBlock]:
        raise NotImplementedError
