"""code_find tool backend: exact search -> AST-expanded context. Zero LLM.

Pipeline per query: ripgrep (or a pure-Python scan when rg is missing) produces
candidate (file, line, column) hits -> tree-sitter expands each hit to its full
enclosing function/class. Symbol mode additionally classifies every hit as the
definition or a reference from the AST node type, returning definition + blast
radius in one call. Deterministic: no model, no step budget, no failed queries.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..core import config
from . import syntax
from .code_search import _SKIP_DIRS, _iter_repo_files

_MAX_HIT_CHARS = 4_000
_MAX_RAW_CANDIDATES = 2_000
_RG_TIMEOUT = 30


def _rg_candidates(root: Path, pattern: str) -> list[tuple[str, int, int]] | None:
    """(rel_path, line, column) candidates via ripgrep, or None when rg can't run."""
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [rg, "--json", "--no-messages", "-e", pattern]
    cmd += [f"--glob=!{name}/**" for name in _SKIP_DIRS]
    cmd.append("./")
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=_RG_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode not in (0, 1):  # 2 = usage/regex error -> let the fallback report it
        return None
    candidates: list[tuple[str, int, int]] = []
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        rel = Path(data["path"]["text"]).as_posix().removeprefix("./")
        submatches = data.get("submatches") or [{}]
        column = int(submatches[0].get("start", 0))
        candidates.append((rel, int(data["line_number"]), column))
        if len(candidates) >= _MAX_RAW_CANDIDATES:
            break
    return candidates


def _python_candidates(root: Path, pattern: str) -> list[tuple[str, int, int]]:
    compiled = re.compile(pattern)
    candidates: list[tuple[str, int, int]] = []
    for file in _iter_repo_files(root):
        try:
            text = file.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue  # skip binary/unreadable files
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = compiled.search(line)
            if match:
                candidates.append((file.relative_to(root).as_posix(), lineno, match.start()))
                if len(candidates) >= _MAX_RAW_CANDIDATES:
                    return candidates
    return candidates


def _candidates(root: Path, pattern: str) -> list[tuple[str, int, int]]:
    found = _rg_candidates(root, pattern)
    if found is None:
        found = _python_candidates(root, pattern)
    return found


class _TreeCache:
    """Parse each touched file once per query."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._trees: dict[str, tuple[Any, bytes, str | None]] = {}

    def get(self, rel: str) -> tuple[Any, bytes, str | None]:
        if rel not in self._trees:
            path = self.root / rel
            lang = syntax.language_for(path)
            tree, source = syntax.parse_file(path) if lang else (None, b"")
            self._trees[rel] = (tree, source, lang)
        return self._trees[rel]


def _snippet(source: bytes, node) -> str:
    start_line, end_line = syntax.node_lines(node)
    lines = source.decode("utf-8", "replace").splitlines()
    content = "\n".join(lines[start_line - 1 : end_line])
    if len(content) > _MAX_HIT_CHARS:
        content = content[:_MAX_HIT_CHARS] + "\n... (snippet truncated)"
    return content


def _line_text(root: Path, rel: str, line: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[line - 1].strip()[:300]
    except (OSError, IndexError):
        return ""


def _expand_hits(
    root: Path,
    cache: _TreeCache,
    candidates: list[tuple[str, int, int]],
    max_hits: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Expand candidates to enclosing-definition hits, deduped by enclosing symbol."""
    hits: list[dict[str, Any]] = []
    by_key: dict[tuple[str, int, int], dict[str, Any]] = {}
    truncated = False
    for rel, line, column in candidates:
        tree, source, lang = cache.get(rel)
        node = None
        if tree is not None and lang:
            node = syntax.enclosing_definition(tree, lang, line, column)
        if node is not None:
            node = syntax.expand_definition(node)
            start_line, end_line = syntax.node_lines(node)
            key = (rel, start_line, end_line)
        else:
            start_line = end_line = line
            key = (rel, line, line)
        existing = by_key.get(key)
        if existing is not None:
            if line not in existing["match_lines"]:
                existing["match_lines"].append(line)
            continue
        if len(hits) >= max_hits:
            truncated = True
            continue  # keep scanning so match_lines of shown hits stay complete
        hit: dict[str, Any] = {
            "file": rel,
            "match_lines": [line],
            "symbol": syntax.node_name(node) if node is not None else None,
            "kind": node.type if node is not None else None,
            "start_line": start_line,
            "end_line": end_line,
            "content": _snippet(source, node) if node is not None else _line_text(root, rel, line),
        }
        by_key[key] = hit
        hits.append(hit)
    return hits, truncated


def _find_pattern(root: Path, pattern: str) -> dict[str, Any]:
    try:
        re.compile(pattern)
    except re.error as exc:
        return {"pattern": pattern, "error": f"invalid regex: {exc}"}
    candidates = _candidates(root, pattern)
    cache = _TreeCache(root)
    hits, truncated = _expand_hits(root, cache, candidates, config.CODE_FIND_MAX_HITS)
    result: dict[str, Any] = {"pattern": pattern, "total_matches": len(candidates), "hits": hits}
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Showing {len(hits)} of more enclosing symbols; {len(candidates)} raw line matches "
            "total. Narrow the pattern to see the rest."
        )
    return result


def _find_symbol(root: Path, symbol: str) -> dict[str, Any]:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])"
    candidates = _candidates(root, pattern)
    cache = _TreeCache(root)

    def_candidates: list[tuple[str, int, int]] = []
    ref_candidates: list[tuple[str, int, int]] = []
    for rel, line, column in candidates:
        tree, _source, lang = cache.get(rel)
        is_def = False
        if tree is not None and lang:
            leaf = syntax.name_node_at(tree, line, column)
            is_def = leaf is not None and syntax.is_definition_name(leaf, lang)
        (def_candidates if is_def else ref_candidates).append((rel, line, column))

    definitions, _ = _expand_hits(root, cache, def_candidates, config.CODE_FIND_MAX_HITS)
    references, truncated = _expand_hits(root, cache, ref_candidates, config.CODE_FIND_MAX_HITS)
    result: dict[str, Any] = {
        "symbol": symbol,
        "total_matches": len(candidates),
        "definitions": definitions,
        "references": references,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"References capped at {config.CODE_FIND_MAX_HITS} enclosing symbols; "
            f"{len(ref_candidates)} reference sites total."
        )
    return result


def _find_one(root: Path, query: dict[str, Any]) -> dict[str, Any]:
    pattern = query.get("pattern")
    symbol = query.get("symbol")
    if bool(pattern) == bool(symbol):
        return {**query, "error": "each query needs exactly one of `pattern` or `symbol`"}
    try:
        if symbol:
            return _find_symbol(root, str(symbol))
        return _find_pattern(root, str(pattern))
    except Exception as exc:
        return {**query, "error": f"{type(exc).__name__}: {exc}"}


async def run_code_find(root_dir: str, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return [{**q, "error": f"root_dir does not exist or is not a directory: {root_dir}"} for q in queries]
    return list(await asyncio.gather(*(asyncio.to_thread(_find_one, root, q) for q in queries)))
