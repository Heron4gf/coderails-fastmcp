"""Environment-driven configuration. All models are overridable via .env."""

import os
from pathlib import Path

from dotenv import load_dotenv


def _find_project_root(start: Path) -> Path:
    """Walk upward from ``start`` until a directory with pyproject.toml is found."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return start.parents[-1]


# Load the .env sitting at the project root, falling back to whatever .env
# python-dotenv finds from the current working directory.
_PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()  # no-op for keys already set


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


# OpenRouter powers web_search (Perplexity Sonar).
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Groq powers code_search and code_apply (Qwen3).
GROQ_API_KEY = _env("GROQ_API_KEY", "")
GROQ_BASE_URL = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

WEB_SEARCH_MODEL = _env("WEB_SEARCH_MODEL", "perplexity/sonar-pro-search")
CODE_SEARCH_MODEL = _env("CODE_SEARCH_MODEL", "qwen/qwen3.6-27b")
CODE_APPLY_MODEL = _env("CODE_APPLY_MODEL", "qwen/qwen3.6-27b")

CODE_SEARCH_MAX_STEPS = int(_env("CODE_SEARCH_MAX_STEPS", "16"))
# Extra full attempts after a failed search; each retry doubles the step budget.
CODE_SEARCH_RETRIES = int(_env("CODE_SEARCH_RETRIES", "1"))

# Deterministic tools (no LLM): output caps.
CODE_FIND_MAX_HITS = int(_env("CODE_FIND_MAX_HITS", "20"))
CODE_MAP_MAX_LINES = int(_env("CODE_MAP_MAX_LINES", "2000"))
