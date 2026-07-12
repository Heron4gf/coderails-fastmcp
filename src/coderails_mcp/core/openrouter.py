"""Shared async OpenRouter client."""

from functools import lru_cache

from openai import AsyncOpenAI

from . import config


@lru_cache(maxsize=1)
def get_client() -> AsyncOpenAI:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to the .env file at the project root."
        )
    return AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )
