"""Extract code entities from source files using tree-sitter AST parsing.

Supports PHP (the full grammar, including HTML interleaving in CakePHP
templates) with exact same output schema as the LLM entity extraction prompt.
Returns None for unsupported languages so the caller can fall back to LLM.
"""

from __future__ import annotations

import os
from typing import Any

import tree_sitter as ts

# Lazy-loaded language objects
_languages: dict[str, ts.Language] = {}
_parsers: dict[str, ts.Parser] = {}

# Extension → language name mapping (.ctp = CakePHP ≤3 view templates)
_EXT_TO_LANG: dict[str, str] = {
    ".php": "php",
    ".ctp": "php",
}


_missing_langs: set[str] = set()


def _get_parser(lang: str) -> ts.Parser | None:
    """Get or create a parser for the given language."""
    if lang in _parsers:
        return _parsers[lang]
    if lang in _missing_langs:
        return None

    try:
        if lang == "php":
            import tree_sitter_php as mod
            # language_php() is the full grammar (PHP + interleaved HTML),
            # which CakePHP templates need; language_php_only() would reject them.
            language = ts.Language(mod.language_php())
        else:
            return None

        parser = ts.Parser(language)
        _languages[lang] = language
        _parsers[lang] = parser
        return parser
    except ImportError:
        _missing_langs.add(lang)
        return None
    except Exception:
        return None


def _report_missing_langs():
    """Print install instructions for missing tree-sitter packages."""
    if not _missing_langs:
        return
    pkg_map = {
        "php": "tree-sitter-php",
    }
    pkgs = sorted({pkg_map.get(lang, f"tree-sitter-{lang}") for lang in _missing_langs})
    print(f"\n  WARNING: Tree-sitter packages not installed for: {', '.join(sorted(_missing_langs))}")
    print(f"  Install with: pip install {' '.join(pkgs)}\n")


def _node_text(node: ts.Node) -> str:
    return node.text.decode("utf-8", errors="replace")


# ===================================================================
# PHP extraction
# ===================================================================


_PHP_TYPE_NODES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "trait",
    "enum_declaration": "enum",
}

# Modifiers appear as direct (unnamed-field) children of declarations.
_PHP_MODIFIER_NODES = {
    "visibility_modifier", "static_modifier", "final_modifier",
    "abstract_modifier", "readonly_modifier", "var_modifier",
}

# Nodes that carry a (possibly qualified) name reference.
_PHP_NAME_NODES = ("name", "qualified_name", "relative_name")

_PHP_INCLUDE_NODES = (
    "include_expression", "include_once_expression",
    "require_expression", "require_once_expression",
)


def _php_simple_name(node: ts.Node | None) -> str:
    """Rightmost segment of a name node: `App\\Model\\Table\\UsersTable` → `UsersTable`.

    Phase 9 matches inheritance/call edges by simple name, so qualified names
    must be reduced to their final segment.
    """
    if node is None:
        return ""
    return _node_text(node).rsplit("\\", 1)[-1]


def _php_modifiers(node: ts.Node) -> str:
    """Space-joined modifier keywords (`public`, `static`, `abstract`, ...)."""
    mods = []
    for c in node.children:
        if c.type in _PHP_MODIFIER_NODES:
            mods.append(_node_text(c))
    return " ".join(mods)


