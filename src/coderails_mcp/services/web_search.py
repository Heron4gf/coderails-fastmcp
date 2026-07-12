"""web_search tool backend: one Sonar Pro completion per query, all in parallel."""

import asyncio
from typing import Any

from ..core import config
from ..core.clients import get_openrouter_client
from ..prompts import load_prompt

SYSTEM_PROMPT = load_prompt("web_search.system")


async def _search_one(query: str) -> dict[str, Any]:
    try:
        response = await get_openrouter_client().chat.completions.create(
            model=config.WEB_SEARCH_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
        )
        return {"query": query, "answer": response.choices[0].message.content}
    except Exception as exc:  # per-query failures must not kill the batch
        return {"query": query, "error": f"{type(exc).__name__}: {exc}"}


async def run_web_search(queries: list[str]) -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(_search_one(q) for q in queries)))
