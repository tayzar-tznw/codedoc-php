"""Parse CakePHP/Phinx migration files into a deterministic DB schema.

Pure tree-sitter AST parsing — no database connection, no LLM. Each migration
file's `change()` (or `up()`) method is read as an ordered list of table
operations (`parse_migration_file`), and `build_schema` replays those lists in
filename order (Phinx's timestamp order) to produce the final schema: tables,
columns, indexes and foreign keys. The graph pipeline can then materialize
DbTables nodes and TableReferences edges from real migration code.

Only literal arguments are trusted: a column name or type that is a variable
or expression skips the whole operation and records a warning — the parser
never guesses. Builder methods that don't shape the relational schema
(addTimestamps, setComment, insert, ...) are ignored silently.
"""

from __future__ import annotations

import os
from typing import Any

import tree_sitter as ts

from graph_generator.treesitter_parser import (
    _get_parser,
    _line,
    _literal_string,
    _node_text,
    _php_simple_name,
)

# Migration base classes: Phinx / CakePHP migrations ≤3 use AbstractMigration,
# the migrations plugin v4+ new backend uses BaseMigration. Matched by simple
# name so both `Migrations\...` and `Phinx\Migration\...` imports qualify.
_MIGRATION_BASES = {"AbstractMigration", "BaseMigration"}

_NAME_NODES = ("name", "qualified_name", "relative_name")

# Distinctly-Phinx builder methods. A chain on an unbound variable is normally
# unrelated code (e.g. `$rows->each(...)`) and skipped silently; if it invokes
# one of these we are provably losing schema ops, so a warning is emitted.
_BUILDER_METHODS = {
    "addColumn", "addIndex", "addForeignKey",
    "removeColumn", "renameColumn", "changeColumn",
}


def _value(node: ts.Node | None) -> tuple[Any, bool]:
    """Best-effort literal value of an expression node → (value, complete).

    complete=False when the node — or any nested part of an array — is not a
    compile-time literal. Array literals still return the entries that did
    parse (dynamic entries dropped), so callers choose: strict fields require
    complete=True, option arrays accept a partial dict.
    """
    if node is None:
        return None, False
    t = node.type

    if t in ("string", "encapsed_string"):
        s = _literal_string(node)
        return (s, True) if s is not None else (None, False)
    if t == "integer":
        txt = _node_text(node).replace("_", "")
        for base in (10, 0):  # base 0 picks up 0x/0b/0o forms
            try:
                return int(txt, base), True
            except ValueError:
                pass
        return None, False
    if t == "float":
        try:
            return float(_node_text(node).replace("_", "")), True
        except ValueError:
            return None, False
    if t == "boolean":
        return _node_text(node).lower() == "true", True
    if t == "null":
        return None, True
    if t == "unary_op_expression":
        operand = node.named_children[-1] if node.named_children else None
        val, ok = _value(operand)
        if ok and isinstance(val, (int, float)) and not isinstance(val, bool):
            op = _node_text(node.children[0]) if node.children else ""
            if op == "-":
                return -val, True
            if op == "+":
                return val, True
        return None, False
    if t == "parenthesized_expression":
        inner = node.named_children[0] if node.named_children else None
        return _value(inner)

    if t == "array_creation_expression":
        elements = [c for c in node.named_children
                    if c.type == "array_element_initializer"]
        complete = len(elements) == len(node.named_children)  # spreads etc.
        keyed = any(len(el.named_children) >= 2 for el in elements)
        if keyed:
            out_d: dict[Any, Any] = {}
            for el in elements:
                if len(el.named_children) < 2:
                    complete = False  # mixed keyed/unkeyed — PHP-legal, rare
                    continue
                key, kok = _value(el.named_children[0])
                val, vok = _value(el.named_children[-1])
                if kok and vok and isinstance(key, (str, int)):
                    out_d[key] = val
                else:
                    complete = False
            return out_d, complete
        out_l: list[Any] = []
        for el in elements:
            item = el.named_children[0] if el.named_children else None
            val, ok = _value(item)
            if ok:
                out_l.append(val)
            else:
                complete = False
        return out_l, complete

    return None, False


