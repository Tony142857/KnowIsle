"""敏感词过滤（模块 B2 预检一环）：演示级词表，生产环境应外置可配（DB / 配置文件热更新）。"""

SENSITIVE_WORDS: tuple[str, ...] = (
    "代考",
    "替考",
    "枪手",
    "买卖答案",
    "包过",
    "作弊神器",
    "赌博",
    "色情",
    "毒品",
    "枪支",
)


def find_sensitive_hits(text: str) -> list[str]:
    """返回命中的敏感词列表（去重、按在文本中的出现顺序）。纯函数，供单测。"""
    hits = [word for word in SENSITIVE_WORDS if word in text]
    hits.sort(key=text.index)
    return hits
