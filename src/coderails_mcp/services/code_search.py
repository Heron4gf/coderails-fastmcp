"""code_search tool backend: agent harness around the Relace fast agentic search model.

The model drives a tool-call loop; we execute its tool calls locally against the
target repository and feed results back until it calls `report_back` (or the step
budget runs out). Tool schemas and prompts follow the Relace docs:
https://docs.relace.ai/docs/fast-agentic-search/agent
"""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..core import config
from ..core.openrouter import get_client
from ..prompts import load_prompt

SYSTEM_PROMPT = load_prompt("code_search.system")
USER_PROMPT_TEMPLATE = load_prompt("code_search.user")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "view_file",
            "description": "Tool for viewing/exploring file contents with line numbers",
            "parameters": {
                "type": "object",
                "required": ["path", "view_range"],
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to file"},
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "[start_line, end_line], 1-indexed inclusive",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_directory",
            "description": "Tool for viewing directory contents recursively",
            "parameters": {
                "type": "object",
                "required": ["path", "include_hidden"],
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to directory"},
                    "include_hidden": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Fast text-based regex search over the repository",
            "parameters": {
                "type": "object",
                "required": ["query", "case_sensitive", "exclude_pattern", "include_pattern"],
                "properties": {
                    "query": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                    "exclude_pattern": {"type": ["string", "null"]},
                    "include_pattern": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Tool for executing bash commands",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {"command": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_back",
            "description": "Tool to report findings after exploring codebase",
            "parameters": {
                "type": "object",
                "required": ["explanation", "files"],
                "properties": {
                    "explanation": {"type": "string"},
                    "files": {
                        "type": "object",
                        "description": "Map of file path -> list of [start, end] line ranges",
                        "additionalProperties": {"type": "array"},
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode", "dist", "build"}
_MAX_DIR_ENTRIES = 250
_MAX_GREP_RESULTS = 50
_MAX_TOOL_OUTPUT = 20_000


def _resolve_in_root(root: Path, path_str: str) -> Path:
    """Resolve a model-supplied path, requiring it to stay inside the repo root."""
    p = Path(path_str)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"Path {path_str} is outside the repository root {root}")
    return p


def _truncate(text: str) -> str:
    if len(text) > _MAX_TOOL_OUTPUT:
        return text[:_MAX_TOOL_OUTPUT] + "\n... (output truncated)"
    return text


def _view_file(root: Path, path: str, view_range: list[int] | None) -> str:
    target = _resolve_in_root(root, path)
    if not target.is_file():
        return f"Error: {path} is not a file"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = 1, len(lines)
    if view_range and len(view_range) == 2:
        start = max(1, int(view_range[0]))
        end = min(len(lines), int(view_range[1])) if int(view_range[1]) != -1 else len(lines)
    numbered = [f"{i}\t{lines[i - 1]}" for i in range(start, end + 1)]
    return _truncate("\n".join(numbered) or "(empty file)")


def _view_directory(root: Path, path: str, include_hidden: bool) -> str:
    target = _resolve_in_root(root, path)
    if not target.is_dir():
        return f"Error: {path} is not a directory"
    entries: list[str] = []

    def walk(d: Path) -> None:
        if len(entries) >= _MAX_DIR_ENTRIES:
            return
        try:
            children = sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
        except OSError as exc:
            entries.append(f"{d} (unreadable: {exc})")
            return
        for child in children:
            if len(entries) >= _MAX_DIR_ENTRIES:
                entries.append("... (listing truncated at 250 entries)")
                return
            if not include_hidden and child.name.startswith("."):
                continue
            if child.is_dir():
                if child.name in _SKIP_DIRS:
                    continue
                entries.append(str(child.relative_to(target)) + "/")
                walk(child)
            else:
                entries.append(str(child.relative_to(target)))

    walk(target)
    return "\n".join(entries) or "(empty directory)"


def _iter_repo_files(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name in _SKIP_DIRS or child.name.startswith("."):
                    continue
                stack.append(child)
            elif child.is_file():
                yield child


def _grep_search(
    root: Path,
    query: str,
    case_sensitive: bool,
    exclude_pattern: str | None,
    include_pattern: str | None,
) -> str:
    try:
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"
    results: list[str] = []
    for file in _iter_repo_files(root):
        rel = file.relative_to(root).as_posix()
        if include_pattern and not file.match(include_pattern):
            continue
        if exclude_pattern and file.match(exclude_pattern):
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # skip binary/unreadable files
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                results.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                if len(results) >= _MAX_GREP_RESULTS:
                    results.append("... (capped at 50 results)")
                    return "\n".join(results)
    return "\n".join(results) or "No matches found."


def _bash(root: Path, command: str) -> str:
    bash_exe = shutil.which("bash")
    if not bash_exe:
        return "Error: bash is unavailable on this system; use view_file/view_directory/grep_search instead."
    try:
        proc = subprocess.run(
            [bash_exe, "-c", command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 30 seconds."
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return _truncate(output.strip() or f"(no output, exit code {proc.returncode})")


async def _execute_tool_call(root: Path, name: str, args: dict[str, Any]) -> str:
    def run() -> str:
        try:
            if name == "view_file":
                return _view_file(root, args.get("path", ""), args.get("view_range"))
            if name == "view_directory":
                return _view_directory(root, args.get("path", ""), bool(args.get("include_hidden", False)))
            if name == "grep_search":
                return _grep_search(
                    root,
                    args.get("query", ""),
                    bool(args.get("case_sensitive", True)),
                    args.get("exclude_pattern"),
                    args.get("include_pattern"),
                )
            if name == "bash":
                return _bash(root, args.get("command", ""))
            return f"Error: unknown tool {name}"
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    return await asyncio.to_thread(run)


def _normalize_reported_files(root: Path, files: Any) -> dict[str, list[list[int]]]:
    """Make reported paths relative to root and shaped as {path: [[start, end], ...]}."""
    normalized: dict[str, list[list[int]]] = {}
    if not isinstance(files, dict):
        return normalized
    for raw_path, ranges in files.items():
        path = str(raw_path).replace("\\", "/")
        try:
            resolved = Path(path) if Path(path).is_absolute() else root / path
            path = resolved.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            path = path.removeprefix("/repo/").lstrip("/")
        normalized[path] = ranges if isinstance(ranges, list) else []
    return normalized


async def _search_one(root: Path, query: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(root_dir=str(root), query=query)},
    ]
    client = get_client()

    try:
        for _ in range(config.CODE_SEARCH_MAX_STEPS):
            response = await client.chat.completions.create(
                model=config.CODE_SEARCH_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            if not tool_calls:
                # Model answered in plain text; treat it as the explanation.
                return {"query": query, "explanation": message.content or "", "files": {}}

            # report_back terminates the loop
            for call in tool_calls:
                if call.function.name == "report_back":
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    return {
                        "query": query,
                        "explanation": args.get("explanation", ""),
                        "files": _normalize_reported_files(root, args.get("files", {})),
                    }

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments},
                        }
                        for c in tool_calls
                    ],
                }
            )

            # Execute all tool calls concurrently
            async def run_call(call) -> str:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    return f"Error: could not parse arguments: {exc}"
                return await _execute_tool_call(root, call.function.name, args)

            outputs = await asyncio.gather(*(run_call(c) for c in tool_calls))
            for call, output in zip(tool_calls, outputs):
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

        return {
            "query": query,
            "explanation": "Step budget exhausted before the agent reported findings. "
            "No verified files to return (files are only reported once confirmed relevant).",
            "files": {},
            "error": "max_steps_reached",
        }
    except Exception as exc:
        return {"query": query, "error": f"{type(exc).__name__}: {exc}", "files": {}}


async def run_code_search(root_dir: str, queries: list[str]) -> list[dict[str, Any]]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return [{"query": q, "error": f"root_dir does not exist or is not a directory: {root_dir}"} for q in queries]
    return list(await asyncio.gather(*(_search_one(root, q) for q in queries)))
