"""Extract code entities from source files using tree-sitter AST parsing.

Supports PHP (the full grammar, including HTML interleaving in CakePHP
templates) with exact same output schema as the LLM entity extraction prompt.
Returns None for unsupported extensions or unparseable files — the caller then
skips entity extraction for that file (chunking falls back to character-based).

Schema v2 added resolution inputs on top of the v1 fields, all additive:
per-class `fqcn`/`start_line`/`end_line`/`heritage`, per-member
`member_kind`/`start_line`/`end_line`, per-method `param_types`/`call_sites`/
`prop_sites`/`class_refs`/`assignments`, and file-level structured `uses`.
v3 adds a file-level `di_bindings` list (DI container wiring), attached by
Phase 1.5 via di_parser — hence the version bump even though parse_entities
itself is unchanged. Positions are 1-based lines and 0-based UTF-16 code-unit
columns (the LSP wire format — tree-sitter's byte columns shift on lines
containing multibyte text, so the conversion happens here, once).
"""

from __future__ import annotations

import os
from typing import Any

import tree_sitter as ts

# Version of the entity dict schema produced by parse_entities. Bump when
# fields change shape; entities.json checkpoints with a different version are
# discarded and re-parsed.
ENTITIES_VERSION = 3

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

# Receiver/expression texts are advisory (type-tracker patterns like `$this`
# or `$this->Users`); cap them so chained expressions don't bloat entities.
_RECEIVER_MAX_CHARS = 160


def _php_simple_name(node: ts.Node | None) -> str:
    """Rightmost segment of a name node: `App\\Model\\Table\\UsersTable` → `UsersTable`."""
    if node is None:
        return ""
    return _node_text(node).rsplit("\\", 1)[-1]


def _line(node: ts.Node) -> int:
    """1-based start line of a node."""
    return node.start_point[0] + 1


def _utf16_col(node: ts.Node, source: bytes) -> int:
    """0-based UTF-16 code-unit column of a node's start (LSP position format).

    tree-sitter reports byte columns; any multibyte text earlier on the line
    (Japanese comments/strings) would silently shift LSP positions without
    this conversion.
    """
    line_start = node.start_byte - node.start_point[1]
    prefix = source[line_start:node.start_byte].decode("utf-8", errors="replace")
    return sum(2 if ord(c) > 0xFFFF else 1 for c in prefix)


def _receiver_text(node: ts.Node | None) -> str:
    if node is None:
        return ""
    return _node_text(node)[:_RECEIVER_MAX_CHARS]


def _php_modifiers(node: ts.Node) -> str:
    """Space-joined modifier keywords (`public`, `static`, `abstract`, ...)."""
    mods = []
    for c in node.children:
        if c.type in _PHP_MODIFIER_NODES:
            mods.append(_node_text(c))
    return " ".join(mods)


def _literal_string(node: ts.Node | None) -> str | None:
    """Content of a literal (non-interpolated) string node, else None.

    Escapes are decoded minimally: `\\\\` → `\\` and the quote escapes —
    enough for FQCN-bearing strings like 'App\\\\Model\\\\Table\\\\UsersTable'.
    """
    if node is None:
        return None
    if node.type in ("string", "encapsed_string"):
        # Single- and double-quoted literals are split into string_content +
        # escape_sequence children (e.g. 'A\\B' → "A", "\\", "B"). Decode the
        # backslash/quote escapes so FQCN strings come back whole; bail on
        # interpolation (variables/expressions), which is not a literal.
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(_node_text(c))
            elif c.type == "escape_sequence":
                raw = _node_text(c)
                parts.append({"\\\\": "\\", "\\\"": '"', "\\'": "'"}.get(raw, raw))
            else:
                return None  # interpolation — not a literal
        return "".join(parts)
    return None


# Strings that look like 'FQCN::method' callables
_CALLABLE_STRING = None  # compiled lazily to keep import cost nil


def _is_callable_string(value: str) -> bool:
    global _CALLABLE_STRING
    if _CALLABLE_STRING is None:
        import re
        _CALLABLE_STRING = re.compile(r"^\\?[A-Za-z_][\w\\]*::[A-Za-z_]\w*$")
    return bool(_CALLABLE_STRING.match(value))


def _str_args(call_node: ts.Node) -> list[str | None]:
    """Literal string contents of the first ≤3 arguments (None per non-literal).

    Convention resolution reads these (`fetchTable('Users')`,
    `addBehavior('Billing.Audit')`); positional Nones keep argument indexes.
    """
    args_node = call_node.child_by_field_name("arguments")
    out: list[str | None] = []
    if args_node is None:
        return out
    for a in args_node.named_children:
        if a.type != "argument":
            continue
        expr = a.named_children[-1] if a.named_children else None
        out.append(_literal_string(expr))
        if len(out) == 3:
            break
    return out


