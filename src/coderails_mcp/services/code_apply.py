"""code_apply tool backend: the edit model rewrites a file (or one anchored symbol) per instruction.

Pipeline per edit: evaluate the instruction -> reject malformed / adjust ambiguous
but feasible -> emit the updated code -> gate it (tree-sitter parse check + lint
diff) -> only then overwrite the file on disk and return a unified diff. With a
`symbol` anchor the model sees and rewrites only that definition, spliced back
into the file. Policy: "Nothing implied gets applied, only what's explicitly
stated" — and the gate means it cannot ship a syntax error or a new undefined name.
"""

import asyncio
import difflib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core import config
from ..core.clients import get_groq_client
from ..prompts import load_prompt
from . import syntax

SYSTEM_PROMPT = load_prompt("code_apply.system")
USER_PROMPT_TEMPLATE = load_prompt("code_apply.user")
ANCHORED_SYSTEM_PROMPT = load_prompt("code_apply.anchored.system")
ANCHORED_USER_PROMPT_TEMPLATE = load_prompt("code_apply.anchored.user")

_REJECTED_RE = re.compile(r"<rejected>(.*?)</rejected>", re.DOTALL)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


def _parse_response(text: str) -> tuple[str, str]:
    """Return ("rejected", reason) or ("applied", new_content) or raise ValueError."""
    # Only look for the rejection marker OUTSIDE fenced code: the file being
    # edited may itself contain the literal rejection pattern (this very module
    # does), and matching inside the emitted code block misreads an applied
    # edit as a rejection.
    rejected = _REJECTED_RE.search(_FENCE_RE.sub("", text))
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


def _resolve_anchor(original: str, lang: str | None, symbol: str) -> tuple[int, int, str]:
    """1-indexed inclusive line range and text of the definition named ``symbol``.

    Raises ValueError when the symbol can't be resolved unambiguously.
    """
    if not lang:
        raise ValueError("symbol anchoring is not supported for this file type")
    tree = syntax.parse(original.encode("utf-8"), lang)
    if tree is None:
        raise ValueError("could not parse the file to resolve the symbol anchor")
    nodes = syntax.find_definitions(tree, lang, symbol)
    if not nodes:
        raise ValueError(f"symbol not found in file: {symbol}")
    if len(nodes) > 1:
        locations = ", ".join(f"line {syntax.node_lines(n)[0]}" for n in nodes)
        raise ValueError(
            f"symbol {symbol} is ambiguous ({len(nodes)} definitions at {locations}); "
            "edit without an anchor instead"
        )
    node = syntax.expand_definition(nodes[0])
    start, end = syntax.node_lines(node)
    lines = original.splitlines(keepends=True)
    return start, end, "".join(lines[start - 1 : end])


def _splice(original: str, start: int, end: int, replacement: str) -> str:
    """Replace 1-indexed inclusive lines [start, end] of ``original`` with ``replacement``."""
    if not replacement.endswith("\n"):
        replacement += "\n"
    lines = original.splitlines(keepends=True)
    return "".join(lines[: start - 1]) + replacement + "".join(lines[end:])


_LINT_TIMEOUT = 30


def _lint_command(lang: str | None, rel_path: str) -> list[str] | None:
    """Command that lints source from stdin, printing one diagnostic per line."""
    if lang == "python":
        return [sys.executable, "-m", "pyflakes"]
    if lang in ("javascript", "typescript", "tsx"):
        eslint = shutil.which("eslint")
        if eslint:
            return [eslint, "--stdin", "--stdin-filename", rel_path, "--format", "unix"]
    return None


def _lint(command: list[str], root: Path, content: str) -> list[str] | None:
    """Normalized diagnostics (location prefixes stripped), or None when the linter can't run."""
    try:
        proc = subprocess.run(
            command, cwd=root, input=content, capture_output=True, text=True, timeout=_LINT_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode not in (0, 1):  # e.g. eslint exit 2: config problem -> skip the gate
        return None
    diagnostics: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 3)
        diagnostics.append(parts[-1].strip() if len(parts) == 4 else line)
    return diagnostics


