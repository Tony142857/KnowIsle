"""OpenAI 兼容大模型客户端（§10.1）。

官方模式与用户自定义 Key 模式仅凭证与路由不同，调用链路完全复用本客户端。
自定义 Key 以 Fernet 加密存储于 user_llm_keys，仅后端调用时解密，前端永不回显。
"""

from collections.abc import AsyncIterator


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式对话：POST {base_url}/chat/completions (stream=True)，逐 token yield。"""
        # TODO(v0.3): SSE 逐 token 解析（模块 A3）
        raise NotImplementedError

    async def chat(self, messages: list[dict]) -> str:
        """非流式对话：用于章节摘要、审核初评、帖子摘要等批量任务。"""
        # TODO(v0.4)
        raise NotImplementedError