def _extract_member_data(body_node: ts.Node | None, source: bytes) -> dict[str, Any]:
    """Walk a method/function body collecting calls and resolution inputs.

    Returns legacy `calls` (deduped simple names — unchanged v1 behavior) plus
    v2 `call_sites`/`prop_sites`/`class_refs`/`assignments` with positions.
    Calls inside closures are attributed to the enclosing method (as before).
    """
    calls: list[str] = []
    call_sites: list[dict] = []
    prop_sites: list[dict] = []
    class_refs: list[dict] = []
    assignments: list[dict] = []

    def _site(name: str, kind: str, pos_node: ts.Node, receiver: str = "",
              qualified: str = "", call_node: ts.Node | None = None,
              dynamic: bool = False) -> dict:
        return {
            "name": name,
            "kind": kind,
            "line": _line(pos_node),
            "col": _utf16_col(pos_node, source),
            "receiver": receiver,
            "qualified": qualified,
            "str_args": _str_args(call_node) if call_node is not None else [],
            "dynamic": dynamic,
        }

    def _call_name_site(node: ts.Node) -> dict | None:
        """Build a call_site for a call-expression node, or None."""
        t = node.type
        if t == "function_call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in _PHP_NAME_NODES:
                full = _node_text(fn)
                simple = full.rsplit("\\", 1)[-1]
                return _site(simple, "function", fn,
                             qualified=full if full != simple else "",
                             call_node=node)
            if fn is not None:
                return _site("", "function", node,
                             receiver=_receiver_text(fn), call_node=node,
                             dynamic=True)
            return None
        if t in ("member_call_expression", "nullsafe_member_call_expression"):
            kind = "nullsafe" if t.startswith("nullsafe") else "method"
            name_node = node.child_by_field_name("name")
            obj = node.child_by_field_name("object")
            if name_node is not None and name_node.type == "name":
                return _site(_node_text(name_node), kind, name_node,
                             receiver=_receiver_text(obj), call_node=node)
            return _site("", kind, node, receiver=_receiver_text(obj),
                         call_node=node, dynamic=True)
        if t == "scoped_call_expression":
            name_node = node.child_by_field_name("name")
            scope = node.child_by_field_name("scope")
            if name_node is not None and name_node.type == "name":
                return _site(_node_text(name_node), "static", name_node,
                             receiver=_receiver_text(scope), call_node=node)
            return _site("", "static", node, receiver=_receiver_text(scope),
                         call_node=node, dynamic=True)
        if t == "object_creation_expression":
            cname = None
            for c in node.children:
                if c.type in _PHP_NAME_NODES:
                    cname = c
                    break
            if cname is not None:
                full = _node_text(cname)
                return _site(full.rsplit("\\", 1)[-1], "new", cname,
                             qualified=full, call_node=node)
            return None  # anonymous class / `new $cls` — no static target
        return None

    def _walk(node: ts.Node):
        t = node.type

        site = _call_name_site(node)
        if site is not None:
            call_sites.append(site)
            # Legacy v1 `calls`: named function/method/static callees only.
            if site["name"] and site["kind"] in ("function", "method", "nullsafe", "static"):
                calls.append(site["name"])

        elif t in ("member_access_expression", "nullsafe_member_access_expression"):
            name_node = node.child_by_field_name("name")
            obj = node.child_by_field_name("object")
            if name_node is not None and name_node.type == "name":
                prop_sites.append({
                    "receiver": _receiver_text(obj),
                    "name": _node_text(name_node),
                    "line": _line(name_node),
                    "col": _utf16_col(name_node, source),
                })

        elif t == "class_constant_access_expression":
            named = node.named_children
            if len(named) >= 2 and named[-1].type == "name" \
                    and _node_text(named[-1]) == "class" \
                    and named[0].type in _PHP_NAME_NODES:
                class_refs.append({
                    "text": _node_text(named[0]),
                    "kind": "class_literal",
                    "line": _line(named[0]),
                    "col": _utf16_col(named[0], source),
                })

        elif t == "binary_expression":
            # `$x instanceof Foo` — the right operand is a class reference.
            # Only name nodes AFTER the instanceof token count (the left
            # operand may itself parse as a bare `name`, e.g. a constant).
            kids = node.children
            for i, c in enumerate(kids):
                if c.type == "instanceof":
                    for rc in kids[i + 1:]:
                        if rc.type in _PHP_NAME_NODES:
                            class_refs.append({
                                "text": _node_text(rc),
                                "kind": "instanceof",
                                "line": _line(rc),
                                "col": _utf16_col(rc, source),
                            })
                            break
                    break

        elif t in ("string", "encapsed_string"):
            # Standalone 'FQCN::method' callable strings (array elements,
            # assignments, arguments) — PHP resolves them as fully qualified.
            lit = _literal_string(node)
            if lit and _is_callable_string(lit):
                cls_part, member = lit.rsplit("::", 1)
                class_refs.append({
                    "text": cls_part,
                    "kind": "callable_string",
                    "name": member,
                    "line": _line(node),
                    "col": _utf16_col(node, source),
                })

        elif t == "array_creation_expression":
            # [$obj, 'method'] callable pairs
            elems = [c for c in node.named_children
                     if c.type == "array_element_initializer"]
            if len(elems) == 2:
                second = elems[1].named_children[-1] if elems[1].named_children else None
                lit = _literal_string(second)
                if lit is not None and elems[0].named_children:
                    class_refs.append({
                        "text": _receiver_text(elems[0].named_children[-1]),
                        "kind": "array_callable",
                        "name": lit,
                        "line": _line(node),
                        "col": _utf16_col(node, source),
                    })

        elif t == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "variable_name":
                var = _node_text(left)
                if right.type == "object_creation_expression":
                    cname = None
                    for c in right.children:
                        if c.type in _PHP_NAME_NODES:
                            cname = c
                            break
                    if cname is not None:
                        assignments.append({
                            "var": var, "rhs_kind": "new",
                            "name": _php_simple_name(cname),
                            "qualified": _node_text(cname),
                            "line": _line(cname),
                            "col": _utf16_col(cname, source),
                        })
                    else:
                        # `new $cls()` — runtime class; receiver is dynamic
                        assignments.append({
                            "var": var, "rhs_kind": "dynamic",
                            "line": _line(right),
                        })
                elif right.type in ("member_call_expression",
                                    "nullsafe_member_call_expression",
                                    "scoped_call_expression",
                                    "function_call_expression"):
                    inner = _call_name_site(right)
                    if inner is not None and inner["name"]:
                        assignments.append({
                            "var": var, "rhs_kind": "call",
                            "name": inner["name"],
                            "line": inner["line"], "col": inner["col"],
                        })
                elif right.type in ("member_access_expression",
                                    "nullsafe_member_access_expression"):
                    name_node = right.child_by_field_name("name")
                    obj = right.child_by_field_name("object")
                    if name_node is not None and name_node.type == "name":
                        assignments.append({
                            "var": var, "rhs_kind": "prop",
                            "receiver": _receiver_text(obj),
                            "name": _node_text(name_node),
                        })
                elif right.type == "variable_name":
                    assignments.append({
                        "var": var, "rhs_kind": "var",
                        "name": _node_text(right),
                    })

        for child in node.children:
            _walk(child)

    if body_node is not None:
        _walk(body_node)

    return {
        "calls": list(dict.fromkeys(calls)),  # dedupe preserving order
        "call_sites": call_sites,
        "prop_sites": prop_sites,
        "class_refs": class_refs,
        "assignments": assignments,
    }