def _arg_nodes(call_node: ts.Node) -> list[ts.Node]:
    """Expression nodes of a call's positional arguments."""
    out: list[ts.Node] = []
    args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return out
    for a in args_node.named_children:
        if a.type != "argument":
            continue
        expr = a.named_children[-1] if a.named_children else None
        if expr is not None:
            out.append(expr)
    return out


def _flatten_chain(node: ts.Node) -> tuple[list[ts.Node], ts.Node | None]:
    """`$x->a()->b()->c()` → ([a_call, b_call, c_call], $x base node)."""
    calls: list[ts.Node] = []
    cur: ts.Node | None = node
    while cur is not None and cur.type == "member_call_expression":
        calls.append(cur)
        cur = cur.child_by_field_name("object")
    calls.reverse()
    return calls, cur


def _parse_ops(path: str, content: str) -> tuple[list[dict], list[str]]:
    """Parse one migration file → (ordered ops, warnings)."""
    parser = _get_parser("php")
    if parser is None:
        return [], [f"{path}: tree-sitter-php not installed; migration skipped"]
    try:
        tree = parser.parse(content.encode("utf-8", errors="replace"))
    except Exception:
        return [], [f"{path}: tree-sitter failed to parse file"]

    ops: list[dict] = []
    warnings: list[str] = []
    # $var → builder context {"table": name, "options": table() options}.
    # Contexts are shared by reference so `$t2 = $table` aliases like a PHP
    # object handle: a later rename() through either variable updates both.
    bindings: dict[str, dict] = {}

    def _warn(node: ts.Node, msg: str) -> None:
        warnings.append(f"{path}:{_line(node)}: {msg}")

    def _strict_str(node: ts.Node | None) -> str | None:
        val, ok = _value(node)
        return val if ok and isinstance(val, str) else None

    def _str_list(node: ts.Node | None) -> list[str] | None:
        """'col' or ['a','b'] → list of names; None if any part non-literal."""
        val, ok = _value(node)
        if not ok:
            return None
        if isinstance(val, str):
            return [val]
        if isinstance(val, list) and val and all(isinstance(v, str) for v in val):
            return val
        return None

    def _opt_dict(args: list[ts.Node], idx: int) -> dict:
        """Lenient options arg: literal entries kept, the rest dropped."""
        if len(args) <= idx:
            return {}
        val, _complete = _value(args[idx])
        return val if isinstance(val, dict) else {}

    def _arg(args: list[ts.Node], idx: int) -> ts.Node | None:
        return args[idx] if len(args) > idx else None

    def _dispatch(ctx: dict, mname: str, call: ts.Node) -> None:
        args = _arg_nodes(call)
        table = ctx["table"]
        # A chained call node spans from the chain's first link, so positions
        # come from the method-name node (`->addForeignKey` itself).
        pos = call.child_by_field_name("name") or call
        line = _line(pos)

        if mname == "addColumn":
            col = _strict_str(_arg(args, 0))
            typ = _strict_str(_arg(args, 1))
            if col is None or typ is None:
                _warn(pos, f"addColumn on '{table}' with non-literal "
                            "name/type skipped")
                return
            ops.append({"op": "add_column", "table": table, "line": line,
                        "column": col, "type": typ,
                        "options": _opt_dict(args, 2)})
        elif mname == "addIndex":
            cols = _str_list(_arg(args, 0))
            if cols is None:
                _warn(pos, f"addIndex on '{table}' with non-literal "
                            "columns skipped")
                return
            ops.append({"op": "add_index", "table": table, "line": line,
                        "columns": cols, "options": _opt_dict(args, 1)})
        elif mname == "addForeignKey":
            col = _str_list(_arg(args, 0))
            ref_table = _strict_str(_arg(args, 1))
            # 2-arg form defaults the referenced column to the implicit PK.
            ref_col = ["id"] if _arg(args, 2) is None else _str_list(_arg(args, 2))
            if col is None or ref_table is None or ref_col is None:
                _warn(pos, f"addForeignKey on '{table}' with non-literal "
                            "arguments skipped")
                return
            # Composite keys: record the first column pair only.
            ops.append({"op": "add_foreign_key", "table": table, "line": line,
                        "column": col[0], "referenced_table": ref_table,
                        "referenced_column": ref_col[0]})
        elif mname == "removeColumn":
            col = _strict_str(_arg(args, 0))
            if col is None:
                _warn(pos, f"removeColumn on '{table}' with non-literal "
                            "name skipped")
                return
            ops.append({"op": "remove_column", "table": table, "line": line,
                        "column": col})
        elif mname == "renameColumn":
            old = _strict_str(_arg(args, 0))
            new = _strict_str(_arg(args, 1))
            if old is None or new is None:
                _warn(pos, f"renameColumn on '{table}' with non-literal "
                            "names skipped")
                return
            ops.append({"op": "rename_column", "table": table, "line": line,
                        "column": old, "new_name": new})
        elif mname == "changeColumn":
            col = _strict_str(_arg(args, 0))
            typ = _strict_str(_arg(args, 1))
            if col is None or typ is None:
                _warn(pos, f"changeColumn on '{table}' with non-literal "
                            "name/type skipped")
                return
            ops.append({"op": "change_column", "table": table, "line": line,
                        "column": col, "type": typ,
                        "options": _opt_dict(args, 2)})
        elif mname == "create":
            ops.append({"op": "create_table", "table": table, "line": line,
                        "options": ctx["options"]})
        elif mname == "drop":
            ops.append({"op": "drop_table", "table": table, "line": line})
        elif mname == "rename":
            new = _strict_str(_arg(args, 0))
            if new is None:
                _warn(pos, f"rename of '{table}' with non-literal name skipped")
                return
            ops.append({"op": "rename_table", "table": table, "line": line,
                        "new_name": new})
            ctx["table"] = new  # rest of the chain / variable now targets it
        # update()/save() only execute already-recorded ops; anything else
        # (addTimestamps, setComment, insert, hasColumn, ...) is ignored.

    def _process_chain(expr: ts.Node) -> dict | None:
        """Handle one `...->m1()->m2()` chain; returns the builder context."""
        calls, base = _flatten_chain(expr)
        if not calls or base is None or base.type != "variable_name":
            return None
        var = _node_text(base)

        if var == "$this":
            first = calls[0]
            name_node = first.child_by_field_name("name")
            fname = (_node_text(name_node)
                     if name_node is not None and name_node.type == "name" else "")
            if fname == "table":
                targs = _arg_nodes(first)
                tname = _strict_str(_arg(targs, 0))
                if tname is None:
                    _warn(first, "non-literal table name in $this->table(); "
                                 "chain skipped")
                    return None
                ctx = {"table": tname, "options": _opt_dict(targs, 1)}
                rest = calls[1:]
            elif fname == "dropTable":  # legacy Phinx helper
                tname = _strict_str(_arg(_arg_nodes(first), 0))
                if tname is None:
                    _warn(first, "non-literal table name in dropTable() skipped")
                else:
                    ops.append({"op": "drop_table", "table": tname,
                                "line": _line(first)})
                return None
            elif fname in ("execute", "query"):
                _warn(first, f"raw SQL via $this->{fname}() is not parsed; "
                             "schema changes in it are invisible")
                return None
            else:
                return None  # hasTable(), fetchAll(), ... — not builders
        else:
            ctx = bindings.get(var)
            if ctx is None:
                names = set()
                for c in calls:
                    nn = c.child_by_field_name("name")
                    if nn is not None and nn.type == "name":
                        names.add(_node_text(nn))
                if names & _BUILDER_METHODS:
                    _warn(base, f"table ops on unbound variable {var} skipped")
                return None
            rest = calls

        for call in rest:
            name_node = call.child_by_field_name("name")
            if name_node is None or name_node.type != "name":
                _warn(call, "dynamic method name in builder chain skipped")
                continue
            _dispatch(ctx, _node_text(name_node), call)
        return ctx

    def _handle_assignment(expr: ts.Node) -> None:
        left = expr.child_by_field_name("left")
        right = expr.child_by_field_name("right")
        if left is None or right is None or left.type != "variable_name":
            return
        var = _node_text(left)
        if right.type == "member_call_expression":
            ctx = _process_chain(right)
            if ctx is not None:
                bindings[var] = ctx
            else:
                bindings.pop(var, None)
        elif right.type == "variable_name":
            src = bindings.get(_node_text(right))
            if src is not None:
                bindings[var] = src
            else:
                bindings.pop(var, None)
        else:
            bindings.pop(var, None)  # variable reused for something else

    def _walk_statements(node: ts.Node) -> None:
        # DFS over named children is source order, so ops inside if/foreach
        # bodies land in sequence; expression statements are terminal to avoid
        # double-processing a chain that sits inside an assignment.
        if node.type == "expression_statement":
            expr = node.named_children[0] if node.named_children else None
            if expr is None:
                return
            if expr.type == "assignment_expression":
                _handle_assignment(expr)
            elif expr.type == "member_call_expression":
                _process_chain(expr)
            return
        for child in node.named_children:
            _walk_statements(child)

    def _find_migration_bodies(node: ts.Node, bodies: list[ts.Node]) -> None:
        if node.type == "class_declaration":
            bases: set[str] = set()
            for c in node.children:
                if c.type == "base_clause":
                    for b in c.children:
                        if b.type in _NAME_NODES:
                            bases.add(_php_simple_name(b))
            if bases & _MIGRATION_BASES:
                decls = node.child_by_field_name("body")
                methods: dict[str, ts.Node] = {}
                if decls is not None:
                    for m in decls.children:
                        if m.type == "method_declaration":
                            nn = m.child_by_field_name("name")
                            if nn is not None:
                                methods[_node_text(nn)] = m
                # Phinx runs change() and ignores up() when both exist.
                target = methods.get("change") or methods.get("up")
                if target is not None:
                    body = target.child_by_field_name("body")
                    if body is not None:
                        bodies.append(body)
        for child in node.named_children:
            _find_migration_bodies(child, bodies)

    bodies: list[ts.Node] = []
    _find_migration_bodies(tree.root_node, bodies)
    for body in bodies:
        bindings.clear()
        _walk_statements(body)
    return ops, warnings


