"""Extract dependency-injection wiring from PHP source (tree-sitter, deterministic).

Static analysis sees type hints and `use` imports, but the runtime *wiring* — which
concrete implements an interface, and which service is injected with which
dependency — lives in a DI container definition. In CakePHP that is
`Application::services(ContainerInterface $container)` (and `ServiceProvider::services`),
using `league/container` conventions. All the useful forms are `::class`-based, so we
can extract them statically:

    $container->add(Interface::class, Concrete::class);          -> bind   I -> C
    $container->add(Service::class)->addArgument(Dep::class);    -> inject S -> Dep
    $container->add(Service::class)
        ->addArgument(A::class)->addArgument(B::class);          -> inject S -> A, S -> B
    $container->addShared(I::class, C::class);                   -> bind
    $container->extend(Service::class)->addArgument(Dep::class); -> inject
    $container->add(Service::class);                             -> registration only

String / closure / `new $config` bindings are intentionally out of reach (runtime).

Mirrors migration_parser.py: a focused standalone walk, no changes to the shared
entity extractor. Returns raw `::class` operand texts; callers resolve them to FQCNs
with resolution.FileCtx (same as class references elsewhere).
"""

from __future__ import annotations

import os

import tree_sitter as ts

from .treesitter_parser import _get_parser, _node_text

# Container registration methods whose first ::class arg names the service/interface.
_REGISTER_METHODS = {"add", "addshared", "extend"}
_CONTAINER_TYPE_HINTS = ("container", "containerinterface")


def _class_const_operand(arg_node: ts.Node) -> str | None:
    """If an `argument` node is `X::class`, return the operand text of X, else None."""
    # argument -> class_constant_access_expression(name/qualified_name, 'class')
    for child in arg_node.named_children:
        if child.type == "class_constant_access_expression":
            named = child.named_children
            if (len(named) >= 2 and named[-1].type == "name"
                    and _node_text(named[-1]) == "class"):
                return _node_text(named[0])
    return None


def _arg_operands(arguments: ts.Node | None) -> list[str | None]:
    """`::class` operand text per positional argument (None for non-::class args)."""
    out: list[str | None] = []
    if arguments is None:
        return out
    for a in arguments.named_children:
        if a.type == "argument":
            out.append(_class_const_operand(a))
    return out


def _array_class_operands(arguments: ts.Node | None) -> list[str]:
    """All `X::class` operands anywhere in the argument list — covers the
    plural `addArguments([A::class, B::class])` array form."""
    out: list[str] = []
    if arguments is None:
        return out

    def _walk(n: ts.Node):
        if n.type == "class_constant_access_expression":
            named = n.named_children
            if (len(named) >= 2 and named[-1].type == "name"
                    and _node_text(named[-1]) == "class"):
                out.append(_node_text(named[0]))
            return
        for c in n.children:
            _walk(c)

    _walk(arguments)
    return out


def _unwind_chain(expr: ts.Node) -> tuple[ts.Node | None, list[tuple[str, ts.Node | None]]]:
    """Flatten a member-call chain into (base_object, [(method, arguments), ...])
    ordered base-first. `$c->add(X)->addArgument(Y)` -> (var $c,
    [('add', args_X), ('addArgument', args_Y)])."""
    calls: list[tuple[str, ts.Node | None]] = []
    node: ts.Node | None = expr
    while node is not None and node.type == "member_call_expression":
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "name":
            return None, []
        calls.append((_node_text(name_node), node.child_by_field_name("arguments")))
        node = node.child_by_field_name("object")
    calls.reverse()
    return node, calls


def _bindings_from_chain(expr: ts.Node) -> list[dict]:
    base_obj, calls = _unwind_chain(expr)
    if base_obj is None or base_obj.type != "variable_name" or not calls:
        return []
    base_method, base_args = calls[0]
    if base_method.lower() not in _REGISTER_METHODS:
        return []
    ops = _arg_operands(base_args)
    if not ops or ops[0] is None:
        return []  # e.g. add('stringKey', ...) — not a class registration
    source = ops[0]
    line = expr.start_point[0] + 1
    out: list[dict] = []
    # bind: add(Interface::class, Concrete::class)
    if len(ops) >= 2 and ops[1]:
        out.append({"kind": "bind", "source": source, "target": ops[1], "line": line})
    # inject: chained ->addArgument(Dep::class) / ->addArguments([A::class, ...])
    for method, args in calls[1:]:
        ml = method.lower()
        if ml == "addargument":
            for op in _arg_operands(args):
                if op:
                    out.append({"kind": "inject", "source": source,
                                "target": op, "line": line})
        elif ml == "addarguments":
            for op in _array_class_operands(args):
                out.append({"kind": "inject", "source": source,
                            "target": op, "line": line})
    return out


def _is_services_method(method_node: ts.Node) -> bool:
    """A `services(...)` method whose first parameter is a container type."""
    name = method_node.child_by_field_name("name")
    if name is None or _node_text(name) != "services":
        return False
    params = method_node.child_by_field_name("parameters")
    if params is None:
        return False
    for p in params.named_children:
        if p.type not in ("simple_parameter", "property_promotion_parameter"):
            continue
        tnode = p.child_by_field_name("type")
        if tnode is not None:
            simple = _node_text(tnode).lstrip("?\\").rsplit("\\", 1)[-1].lower()
            if simple in _CONTAINER_TYPE_HINTS:
                return True
    return False


def extract_di_bindings(path: str, content: str) -> list[dict]:
    """DI bindings wired in this file's container `services()` method(s).

    Each: {"kind": "bind"|"inject", "source": <::class text>, "target": <::class text>,
    "line": int}. Returns [] for non-PHP / no wiring.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".php", ".ctp"):
        return []
    parser = _get_parser("php")
    if parser is None:
        return []
    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
    except Exception:
        return []

    out: list[dict] = []

    def _walk_methods(node: ts.Node):
        if node.type == "method_declaration" and _is_services_method(node):
            body = node.child_by_field_name("body")
            if body is not None:
                _walk_statements(body, out)
            return  # don't descend into a services() method twice
        for c in node.children:
            _walk_methods(c)

    def _walk_statements(node: ts.Node, acc: list[dict]):
        if node.type == "member_call_expression":
            acc.extend(_bindings_from_chain(node))
            return  # the chain is handled as a whole
        for c in node.children:
            _walk_statements(c, acc)

    _walk_methods(tree.root_node)
    # de-dupe identical bindings (same chain seen once, but guard anyway)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for b in out:
        key = (b["kind"], b["source"], b["target"])
        if key not in seen:
            seen.add(key)
            deduped.append(b)
    return deduped
