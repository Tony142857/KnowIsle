"""AI 校验（模块 A1「自动解析 + AI 校验」）：对 tree_builder 产出的章节树做 LLM 校验。

校验是附加层：仅产出结构化结论（ok + issues + suggestions）供人工修正参考，
不回写 chapters/chunks，tree_builder 的规则提取结果始终直接采用（既有行为不变）。
documents 表无校验结论字段，本迭代不新增表、不动迁移，结论统一写入 worker 日志，
人工修正时按 document_id 检索日志即可（见 verify_tree_safe 的日志输出）。
"""

import json
import logging

from app.core.llm.prompts import TREE_VERIFY_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.parser.base import ParsedBlock

logger = logging.getLogger(__name__)

_MAX_CHAPTERS = 50  # 送检章节数上限（SHORT 档上下文有限，超出截断）
_SAMPLE_CHARS = 200  # 每章正文样本长度上限


def collect_tree_sample(blocks: list[ParsedBlock]) -> list[dict]:
    """从 ParsedBlock 序列提取送检样本：[{"title": ..., "sample": ...}, ...]。

    与 tree_builder 同口径：仅 level==1 标题视作章节；其后的正文块摘录为该章
    样本（首个章节之前的正文忽略），样本累计到 _SAMPLE_CHARS 即截断。
    """
    chapters: list[dict] = []
    for block in blocks:
        if block.level == 1 and block.title:
            chapters.append({"title": block.title.strip(), "sample": ""})
        elif block.level == 0 and chapters:
            current = chapters[-1]
            remaining = _SAMPLE_CHARS - len(current["sample"])
            if remaining > 0 and block.content.strip():
                piece = block.content.strip()[:remaining]
                current["sample"] = f"{current['sample']}{piece}"[:_SAMPLE_CHARS]
    return chapters[:_MAX_CHAPTERS]


def parse_verify_response(text: str) -> dict:
    """从 LLM 输出提取 JSON 结论（容忍 ```json 代码围栏与前后杂文本），并规范化字段类型。

    与 moderation/precheck._parse_ai_review 同一提取策略：取第一个 { 到最后一个 } 子串。
    ok 缺省按 issues 是否为空推断；issues/suggestions 强制为字符串列表。
    无法提取合法 JSON 时抛 ValueError，由 verify_tree_safe 降级处理。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("章节树校验输出中不含 JSON")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("章节树校验输出不是 JSON 对象")
    issues = [str(i) for i in data.get("issues") or []]
    suggestions = [str(s) for s in data.get("suggestions") or []]
    ok = data.get("ok")
    if not isinstance(ok, bool):
        ok = not issues
    return {"ok": ok, "issues": issues, "suggestions": suggestions}


async def verify_tree(client, sample: list[dict]) -> dict:
    """SHORT 档 LLM 校验章节边界：输入 collect_tree_sample 的样本，输出结构化结论。

    LLM 调用失败 / 输出解析失败均向上抛出，由 verify_tree_safe 统一降级。
    """
    lines = []
    for idx, chapter in enumerate(sample, 1):
        lines.append(f"{idx}. {chapter['title']}")
        if chapter["sample"]:
            lines.append(f"   正文样本：{chapter['sample']}")
    prompt = TREE_VERIFY_PROMPT.format(tree_sample="\n".join(lines))
    text, _ = await client.chat([{"role": "user", "content": prompt}])
    return parse_verify_response(text)


async def verify_tree_safe(blocks: list[ParsedBlock], *, document_id: int, client=None) -> dict:
    """失败容错的校验入口：任何异常（LLM 故障 / 超时 / 输出解析失败）一律降级为
    「规则结果直接采用」（ok=True + fallback 原因）并记日志，绝不抛出，
    因此绝不影响文档解析主流程的成功与否。无章节（全文无一级标题）时跳过 LLM 调用。
    """
    try:
        sample = collect_tree_sample(blocks)
        if not sample:
            logger.info("章节树 AI 校验跳过：全文无一级标题 document_id=%s", document_id)
            return {"ok": True, "issues": [], "suggestions": [], "fallback": "no_chapters"}
        client = client or get_official_client(ModelTier.SHORT)
        result = await verify_tree(client, sample)
    except Exception as exc:
        logger.warning(
            "章节树 AI 校验失败，降级为规则结果直接采用 document_id=%s: %s",
            document_id, exc,
        )
        return {"ok": True, "issues": [], "suggestions": [], "fallback": str(exc)}
    if result["ok"]:
        logger.info("章节树 AI 校验通过 document_id=%s", document_id)
    else:
        logger.warning(
            "章节树 AI 校验发现问题（供人工修正参考）document_id=%s issues=%s suggestions=%s",
            document_id, result["issues"], result["suggestions"],
        )
    return result
