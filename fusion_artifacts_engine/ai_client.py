import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


async def list_models(
    base_url: str, timeout: float = 10.0, api_key: str | None = None
) -> list[str]:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    models = [m.get("id") for m in data.get("data", []) if m.get("id")]
    logger.info("Listed models from %s: %d available", base_url, len(models))
    return models


async def chat(
    base_url: str,
    model: str,
    messages: list[dict],
    timeout: float = 120.0,
    api_key: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.7,
) -> str:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    logger.info("AI chat request: model=%s messages=%d", model, len(messages))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    text = data["choices"][0]["message"]["content"]
    logger.info("AI chat done: model=%s chars=%d", model, len(text or ""))
    return text or ""
