"""Shared tree-sitter plumbing for the deterministic tools.

Used by code_map (symbol outlines), code_find (AST-expanded search hits) and the
code_apply gate (anchor resolution + parse check). Parsers come from
tree-sitter-language-pack: parse on demand, no persistent index, no cache to
invalidate — it's milliseconds per file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterator

try:
    from tree_sitter import Node, Parser, Tree
    from tree_sitter_language_pack import get_parser as _get_ts_parser

    AVAILABLE = True
except ImportError:  # pragma: no cover - degraded mode without the packages
    Node = Parser = Tree = None  # type: ignore[assignment]
    AVAILABLE = False

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "bash",
    ".bash": "bash",
}

# Node types that count as a symbol definition, per language.
_DEFINITION_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "method_definition",
    },
    "typescript": {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    },
    "java": {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "method_declaration",
        "constructor_declaration",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item"},
    "ruby": {"method", "singleton_method", "class", "module"},
    "c": {"function_definition", "struct_specifier", "enum_specifier", "type_definition"},
    "cpp": {
        "function_definition",
        "struct_specifier",
        "enum_specifier",
        "type_definition",
        "class_specifier",
        "namespace_definition",
    },
    "csharp": {
        "class_declaration",
        "interface_declaration",
        "struct_declaration",
        "enum_declaration",
        "record_declaration",
        "method_declaration",
        "constructor_declaration",
        "namespace_declaration",
    },
    "php": {
        "function_definition",
        "method_declaration",
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
    },
    "swift": {"function_declaration", "class_declaration", "protocol_declaration"},
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "scala": {"function_definition", "class_definition", "object_definition", "trait_definition"},
    "lua": {"function_declaration"},
    "bash": {"function_definition"},
}
_DEFINITION_TYPES["tsx"] = _DEFINITION_TYPES["typescript"]

# Container-like definitions whose direct members are worth one level of outline nesting.
_CLASS_LIKE = {
    "class_definition",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "class_specifier",
    "struct_specifier",
    "struct_declaration",
    "namespace_definition",
    "namespace_declaration",
    "impl_item",
    "trait_item",
    "trait_declaration",
    "trait_definition",
    "mod_item",
    "class",
    "module",
    "object_declaration",
    "object_definition",
    "record_declaration",
    "protocol_declaration",
}

# Wrapper nodes to see through when their child is the real definition.
_WRAPPER_TYPES = {"decorated_definition", "export_statement"}
# const foo = () => {} counts as a function definition via its declarator.
_FUNCTION_VALUE_TYPES = {"arrow_function", "function_expression", "function"}
_DECLARATION_LIST_TYPES = {"lexical_declaration", "variable_declaration"}

_MAX_PARSE_BYTES = 1_000_000
_MAX_SIGNATURE_CHARS = 160


def language_for(path: Path) -> str | None:
    """Language name for a file, or None when we have no parser mapping for it."""
    return _EXT_TO_LANG.get(path.suffix.lower())


@lru_cache(maxsize=None)
def _parser(lang: str):
    try:
        return _get_ts_parser(lang)
    except Exception:
        return None


def parse(source: bytes, lang: str):
    """Parse source bytes; returns a Tree or None when unsupported/too large."""
    if not AVAILABLE or len(source) > _MAX_PARSE_BYTES:
        return None
    parser = _parser(lang)
    if parser is None:
        return None
    try:
        return parser.parse(source)
    except Exception:
        return None


def parse_file(path: Path):
    """Parse a file from disk; returns (tree, source_bytes) or (None, b"")."""
    lang = language_for(path)
    if not lang:
        return None, b""
    try:
        source = path.read_bytes()
    except OSError:
        return None, b""
    return parse(source, lang), source


def has_parse_errors(source: str, lang: str) -> bool | None:
    """True/False when we can check the syntax, None when we can't."""
    tree = parse(source.encode("utf-8"), lang)
    if tree is None:
        return None
    return tree.root_node.has_error