def _extract_php_invocations(body_node: ts.Node | None) -> list[str]:
    """Extract called names from a method/function body.

    Covers `foo()`, `$obj->foo()`, `$obj?->foo()`, and `Foo::bar()`. Dynamic
    callees (`$this->$method()`, `$fn()`) have no `name` node and are skipped.
    """
    calls: list[str] = []

    def _walk(node: ts.Node):
        t = node.type
        if t == "function_call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in _PHP_NAME_NODES:
                calls.append(_php_simple_name(fn))
        elif t in ("member_call_expression", "nullsafe_member_call_expression",
                   "scoped_call_expression"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "name":
                calls.append(_node_text(name_node))
        for child in node.children:
            _walk(child)

    if body_node is not None:
        _walk(body_node)
    return list(dict.fromkeys(calls))  # dedupe preserving order


def _extract_php_function(node: ts.Node) -> dict | None:
    """Build a method dict from a `method_declaration` or `function_definition`.

    Both node types expose the same fields (name / parameters / return_type /
    body); an abstract method simply has no `body`.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _node_text(name_node)
    if not name:
        return None

    params = ""
    params_node = node.child_by_field_name("parameters")
    if params_node is not None:
        params = _node_text(params_node)[1:-1].strip()

    ret_type = ""
    ret_node = node.child_by_field_name("return_type")
    if ret_node is not None:
        ret_type = _node_text(ret_node)

    return {
        "name": name,
        "modifiers": _php_modifiers(node),
        "return_type": ret_type,
        "parameters": params,
        "calls": _extract_php_invocations(node.child_by_field_name("body")),
    }


def _extract_php_properties(node: ts.Node) -> list[dict]:
    """`property_declaration` → zero-parameter members.

    Mirrors how the graph treats Kotlin properties / Ruby attr_* accessors:
    properties appear as members so CakePHP entities keep their fields in the
    graph. `return_type` records the declared type; `calls` captures default
    values' calls (rare) — kept empty for simplicity.
    """
    mods = _php_modifiers(node)
    type_node = node.child_by_field_name("type")
    ptype = _node_text(type_node) if type_node is not None else ""

    out: list[dict] = []
    for c in node.children:
        if c.type != "property_element":
            continue
        vn = c.child_by_field_name("name")
        if vn is None:
            for cc in c.children:
                if cc.type == "variable_name":
                    vn = cc
                    break
        if vn is None:
            continue
        pname = _node_text(vn).lstrip("$")
        if pname:
            out.append({
                "name": pname,
                "modifiers": mods,
                "return_type": ptype,
                "parameters": "",
                "calls": [],
            })
    return out


def _process_php_type(node: ts.Node, classes: list[dict]) -> None:
    """Emit a class/interface/trait/enum entry from a type declaration node."""
    kind = _PHP_TYPE_NODES.get(node.type)
    if kind is None:
        return
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node)
    if not name:
        return

    base_classes: list[str] = []
    interfaces: list[str] = []
    methods: list[dict] = []

    for c in node.children:
        if c.type == "base_clause":
            # `extends` — single class for classes, possibly many for interfaces
            for bc in c.children:
                if bc.type in _PHP_NAME_NODES:
                    base_classes.append(_php_simple_name(bc))
        elif c.type == "class_interface_clause":
            # `implements`
            for ic in c.children:
                if ic.type in _PHP_NAME_NODES:
                    interfaces.append(_php_simple_name(ic))

    body = node.child_by_field_name("body")
    if body is not None:
        for member in body.children:
            if member.type == "method_declaration":
                m = _extract_php_function(member)
                if m:
                    methods.append(m)
            elif member.type == "property_declaration":
                methods.extend(_extract_php_properties(member))
            elif member.type == "use_declaration":
                # Trait use — record like a mixin (matches Ruby `include`)
                for un in member.children:
                    if un.type in _PHP_NAME_NODES:
                        interfaces.append(_php_simple_name(un))
            elif member.type == "enum_case":
                cn = member.child_by_field_name("name")
                if cn is not None:
                    methods.append({
                        "name": _node_text(cn), "modifiers": "case",
                        "return_type": "", "parameters": "", "calls": [],
                    })

    classes.append({
        "name": name,
        "kind": kind,
        "modifiers": _php_modifiers(node),
        "base_classes": base_classes,
        "interfaces": list(dict.fromkeys(interfaces)),
        "methods": methods,
    })


def _parse_php(tree: ts.Tree, file_path: str) -> dict[str, Any]:
    """Parse a PHP file AST into the entity schema."""
    root = tree.root_node
    namespace = ""
    imports: list[str] = []
    classes: list[dict] = []
    global_methods: list[dict] = []

    # Imports: `use Foo\Bar;` clauses plus literal include/require targets.
    # Walks the whole tree so declarations inside braced namespaces are caught.
    def _walk_imports(node: ts.Node):
        if node.type == "namespace_use_declaration":
            clauses = [c for c in node.children if c.type == "namespace_use_clause"]
            if clauses:
                for c in clauses:
                    imports.append(_node_text(c))
            else:
                # Group form `use Foo\{Bar, Baz};` — keep the raw declaration
                imports.append(_node_text(node).removeprefix("use ").rstrip(";"))
        elif node.type in _PHP_INCLUDE_NODES:
            content = _php_find_string_content(node)
            if content:
                imports.append(content)
        for child in node.children:
            _walk_imports(child)

    _walk_imports(root)

    def _walk_top(nodes):
        nonlocal namespace
        for c in nodes:
            t = c.type
            if t == "namespace_definition":
                nn = c.child_by_field_name("name")
                if nn is not None and not namespace:
                    namespace = _node_text(nn)
                body = c.child_by_field_name("body")
                if body is not None:  # rare braced form: namespace Foo { ... }
                    _walk_top(body.children)
            elif t in _PHP_TYPE_NODES:
                _process_php_type(c, classes)
            elif t == "function_definition":
                m = _extract_php_function(c)
                if m:
                    global_methods.append(m)

    _walk_top(root.children)

    # Top-level functions go into a pseudo-class (matches the LLM schema).
    if global_methods:
        classes.append({
            "name": "(global)",
            "kind": "module",
            "modifiers": "",
            "base_classes": [],
            "interfaces": [],
            "methods": global_methods,
        })

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": list(dict.fromkeys(imports)),
    }


def _php_find_string_content(node: ts.Node) -> str:
    """First literal string content under *node* (for include/require targets).

    Handles `'a.php'` (string), `"a.php"` (encapsed_string), and parenthesized
    forms. Dynamic targets (variables, concatenation) return "".
    """
    if node.type == "string_content":
        return _node_text(node)
    for child in node.children:
        found = _php_find_string_content(child)
        if found:
            return found
    return ""


_LANG_PARSERS = {
    "php": _parse_php,
}


# ===================================================================
# Structural chunking
# ===================================================================


def _node_source(node: ts.Node, source: bytes) -> str:
    """Extract the source text for an AST node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# Type declarations we split into member-level units
_TYPE_DECLS = {
    "class_declaration", "interface_declaration",
    "trait_declaration", "enum_declaration",
}

# Containers that hold type declarations (PHP class bodies)
_CONTAINERS = {
    "declaration_list",
}


def _collect_units(node: ts.Node, source: bytes, max_chars: int) -> list[str]:
    """Walk an AST node and collect semantic units that fit within max_chars.

    A "unit" is a complete, self-contained piece of code: a use block,
    a class, a method, a field, etc. Large classes are broken into
    member-level units. Oversized single members get character-split
    with a context header.
    """
    units: list[str] = []

    # Group consecutive use declarations
    usings: list[str] = []
    i = 0
    children = node.children
    while i < len(children):
        child = children[i]
        if child.type == "namespace_use_declaration":
            usings.append(_node_source(child, source))
            i += 1
            continue
        if usings:
            units.append("\n".join(usings))
            usings = []

        size = child.end_byte - child.start_byte

        if child.type in _TYPE_DECLS:
            if size <= max_chars:
                # Whole class fits — keep it as one unit
                units.append(_node_source(child, source))
            else:
                # Class too large — break into members
                class_name = ""
                for cc in child.children:
                    if cc.type == "name":
                        class_name = _node_source(cc, source)
                        break

                # Collect the class header (modifiers, name, base clause)
                header_parts = []
                for cc in child.children:
                    if cc.type in _CONTAINERS or cc.type == "enum_declaration_list":
                        break
                    header_parts.append(_node_source(cc, source))
                header = " ".join(header_parts)

                for cc in child.children:
                    if cc.type not in _CONTAINERS and cc.type != "enum_declaration_list":
                        continue
                    for member in cc.children:
                        if not member.is_named:
                            continue
                        member_src = _node_source(member, source)
                        member_size = len(member_src)

                        if member_size <= max_chars:
                            # Add class context so LLM knows where this belongs
                            units.append(f"// class {class_name}\n{header} {{\n{member_src}\n}}")
                        else:
                            # Single member too large — character-split with context
                            member_name = ""
                            for mn in member.children:
                                if mn.type == "name":
                                    member_name = _node_source(mn, source)
                                    break
                            context = f"// class {class_name}, member {member_name} ({member_size:,} chars, split)"
                            pos = 0
                            part_idx = 0
                            while pos < member_size:
                                end = pos + max_chars - 200  # room for context header
                                chunk_text = member_src[pos:end]
                                part_idx += 1
                                units.append(f"{context} part {part_idx}\n{chunk_text}")
                                pos = end

        else:
            # Other top-level nodes (php_tag, text, namespace statements,
            # expressions, comments, etc.)
            src = _node_source(child, source)
            if not src.strip():
                i += 1
                continue
            if len(src) <= max_chars:
                units.append(src)
            else:
                # Oversized node — character-split with context
                context = f"// {child.type} (line {child.start_point[0]+1}, {len(src):,} chars, split)"
                pos = 0
                part_idx = 0
                while pos < len(src):
                    end = pos + max_chars - 200
                    chunk_text = src[pos:end]
                    part_idx += 1
                    units.append(f"{context} part {part_idx}\n{chunk_text}")
                    pos = end

        i += 1

    if usings:
        units.append("\n".join(usings))

    return units


def chunk_by_structure(file_path: str, content: str, max_chars: int = 800_000) -> list[str] | None:
    """Split a source file into semantically meaningful chunks using tree-sitter.

    Each chunk contains complete code structures (classes, methods, fields).
    Returns None if the language is unsupported (caller should fall back).

    Args:
        file_path: Path to determine language
        content: Full source code
        max_chars: Max characters per chunk

    Returns:
        List of chunk strings, or None if language unsupported.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None

    parser = _get_parser(lang)
    if not parser:
        return None

    try:
        source = content.encode("utf-8", errors="replace")
        tree = parser.parse(source)
    except Exception:
        return None

    # Use 95% of budget for units to leave room for join separators and headers
    unit_budget = int(max_chars * 0.95)
    units = _collect_units(tree.root_node, source, unit_budget)
    if not units:
        return [content]  # fallback: whole file as single chunk

    # Pack units into chunks greedily
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for unit in units:
        unit_size = len(unit) + 2  # account for "\n\n" separator
        if current and current_size + unit_size > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_size = 0
        current.append(unit)
        current_size += unit_size

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def parse_entities(file_path: str, content: str) -> dict[str, Any] | None:
    """Parse a source file and extract entities using tree-sitter.

    Args:
        file_path: Path to the source file (used to determine language and for output)
        content: Source code content as string

    Returns:
        Entity dict matching the LLM extraction schema, or None if language unsupported.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None

    parser = _get_parser(lang)
    if not parser:
        return None

    parse_fn = _LANG_PARSERS.get(lang)
    if not parse_fn:
        return None

    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return parse_fn(tree, file_path)
    except Exception:
        return None
