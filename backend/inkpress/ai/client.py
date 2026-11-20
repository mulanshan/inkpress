from __future__ import annotations

import asyncio
from typing import Any

import httpx
from openai import AsyncOpenAI

from inkpress.logging import get_logger

logger = get_logger(__name__)


class AIClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        outbound_proxy: str = "",
        use_proxy: bool = False,
        max_concurrency: int = 3,
        timeout_seconds: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        http_client = httpx.AsyncClient(
            proxy=outbound_proxy if (use_proxy and outbound_proxy) else None,
            timeout=float(timeout_seconds),
            trust_env=False,
        )
        self._http_client = http_client
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            http_client=http_client,
            max_retries=2,
        )

    async def close(self) -> None:
        await self._http_client.aclose()

    async def chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content if resp.choices else ""
            return (content or "").strip()

    async def test_connection(self) -> dict[str, Any]:
        reply = await self.chat(
            system="You are a concise connectivity check assistant.",
            user="Reply with exactly: OK",
            temperature=0.0,
            max_tokens=16,
        )
        return {"ok": True, "model": self.model, "base_url": self.base_url, "reply": reply}
