"""code_map tool backend: deterministic tree + symbol outline. Zero LLM.

One call orients the caller in an unfamiliar repo: a directory tree where each
code file lists its top-level symbols with signatures. Parsed on demand with
tree-sitter — no persistent index, no cache to invalidate. When the scope is
too big for the line cap the output degrades to tree-only (then dirs-only with
file counts) and tells the caller to re-scope via `path`/`depth`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..core import config
from . import syntax
from .code_search import _SKIP_DIRS

_INDENT = "  "


def _children(directory: Path) -> tuple[list[Path], list[Path]]:
    """(subdirs, files) of a directory, skipping hidden and vendored entries."""
    dirs: list[Path] = []
    files: list[Path] = []
    try:
        for child in sorted(directory.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            (dirs if child.is_dir() else files).append(child)
    except OSError:
        pass
    return dirs, files


def _count_files(directory: Path) -> int:
    total = 0
    dirs, files = _children(directory)
    total += len(files)
    for sub in dirs:
        total += _count_files(sub)
    return total


def _render(scope: Path, max_depth: int | None, with_symbols: bool) -> list[str]:
    lines: list[str] = []

    def walk(directory: Path, level: int) -> None:
        dirs, files = _children(directory)
        indent = _INDENT * level
        if max_depth is not None and level >= max_depth:
            if dirs or files:
                lines.append(f"{indent}... ({_count_files(directory)} files below depth limit)")
            return
        for file in files:
            lines.append(f"{indent}{file.name}")
            if with_symbols:
                tree, source = syntax.parse_file(file)
                if tree is not None:
                    lang = syntax.language_for(file) or ""
                    for depth, sig in syntax.outline(tree, source, lang):
                        lines.append(f"{indent}{_INDENT * (depth + 1)}{sig}")
        for sub in dirs:
            lines.append(f"{indent}{sub.name}/")
            walk(sub, level + 1)

    walk(scope, 0)
    return lines


def _render_dirs_only(scope: Path, max_depth: int | None) -> list[str]:
    lines: list[str] = []

    def walk(directory: Path, level: int) -> None:
        dirs, _files = _children(directory)
        for sub in dirs:
            lines.append(f"{_INDENT * level}{sub.name}/ ({_count_files(sub)} files)")
            if max_depth is None or level + 1 < max_depth:
                walk(sub, level + 1)

    lines.append(f"./ ({_count_files(scope)} files)")
    walk(scope, 0)
    return lines


def _map_sync(root: Path, path: str | None, depth: int | None) -> dict[str, Any]:
    scope = (root / path).resolve() if path else root
    if scope != root and root not in scope.parents:
        return {"error": f"path {path} is outside root_dir {root}"}
    if not scope.is_dir():
        return {"error": f"path is not a directory: {scope}"}

    cap = config.CODE_MAP_MAX_LINES
    rel_scope = scope.relative_to(root).as_posix() if scope != root else "."
    result: dict[str, Any] = {"root": str(root), "path": rel_scope, "depth": depth}

    for mode, lines in (
        ("symbols", None),
        ("files", None),
        ("dirs", None),
    ):
        if mode == "symbols":
            lines = _render(scope, depth, with_symbols=True)
        elif mode == "files":
            lines = _render(scope, depth, with_symbols=False)
        else:
            lines = _render_dirs_only(scope, depth)
        if len(lines) <= cap:
            result.update(mode=mode, line_count=len(lines), map="\n".join(lines))
            if mode != "symbols":
                result["note"] = (
                    f"Scope too big for symbol outlines within the {cap}-line cap; degraded to "
                    f"{mode}-only. Re-scope with `path` (a subdirectory) and/or `depth` to get symbols."
                )
            return result

    lines = _render_dirs_only(scope, depth)[:cap]
    result.update(
        mode="dirs",
        line_count=len(lines),
        map="\n".join(lines),
        note=(
            f"Even the directory listing exceeded the {cap}-line cap and was truncated. "
            "Re-scope with `path` and/or `depth`."
        ),
    )
    return result


async def run_code_map(root_dir: str, path: str | None = None, depth: int | None = None) -> dict[str, Any]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return {"error": f"root_dir does not exist or is not a directory: {root_dir}"}
    return await asyncio.to_thread(_map_sync, root, path, depth)