def is_definition(node, lang: str) -> bool:
    if node.type in _DEFINITION_TYPES.get(lang, set()):
        return True
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        return value is not None and value.type in _FUNCTION_VALUE_TYPES
    return False


def expand_definition(node):
    """Grow a definition to include its wrapper (decorators, export keyword, const)."""
    current = node
    while current.parent is not None and (
        current.parent.type in _WRAPPER_TYPES or current.parent.type in _DECLARATION_LIST_TYPES
    ):
        current = current.parent
    return current


def _unwrap(node):
    """See through wrapper nodes to the definition they carry, if any."""
    if node.type in _WRAPPER_TYPES:
        inner = node.child_by_field_name("definition") or node.child_by_field_name("declaration")
        if inner is not None:
            return inner
        for child in node.named_children:
            return child
    return node


def node_name(node) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return name.text.decode("utf-8", "replace")
    return None


def node_lines(node) -> tuple[int, int]:
    """1-indexed inclusive (start_line, end_line) of a node."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def signature(node, source: bytes) -> str:
    """One-line signature: node text up to its body, whitespace collapsed."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None and body.start_byte > node.start_byte else node.end_byte
    text = source[node.start_byte : end].decode("utf-8", "replace")
    text = " ".join(text.split()).strip(" {:")
    first_line = text.splitlines()[0] if "\n" in text else text
    if len(first_line) > _MAX_SIGNATURE_CHARS:
        first_line = first_line[:_MAX_SIGNATURE_CHARS] + "…"
    return first_line


def _definition_children(node, lang: str) -> Iterator:
    """Direct member definitions of a container node (or top-level of the file)."""
    body = node.child_by_field_name("body")
    scope = body if body is not None else node
    for child in scope.named_children:
        child = _unwrap(child)
        if child.type in _DECLARATION_LIST_TYPES:
            for declarator in child.named_children:
                if declarator.type == "variable_declarator" and is_definition(declarator, lang):
                    yield declarator
        elif is_definition(child, lang):
            yield child


def outline(tree, source: bytes, lang: str) -> list[tuple[int, str]]:
    """Top-level symbols with signatures as (depth, signature) rows.

    Container definitions (classes, interfaces, impls) additionally list their
    direct member definitions one level deep. No prose, no descriptions.
    """
    rows: list[tuple[int, str]] = []
    for top in _definition_children(tree.root_node, lang):
        rows.append((0, signature(top, source)))
        if top.type in _CLASS_LIKE:
            for member in _definition_children(top, lang):
                rows.append((1, signature(member, source)))
    return rows


def iter_definitions(tree, lang: str) -> Iterator:
    """Every definition node in the tree, depth-first, iteratively."""
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if is_definition(node, lang):
            yield node
        stack.extend(reversed(node.named_children))


def find_definitions(tree, lang: str, name: str) -> list:
    """All definition nodes whose name matches, in source order."""
    return sorted(
        (node for node in iter_definitions(tree, lang) if node_name(node) == name),
        key=lambda n: n.start_byte,
    )


def enclosing_definition(tree, lang: str, line: int, column: int = 0):
    """Smallest definition node containing the given 1-indexed line, or None."""
    row = line - 1
    node = tree.root_node.descendant_for_point_range((row, column), (row, column))
    while node is not None:
        if is_definition(node, lang):
            return node
        node = node.parent
    return None


def name_node_at(tree, line: int, column: int):
    """The named leaf at a 1-indexed line / 0-indexed column, or None."""
    row = line - 1
    return tree.root_node.descendant_for_point_range((row, column), (row, column))


def is_definition_name(node, lang: str) -> bool:
    """True when this leaf is the name being *defined* (vs. a mere reference)."""
    parent = node.parent
    if parent is None:
        return False
    if is_definition(parent, lang) and parent.child_by_field_name("name") == node:
        return True
    return False
