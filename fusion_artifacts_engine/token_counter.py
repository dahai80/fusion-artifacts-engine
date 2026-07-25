import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class TokenCounter:

    def __init__(self, mlx_url: str = "http://localhost:8890"):
        self.mlx_url = mlx_url.rstrip("/")
        self._tiktoken_enc = None

    async def count(self, text: str, model: Optional[str] = None) -> int:
        try:
            return await self._count_via_mlx(text, model)
        except Exception as e:
            logger.debug(f"MLX count failed ({e}), falling back to tiktoken")
            return self._count_via_tiktoken(text)

    def count_sync(self, text: str) -> int:
        try:
            return self._count_via_tiktoken(text)
        except Exception as e:
            logger.debug(f"tiktoken failed ({e}), falling back to heuristic")
            return len(text) // 4

    async def count_messages(self, messages: list[dict], model: Optional[str] = None) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += await self.count(content, model)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text", "") or block.get("content", "")
                        if text:
                            total += await self.count(text, model)
        return total

    async def check_safety(
        self,
        messages: list[dict],
        max_context: int = 180_000,
        reserve_output: int = 8192,
        model: Optional[str] = None,
    ) -> tuple[bool, int, int]:
        total = await self.count_messages(messages, model)
        remaining = max_context - total - reserve_output
        safe = remaining > 0
        logger.debug(f"Safety check: total={total} remaining={remaining} safe={safe}")
        return safe, total, remaining

    async def _count_via_mlx(self, text: str, model: Optional[str] = None) -> int:
        model = model or "default"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{self.mlx_url}/v1/messages/count_tokens",
                json={"model": model, "messages": [{"role": "user", "content": text}]},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("total_tokens", 0)

    def _count_via_tiktoken(self, text: str) -> int:
        if self._tiktoken_enc is None:
            try:
                import tiktoken
                self._tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            except ImportError:
                logger.warning("tiktoken not installed, using heuristic")
                return len(text) // 4
        return len(self._tiktoken_enc.encode(text))
