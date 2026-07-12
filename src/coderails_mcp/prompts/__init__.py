"""Prompt templates, stored as Markdown files and loaded by name.

Keeping prompts out of the Python modules lets them be edited without touching
code. Load one with ``load_prompt("code_apply.system")`` and fill any
``{placeholder}`` fields with ``str.format`` at the call site.
"""

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Return the text of ``<name>.md`` from this package.

    Args:
        name: Prompt file stem, e.g. ``"code_apply.system"``.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")