def _param_types(params_node: ts.Node | None) -> dict[str, str]:
    """`$var` → declared type text, for typed (incl. promoted) parameters."""
    out: dict[str, str] = {}
    if params_node is None:
        return out
    for p in params_node.named_children:
        if p.type not in ("simple_parameter", "property_promotion_parameter",
                          "variadic_parameter"):
            continue
        tnode = p.child_by_field_name("type")
        nnode = p.child_by_field_name("name")
        if tnode is not None and nnode is not None:
            out[_node_text(nnode)] = _node_text(tnode)
    return out


def _extract_php_function(node: ts.Node, source: bytes,
                          member_kind: str = "method") -> dict | None:
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
        # Collapse newlines/indentation (multi-line promoted-constructor params
        # would otherwise flow verbatim into the Methods.signature column).
        params = " ".join(_node_text(params_node)[1:-1].split()).rstrip(",")

    ret_type = ""
    ret_node = node.child_by_field_name("return_type")
    if ret_node is not None:
        ret_type = _node_text(ret_node)

    member = {
        "name": name,
        "modifiers": _php_modifiers(node),
        "return_type": ret_type,
        "parameters": params,
        "member_kind": member_kind,
        "start_line": _line(node),
        "end_line": node.end_point[0] + 1,
        "param_types": _param_types(params_node),
    }
    member.update(_extract_member_data(node.child_by_field_name("body"), source))
    return member


