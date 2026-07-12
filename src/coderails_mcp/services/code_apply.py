"""code_apply tool backend: Mercury 2 rewrites a whole file per edit instruction.

Pipeline per edit: evaluate the instruction -> reject malformed / adjust ambiguous
but feasible -> emit the complete updated file -> we overwrite the file on disk
and return a unified diff. Policy: "Nothing implied gets applied, only what's
explicitly stated."
"""

import asyncio
import difflib
import re
from pathlib import Path
from typing import Any

from ..core import config
from ..core.openrouter import get_client
from ..prompts import load_prompt

SYSTEM_PROMPT = load_prompt("code_apply.system")
USER_PROMPT_TEMPLATE = load_prompt("code_apply.user")

_REJECTED_RE = re.compile(r"<rejected>(.*?)</rejected>", re.DOTALL)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


def _parse_response(text: str) -> tuple[str, str]:
    """Return ("rejected", reason) or ("applied", new_content) or raise ValueError."""
    rejected = _REJECTED_RE.search(text)
    if rejected:
        return "rejected", rejected.group(1).strip()
    fences = _FENCE_RE.findall(text)
    if fences:
        # Take the largest fenced block: the full file dwarfs any stray snippet.
        return "applied", max(fences, key=len)
    raise ValueError("Model response contained neither <rejected> nor a fenced code block.")


def _write_preserving_newlines(target: Path, original: str, new_content: str) -> str:
    """Write new content using the original file's dominant newline style."""
    if not new_content.endswith("\n"):
        new_content += "\n"
    newline = "\r\n" if original.count("\r\n") >= original.count("\n") - original.count("\r\n") and "\r\n" in original else "\n"
    with open(target, "w", encoding="utf-8", newline=newline) as f:
        f.write(new_content)
    return new_content


def _unified_diff(path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


async def _apply_one(root: Path, file: str, instruction: str) -> dict[str, Any]:
    result: dict[str, Any] = {"file": file, "query": instruction}
    try:
        target = Path(file)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"File {file} is outside root_dir {root}")
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {target}")

        original = target.read_text(encoding="utf-8", errors="replace")

        response = await get_client().chat.completions.create(
            model=config.CODE_APPLY_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        file_path=target.as_posix(), content=original, instruction=instruction
                    ),
                },
            ],
            max_tokens=32768,
        )
        status, payload = _parse_response(response.choices[0].message.content or "")

        if status == "rejected":
            result.update(status="rejected", reason=payload)
            return result

        new_content = _write_preserving_newlines(target, original, payload)
        rel = target.relative_to(root).as_posix() if root in target.parents or target == root else target.as_posix()
        result.update(status="applied", diff=_unified_diff(rel, original, new_content) or "(no changes)")
        return result
    except Exception as exc:
        result.update(status="error", reason=f"{type(exc).__name__}: {exc}")
        return result


async def run_code_apply(root_dir: str, edits: list[dict[str, str]]) -> list[dict[str, Any]]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return [
            {"file": e.get("file", ""), "status": "error", "reason": f"root_dir does not exist: {root_dir}"}
            for e in edits
        ]

    # Edits on different files run in parallel; edits on the same file run
    # sequentially in the order given, to avoid write races.
    groups: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for edit in edits:
        key = str((root / edit["file"]).resolve()) if not Path(edit["file"]).is_absolute() else str(Path(edit["file"]).resolve())
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(edit)

    async def run_group(group: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [await _apply_one(root, e["file"], e["query"]) for e in group]

    group_results = await asyncio.gather(*(run_group(groups[k]) for k in order))

    # Restore the caller's original edit order.
    by_group = {k: list(res) for k, res in zip(order, group_results)}
    results: list[dict[str, Any]] = []
    for edit in edits:
        key = str((root / edit["file"]).resolve()) if not Path(edit["file"]).is_absolute() else str(Path(edit["file"]).resolve())
        results.append(by_group[key].pop(0))
    return results
