"""MCP tool definitions.

Each tool is a thin, well-documented wrapper that validates its arguments with a
pydantic model and delegates to a backend in ``coderails_mcp.services``. Call
``register_tools(mcp)`` to attach them to a FastMCP server.
"""

from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from ..services.code_apply import run_code_apply
from ..services.code_search import run_code_search
from ..services.web_search import run_web_search


class SearchQuery(BaseModel):
    query: str = Field(description="A single natural-language query.")


class EditQuery(BaseModel):
    file: str = Field(description="Path of the file to edit, relative to root_dir (or absolute).")
    query: str = Field(
        description=(
            "Natural-language edit instruction for this file. May include code examples. "
            "Every intended change must be explicitly stated: compound or partially-specified "
            "instructions are rejected, not guessed at."
        )
    )


def register_tools(mcp: FastMCP) -> None:
    """Register the web_search, code_search and code_apply tools on ``mcp``."""

    @mcp.tool
    async def web_search(queries: list[SearchQuery]) -> list[dict[str, Any]]:
        """Search the web and get direct, sourced answers.

        Prompted by your coding agent. Each query is answered independently by an
        agentic web-search model; pass multiple queries in one call to run them all
        in parallel and take advantage of the model's speed.

        Returns one result per query: {query, answer} or {query, error}.
        """
        return await run_web_search([q.query for q in queries])

    @mcp.tool
    async def code_search(root_dir: str, queries: list[SearchQuery]) -> list[dict[str, Any]]:
        """Find the files in a codebase that matter for a natural-language query.

        Search files -> a fast search agent generates commands -> performs agentic
        search (view_file / view_directory / grep) inside root_dir -> returns only
        relevant files.

        "Returns only what it can verify, never what it assumes." The tool only
        returns files the agent actually visited and confirmed relevant through
        view_file/grep, never files inferred as "probably related" from naming
        patterns or folder structure alone. If a relevant file isn't found within
        the step budget, it returns what it found, not a guess at what else might
        exist.

        Pass multiple queries in one call: each runs as an independent parallel
        search. Returns one result per query:
        {query, explanation, files: [{file, role, snippets}]} where `role` is a
        <=5-word tag saying why the file matters and each snippet is
        {start_line, end_line, content} carrying the actual source lines, so you
        usually don't need to open the files yourself.

        Reliability semantics: if the step budget runs out, findings are salvaged
        and marked `partial: true` — trust the files listed, but the topic may not
        be fully covered. A query that fails outright is retried with a doubled
        budget; if it still fails it comes back with `not_covered: true` and an
        explanation starting "TOPIC NOT COVERED". Treat that as a hole in the
        results: no other query's answer stands in for it — search that topic
        yourself or re-run it.

        Args:
            root_dir: Absolute path of the repository/project to search.
            queries: Natural-language descriptions of what to locate (e.g. "auth logic").
        """
        return await run_code_search(root_dir, [q.query for q in queries])

    @mcp.tool
    async def code_apply(root_dir: str, queries: list[EditQuery]) -> list[dict[str, Any]]:
        """Apply natural-language edit instructions to files and get back diffs.

        Edit code -> the edit model evaluates the instruction -> applies it to the
        file -> the file is overwritten on disk -> a unified diff is returned.

        "Nothing implied gets applied, only what's explicitly stated." Every clause
        in the instruction must map to an explicit, separately stated action.
        Compound or partially-specified instructions get rejected, not guessed at:
        malformed prompts that would cause bad edits are rejected; ambiguous prompts
        that are still feasible are adjusted to their most literal reading.

        Pass multiple queries in one call: edits to different files run in parallel
        (same-file edits run sequentially in order). Returns one result per query:
        {file, status: "applied"|"rejected"|"error", diff?, reason?}.

        Args:
            root_dir: Absolute path of the project containing the files.
            queries: One {file, query} edit instruction per change.
        """
        return await run_code_apply(root_dir, [{"file": q.file, "query": q.query} for q in queries])