def parse_migration_file(path: str, content: str) -> list[dict]:
    """Ordered list of operations in one migration file's up/change method.

    Each op: {"op": "create_table"|"add_column"|"add_index"|"add_foreign_key"
              |"remove_column"|"change_column"|"drop_table"|"rename_table"
              |"rename_column",
              "table": <table name resolved from the $table var / $this->table(...)>,
              "line": <1-based source line>,
              ...op-specific fields...}

    Op-specific fields:
      create_table     options (the $this->table() options dict)
      add_column       column, type, options
      add_index        columns (list), options
      add_foreign_key  column, referenced_table, referenced_column
      remove_column    column
      rename_column    column, new_name
      change_column    column, type, options
      rename_table     new_name
      drop_table       —

    Ops appear in source-call order; `create_table` lands at the `->create()`
    call, i.e. after the chain's addColumn/addIndex ops. Return [] if
    unparseable (no migration class, no change/up method, or parser missing).
    """
    ops, _warnings = _parse_ops(path, content)
    return ops


def _ensure_table(tables: dict[str, dict], name: str, path: str) -> dict:
    if name not in tables:
        tables[name] = {"name": name, "columns": [], "indexes": [],
                        "foreign_keys": [], "source_file": path}
    return tables[name]


def _apply_op(op: dict, tables: dict[str, dict], created: set[str],
              warnings: list[str], path: str) -> None:
    """Replay one op onto the accumulating schema."""
    kind = op["op"]
    name = op["table"]

    if kind == "create_table":
        entry = _ensure_table(tables, name, path)
        if name in created:
            warnings.append(
                f"{path}: duplicate create of table '{name}' (first created "
                f"in {entry['source_file']}); columns merged, first wins")
            return
        created.add(name)
        entry["source_file"] = path
        # Phinx adds an implicit integer PK unless the table() options say
        # otherwise ('id' => false disables, 'id' => 'name' renames it).
        id_opt = op.get("options", {}).get("id", True)
        if id_opt is not False:
            id_name = id_opt if isinstance(id_opt, str) else "id"
            if all(c["name"] != id_name for c in entry["columns"]):
                entry["columns"].insert(0, {
                    "name": id_name, "type": "integer",
                    "options": {"primary_key": True}})
        return

    if kind == "drop_table":
        if name in tables:
            del tables[name]
            created.discard(name)
        else:
            warnings.append(f"{path}: drop of unknown table '{name}' ignored")
        return

    if kind == "rename_table":
        new = op["new_name"]
        if name not in tables:
            warnings.append(f"{path}: rename of unknown table '{name}' ignored")
            return
        if new in tables:
            warnings.append(f"{path}: rename '{name}' → '{new}' collides with "
                            "an existing table; ignored")
            return
        entry = tables.pop(name)
        entry["name"] = new
        tables[new] = entry
        if name in created:
            created.discard(name)
            created.add(new)
        for other in tables.values():  # FK targets follow the rename, as in a DB
            for fk in other["foreign_keys"]:
                if fk["referenced_table"] == name:
                    fk["referenced_table"] = new
        return

    entry = _ensure_table(tables, name, path)

    if kind == "add_column":
        if all(c["name"] != op["column"] for c in entry["columns"]):
            entry["columns"].append({"name": op["column"], "type": op["type"],
                                     "options": op.get("options", {})})
    elif kind == "add_index":
        opts = op.get("options", {})
        idx_name = opts.get("name")
        idx = {"columns": list(op["columns"]),
               "unique": opts.get("unique") is True,
               "name": idx_name if isinstance(idx_name, str) else ""}
        if idx not in entry["indexes"]:
            entry["indexes"].append(idx)
    elif kind == "add_foreign_key":
        fk = {"column": op["column"],
              "referenced_table": op["referenced_table"],
              "referenced_column": op["referenced_column"]}
        if fk not in entry["foreign_keys"]:
            entry["foreign_keys"].append(fk)
    elif kind == "remove_column":
        col = op["column"]
        before = len(entry["columns"])
        entry["columns"] = [c for c in entry["columns"] if c["name"] != col]
        if len(entry["columns"]) == before:
            warnings.append(f"{path}: removeColumn '{col}' not found on "
                            f"table '{name}'")
        # Indexes/FKs over a dropped column can't survive in a real DB either.
        entry["indexes"] = [i for i in entry["indexes"]
                            if col not in i["columns"]]
        entry["foreign_keys"] = [f for f in entry["foreign_keys"]
                                 if f["column"] != col]
    elif kind == "rename_column":
        old, new = op["column"], op["new_name"]
        found = False
        for c in entry["columns"]:
            if c["name"] == old:
                c["name"] = new
                found = True
                break
        if not found:
            warnings.append(f"{path}: renameColumn '{old}' not found on "
                            f"table '{name}'")
            return
        for idx in entry["indexes"]:
            idx["columns"] = [new if c == old else c for c in idx["columns"]]
        for fk in entry["foreign_keys"]:
            if fk["column"] == old:
                fk["column"] = new
    elif kind == "change_column":
        for c in entry["columns"]:
            if c["name"] == op["column"]:
                c["type"] = op["type"]
                c["options"] = op.get("options", {})
                return
        # Unknown column: the table was likely created outside this migration
        # set — record the declared end state rather than dropping it.
        warnings.append(f"{path}: changeColumn '{op['column']}' not found on "
                        f"table '{name}'; added as new column")
        entry["columns"].append({"name": op["column"], "type": op["type"],
                                 "options": op.get("options", {})})


