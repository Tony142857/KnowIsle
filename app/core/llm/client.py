"""OpenAI 兼容大模型客户端（§10.1）。

官方模式与用户自定义 Key 模式仅凭证与路由不同，调用链路完全复用本客户端。
自定义 Key 以 Fernet 加密存储于 user_llm_keys，仅后端调用时解密，前端永不回显。
"""

import json
from collections.abc import AsyncIterator

import httpx

_TIMEOUT = httpx.Timeout(connect=10, read=120, write=30, pool=10)


class LLMError(RuntimeError):
    """LLM 接口调用失败（带 HTTP 状态码上下文）。"""


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.last_usage: dict = {}  # 最近一次调用的 usage（流式取末尾 usage 块）

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=_TIMEOUT)

    async def chat(self, messages: list[dict]) -> tuple[str, dict]:
        """非流式对话：返回 (回答文本, usage)。用于章节摘要、审核初评、帖子摘要等批量任务。"""
        async with self._client() as client:
            resp = await client.post(
                "/chat/completions",
                headers=self._headers(),
                json={"model": self.model, "messages": messages, "stream": False},
            )
        if resp.status_code != 200:
            raise LLMError(f"LLM 请求失败：HTTP {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        self.last_usage = data.get("usage") or {}
        return data["choices"][0]["message"]["content"], self.last_usage

    async def chat_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式对话：POST {base_url}/chat/completions (stream=True)，解析 SSE 逐 token yield。"""
        self.last_usage = {}
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        async with self._client() as client:
            async with client.stream(
                "POST", "/chat/completions", headers=self._headers(), json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    raise LLMError(f"LLM 请求失败：HTTP {resp.status_code} {body}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # 容错：跳过残缺行
                    if obj.get("usage"):
                        self.last_usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if choices:
                        token = (choices[0].get("delta") or {}).get("content")
                        if token:
                            yield token