def _run_gate(
    root: Path, rel_path: str, lang: str | None, original: str, candidate: str
) -> tuple[str | None, list[str]]:
    """Gate candidate content before anything is written: (gate_error, remaining_diagnostics).

    Parse errors and lint diagnostics *introduced by the edit* fail the gate;
    problems already present in the original are grandfathered and merely reported.
    """
    if lang:
        if syntax.has_parse_errors(candidate, lang) and not syntax.has_parse_errors(original, lang):
            return "the edited file no longer parses (syntax error introduced by the edit)", []
    command = _lint_command(lang, rel_path)
    if not command:
        return None, []
    baseline = _lint(command, root, original)
    current = _lint(command, root, candidate)
    if baseline is None or current is None:
        return None, []
    remaining = list(baseline)
    introduced: list[str] = []
    for diag in current:
        if diag in remaining:
            remaining.remove(diag)
        else:
            introduced.append(diag)
    if introduced:
        return "the edit introduces lint errors: " + "; ".join(introduced[:10]), []
    return None, current


async def _apply_one(root: Path, file: str, instruction: str, symbol: str | None = None) -> dict[str, Any]:
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
        lang = syntax.language_for(target)

        if symbol:
            start, end, region = _resolve_anchor(original, lang, symbol)
            messages = [
                {"role": "system", "content": ANCHORED_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": ANCHORED_USER_PROMPT_TEMPLATE.format(
                        file_path=target.as_posix(),
                        symbol=symbol,
                        start_line=start,
                        end_line=end,
                        content=region,
                        instruction=instruction,
                    ),
                },
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        file_path=target.as_posix(), content=original, instruction=instruction
                    ),
                },
            ]

        response = await get_groq_client().chat.completions.create(
            model=config.CODE_APPLY_MODEL,
            messages=messages,
            max_tokens=32768,
        )
        status, payload = _parse_response(response.choices[0].message.content or "")

        if status == "rejected":
            result.update(status="rejected", reason=payload)
            return result

        candidate = _splice(original, start, end, payload) if symbol else payload
        if not candidate.endswith("\n"):
            candidate += "\n"

        rel = target.relative_to(root).as_posix() if root in target.parents or target == root else target.as_posix()
        gate_error, lint_notes = await asyncio.to_thread(_run_gate, root, rel, lang, original, candidate)
        if gate_error:
            # Nothing is written on gate failure: the file on disk is untouched.
            result.update(status="gate_failed", reason=gate_error)
            return result

        new_content = _write_preserving_newlines(target, original, candidate)
        result.update(status="applied", diff=_unified_diff(rel, original, new_content) or "(no changes)")
        if lint_notes:
            result["lint"] = lint_notes
        return result
    except Exception as exc:
        result.update(status="error", reason=f"{type(exc).__name__}: {exc}")
        return result


async def run_code_apply(root_dir: str, edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return [
            {"file": e.get("file", ""), "status": "error", "reason": f"root_dir does not exist: {root_dir}"}
            for e in edits
        ]

    # Edits on different files run in parallel; edits on the same file run
    # sequentially in the order given, to avoid write races.
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for edit in edits:
        key = str((root / edit["file"]).resolve()) if not Path(edit["file"]).is_absolute() else str(Path(edit["file"]).resolve())
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(edit)

    async def run_group(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [await _apply_one(root, e["file"], e["query"], e.get("symbol")) for e in group]

    group_results = await asyncio.gather(*(run_group(groups[k]) for k in order))

    # Restore the caller's original edit order.
    by_group = {k: list(res) for k, res in zip(order, group_results)}
    results: list[dict[str, Any]] = []
    for edit in edits:
        key = str((root / edit["file"]).resolve()) if not Path(edit["file"]).is_absolute() else str(Path(edit["file"]).resolve())
        results.append(by_group[key].pop(0))
    return results