def build_schema(migration_files: list[tuple[str, str]]) -> dict:
    """Replay migrations into the final schema.

    migration_files: list of (path, content), replayed in filename order
    (Phinx runs migrations in timestamp order = lexical filename order — the
    caller passes them pre-sorted, but they are re-sorted by basename here
    defensively). Paths are recorded verbatim as source_file, so pass paths
    relative to the scanned project root.

    Returns:
      {"tables": {table_name: {
            "name": str,
            "columns": [{"name","type","options":{...}}],  # declaration order,
                                                           # implicit id first
            "indexes": [{"columns":[...], "unique":bool, "name":str}],
            "foreign_keys": [{"column","referenced_table","referenced_column"}],
            "source_file": <path of the migration that created it>,
       }},
       "warnings": [str, ...]}   # dynamic args skipped, duplicate create, ...
    """
    ordered = sorted(migration_files, key=lambda pf: os.path.basename(pf[0]))
    tables: dict[str, dict] = {}
    created: set[str] = set()
    warnings: list[str] = []

    for path, content in ordered:
        ops, parse_warnings = _parse_ops(path, content)
        warnings.extend(parse_warnings)
        for op in ops:
            _apply_op(op, tables, created, warnings, path)

    for name in tables:
        if name not in created:
            warnings.append(f"table '{name}' has operations but no create() "
                            "in these migrations; created elsewhere?")
    return {"tables": tables, "warnings": warnings}
