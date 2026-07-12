"""Shared async OpenAI-compatible clients, one per provider.

Both providers speak the OpenAI wire protocol, so they share the same
``AsyncOpenAI`` class and differ only in base URL and API key:

- ``get_openrouter_client`` — OpenRouter, used by ``web_search`` (Perplexity Sonar).
- ``get_groq_client`` — Groq, used by ``code_search`` and ``code_apply`` (Qwen3).
"""

from functools import lru_cache

from openai import AsyncOpenAI

from . import config


@lru_cache(maxsize=1)
def get_openrouter_client() -> AsyncOpenAI:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to the .env file at the project root."
        )
    return AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )


@lru_cache(maxsize=1)
def get_groq_client() -> AsyncOpenAI:
    if not config.GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to the .env file at the project root."
        )
    return AsyncOpenAI(
        api_key=config.GROQ_API_KEY,
        base_url=config.GROQ_BASE_URL,
    )