def _extract_php_properties(node: ts.Node, source: bytes) -> list[dict]:
    """`property_declaration` → zero-parameter members.

    Properties are surfaced as zero-parameter members so CakePHP entities keep
    their fields in the graph. `return_type` records the declared type; calls
    in default values (rare) are not extracted, so `calls` stays empty.
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
                "member_kind": "property",
                "start_line": _line(node),
                "end_line": node.end_point[0] + 1,
                "param_types": {},
                "calls": [],
                "call_sites": [],
                "prop_sites": [],
                "class_refs": [],
                "assignments": [],
            })
    return out


def _process_php_type(node: ts.Node, classes: list[dict], source: bytes) -> None:
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
    heritage: list[dict] = []
    methods: list[dict] = []

    def _heritage_entry(ref_node: ts.Node, relation: str) -> dict:
        return {
            "name": _php_simple_name(ref_node),
            "qualified": _node_text(ref_node),
            "relation": relation,
            "line": _line(ref_node),
            "col": _utf16_col(ref_node, source),
        }

    for c in node.children:
        if c.type == "base_clause":
            # `extends` — single class for classes, possibly many for interfaces
            for bc in c.children:
                if bc.type in _PHP_NAME_NODES:
                    base_classes.append(_php_simple_name(bc))
                    heritage.append(_heritage_entry(bc, "extends"))
        elif c.type == "class_interface_clause":
            # `implements`
            for ic in c.children:
                if ic.type in _PHP_NAME_NODES:
                    interfaces.append(_php_simple_name(ic))
                    heritage.append(_heritage_entry(ic, "implements"))

    # Properties/enum cases are collected apart from real methods: Phase 8
    # keys method IDs by (file, class, name), so a property whose name matches
    # a method would silently share the method's graph row.
    pseudo_members: list[dict] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for member in body.children:
            if member.type == "method_declaration":
                m = _extract_php_function(member, source, member_kind="method")
                if m:
                    methods.append(m)
            elif member.type == "property_declaration":
                pseudo_members.extend(_extract_php_properties(member, source))
            elif member.type == "use_declaration":
                # Trait use — recorded alongside interfaces, as a mixin
                for un in member.children:
                    if un.type in _PHP_NAME_NODES:
                        interfaces.append(_php_simple_name(un))
                        heritage.append(_heritage_entry(un, "uses"))
            elif member.type == "enum_case":
                cn = member.child_by_field_name("name")
                if cn is not None:
                    pseudo_members.append({
                        "name": _node_text(cn), "modifiers": "case",
                        "return_type": "", "parameters": "",
                        "member_kind": "enum_case",
                        "start_line": _line(member),
                        "end_line": member.end_point[0] + 1,
                        "param_types": {},
                        "calls": [], "call_sites": [], "prop_sites": [],
                        "class_refs": [], "assignments": [],
                    })

    method_names = {m["name"] for m in methods}
    seen_pseudo: set[str] = set()
    for p in pseudo_members:
        if p["name"] in method_names or p["name"] in seen_pseudo:
            continue
        seen_pseudo.add(p["name"])
        methods.append(p)

    classes.append({
        "name": name,
        "kind": kind,
        "modifiers": _php_modifiers(node),
        "fqcn": "",  # filled by _parse_php once the namespace is known
        "start_line": _line(node),
        "end_line": node.end_point[0] + 1,
        "base_classes": base_classes,
        "interfaces": list(dict.fromkeys(interfaces)),
        "heritage": heritage,
        "methods": methods,
    })


def _collect_uses(root: ts.Node, source: bytes) -> list[dict]:
    """Structured `use` imports: FQCN + alias + kind + position.

    Handles the group form `use Foo\\{Bar, Baz as Qux}` correctly (the legacy
    `imports` list records group clauses without their `Foo\\` prefix).
    """
    uses: list[dict] = []

    def _handle_clause(clause: ts.Node, prefix: str, kind: str):
        target: ts.Node | None = None
        alias = ""
        for cc in clause.children:
            # `use function Foo\bar;` puts the keyword inside the clause
            if cc.type == "function":
                kind = "function"
            elif cc.type == "const":
                kind = "const"
            elif cc.type in _PHP_NAME_NODES or cc.type == "namespace_name":
                if target is None:
                    target = cc
                else:
                    # Group form: `use Foo\{Bar as Baz}` puts the alias as a
                    # bare trailing `name` node (no aliasing-clause wrapper).
                    alias = _node_text(cc)
            elif cc.type == "namespace_aliasing_clause":
                for an in cc.children:
                    if an.type == "name":
                        alias = _node_text(an)
        if target is None:
            return
        fq = _node_text(target)
        if prefix:
            fq = f"{prefix}\\{fq}"
        uses.append({
            "fqcn": fq,
            "alias": alias or fq.rsplit("\\", 1)[-1],
            "kind": kind,
            "line": _line(target),
            "col": _utf16_col(target, source),
        })

    def _walk(n: ts.Node):
        if n.type == "namespace_use_declaration":
            kind = "class"
            prefix = ""
            group: ts.Node | None = None
            for c in n.children:
                if c.type == "function":
                    kind = "function"
                elif c.type == "const":
                    kind = "const"
                elif c.type == "namespace_name":
                    prefix = _node_text(c)
                elif c.type == "namespace_use_group":
                    group = c
            if group is not None:
                for cl in group.children:
                    if cl.type == "namespace_use_clause":
                        _handle_clause(cl, prefix, kind)
            else:
                for cl in n.children:
                    if cl.type == "namespace_use_clause":
                        _handle_clause(cl, "", kind)
        for child in n.children:
            _walk(child)

    _walk(root)
    return uses


def _parse_php(tree: ts.Tree, file_path: str, source: bytes) -> dict[str, Any]:
    """Parse a PHP file AST into the entity schema."""
    root = tree.root_node
    namespace = ""
    imports: list[str] = []
    classes: list[dict] = []
    global_methods: list[dict] = []

    # Imports: `use Foo\Bar;` clauses plus literal include/require targets.
    # Walks the whole tree so declarations inside braced namespaces are caught.
    def _use_clauses(n: ts.Node) -> list[ts.Node]:
        # Clauses sit directly under the declaration, or nested inside a
        # namespace_use_group for the `use Foo\{Bar, Baz as Qux};` form.
        found: list[ts.Node] = []
        for c in n.children:
            if c.type == "namespace_use_clause":
                found.append(c)
            else:
                found.extend(_use_clauses(c))
        return found

    def _walk_imports(node: ts.Node):
        if node.type == "namespace_use_declaration":
            clauses = _use_clauses(node)
            if clauses:
                for c in clauses:
                    imports.append(_node_text(c))
            else:
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
                _process_php_type(c, classes, source)
            elif t == "function_definition":
                m = _extract_php_function(c, source, member_kind="function")
                if m:
                    global_methods.append(m)

    _walk_top(root.children)

    # Top-level functions go into a pseudo-class (matches the LLM schema).
    if global_methods:
        classes.append({
            "name": "(global)",
            "kind": "module",
            "modifiers": "",
            "fqcn": "",
            "start_line": 1,
            "end_line": root.end_point[0] + 1,
            "base_classes": [],
            "interfaces": [],
            "heritage": [],
            "methods": global_methods,
        })

    # FQCNs need the file-level namespace, which the walk discovers along the
    # way — assign in a post-pass. The (global) pseudo-class keeps fqcn "".
    for cls in classes:
        if cls["name"] != "(global)":
            cls["fqcn"] = f"{namespace}\\{cls['name']}" if namespace else cls["name"]

    return {
        "file_path": file_path,
        "namespace": namespace,
        "classes": classes,
        "imports": list(dict.fromkeys(imports)),
        "uses": _collect_uses(root, source),
    }


def _php_find_string_content(node: ts.Node) -> str:
    """First literal string content under *node* (for include/require targets).

    Handles `'a.php'` (string), `"a.php"` (encapsed_string), and parenthesized
    forms. For concatenations (`__DIR__ . '/app.php'`) the first literal
    fragment is returned; purely dynamic targets (variables) return "".
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
                            # Clamp: with a tiny budget the header allowance
                            # would make the step <= 0 and loop forever.
                            step = max(1, max_chars - 200)  # room for context header
                            pos = 0
                            part_idx = 0
                            while pos < member_size:
                                chunk_text = member_src[pos:pos + step]
                                part_idx += 1
                                units.append(f"{context} part {part_idx}\n{chunk_text}")
                                pos += step

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
                step = max(1, max_chars - 200)  # clamped: see member split above
                pos = 0
                part_idx = 0
                while pos < len(src):
                    chunk_text = src[pos:pos + step]
                    part_idx += 1
                    units.append(f"{context} part {part_idx}\n{chunk_text}")
                    pos += step

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
        Entity dict matching the LLM extraction schema (v2, see module
        docstring), or None if language unsupported.
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
        source = content.encode("utf-8", errors="replace")
        tree = parser.parse(source)
        return parse_fn(tree, file_path, source)
    except Exception:
        return None
