# CodeRails FastMCP

A local [FastMCP](https://github.com/jlowin/fastmcp) server (stdio, no auth) that
exposes three **batch-parallel** tools. `web_search` runs on OpenRouter;
`code_search` and `code_apply` run on Groq:

| Tool | Provider | Default model | What it does |
|---|---|---|---|
| `web_search` | OpenRouter | `perplexity/sonar-pro-search` | Answers web queries with sourced results |
| `code_search` | Groq | `qwen/qwen3.6-27b` | Agentic codebase search; returns only verified relevant files with a role tag and source snippets (content + line ranges) |
| `code_apply` | Groq | `qwen/qwen3.6-27b` | Applies natural-language edits by rewriting whole files; returns unified diffs |

Every tool accepts a `queries` array and runs all queries concurrently — that
parallelism is the whole point, so always batch your independent queries into a
single call.

## Project layout

```
src/coderails_mcp/
├── __init__.py          # package exports (create_server, main, mcp)
├── __main__.py          # enables `python -m coderails_mcp`
├── app/                 # application layer
│   ├── server.py        # builds and runs the FastMCP server
│   └── tools.py         # the three MCP tool definitions + argument models
├── core/                # shared infrastructure
│   ├── config.py        # .env-driven configuration
│   └── clients.py       # async OpenRouter + Groq clients
├── services/            # tool backends (the actual model calls)
│   ├── web_search.py
│   ├── code_search.py
│   └── code_apply.py
└── prompts/             # prompt templates, kept out of the code
    ├── __init__.py      # load_prompt(name) helper
    ├── code_apply.system.md
    ├── code_apply.user.md
    ├── code_search.system.md
    └── code_search.user.md
```

The layering is: `app` (MCP surface) → `services` (backends) → `core` +
`prompts` (infrastructure). Prompts live as Markdown files so they can be tuned
without touching Python.

## Setup

```sh
uv sync
```

Copy `.env.example` to `.env` and add your OpenRouter API key. Every model and
limit is overridable there:

```sh
cp .env.example .env
# then edit .env and set OPENROUTER_API_KEY=...
```

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required for web_search)* | Your OpenRouter key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API base URL |
| `GROQ_API_KEY` | *(required for code_search / code_apply)* | Your Groq key |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq API base URL |
| `WEB_SEARCH_MODEL` | `perplexity/sonar-pro-search` | `web_search` model (OpenRouter) |
| `CODE_SEARCH_MODEL` | `qwen/qwen3.6-27b` | `code_search` model (Groq) |
| `CODE_APPLY_MODEL` | `qwen/qwen3.6-27b` | `code_apply` model (Groq) |
| `CODE_SEARCH_MAX_STEPS` | `16` | Max agent steps per `code_search` query |

## Run

```sh
uv run python -m coderails_mcp
```

(or, after `uv sync` installs the console script, just `coderails-mcp`.)

## Register with Claude Code (user scope)

Add to `~/.claude.json` under `mcpServers`, pointing `--directory` at your clone:

```json
"coderails": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "--directory", "/absolute/path/to/coderails-fastmcp", "python", "-m", "coderails_mcp"]
}
```

## Tool call shapes

```json
{"tool": "web_search", "queries": [{"query": "how to use the openai python client"}]}
{"tool": "code_search", "root_dir": "C:\\my_project", "queries": [{"query": "auth logic"}]}
{"tool": "code_apply", "root_dir": "C:\\my_project", "queries": [{"file": "src/auth.py", "query": "insert an if clause in method X that checks ..."}]}
```

`code_apply` policy: **"Nothing implied gets applied, only what's explicitly
stated."** Malformed or underspecified instructions are rejected (`status:
"rejected"` with a reason); ambiguous but feasible ones are applied literally.
`code_search` follows the mirror policy: **"Returns only what it can verify,
never what it assumes"** — it reports only files it actually opened and confirmed
relevant.
