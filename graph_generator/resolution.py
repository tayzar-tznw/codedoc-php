"""Phase 1.6 — semantic resolution of PHP call sites, inheritance, and imports.

Combines three mechanisms, strongest first:

1. **LSP (Intelephense)** `textDocument/definition` at each parser-captured
   position — resolves aliased imports, receiver types, fluent chains,
   `parent::`, interface dispatch.
2. **CakePHP conventions** (php_conventions.py) — string loaders
   (`fetchTable('Billing.Audits')`), behavior mixins, magic finders,
   `__call`/`__get`, entity virtual fields. Every candidate is confirmed
   against a real class/member before it is recorded.
3. **Parser name resolution** — `use` maps + namespace rules, as a fallback
   when the LSP is unavailable.

Every resolution carries provenance (`via`) and a status:
  resolved   — target is a known internal (in-graph) member
  external   — target confirmed but lives outside the graph (vendor)
  ambiguous  — several distinct static targets (candidates recorded)
  dynamic    — receiver/callee computed at runtime (no static target)
  unresolved — nothing confirmed (never guessed)

Edge derivation later turns only `resolved` into MethodCalls edges; ambiguous/
dynamic/unresolved go to PossiblyCalls (capped) — never wrong CALLS edges.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from . import config
from . import php_conventions as conv
from .treesitter_parser import ENTITIES_VERSION, parse_entities

RESOLUTIONS_VERSION = 1

_WORD = re.compile(r"^[A-Za-z_]\w*$")

# Sentinel type for receivers whose class is computed at runtime
DYNAMIC_TYPE = "!dynamic"


def _norm(name: str) -> str:
    """PHP class/method names are case-insensitive — normalize for lookups."""
    return name.lower()


# ═══════════════════════════════════════════════════════════════════
# Entity index (span lookups + lazy vendor parsing)
# ═══════════════════════════════════════════════════════════════════


class EntityIndex:
    """Class/member lookup over extracted entities, with lazy parsing of
    files outside the scan set (vendor) so LSP definition targets can be
    mapped to exact FQCN::member even when those files have no graph nodes."""

    def __init__(self, target_dir: str, extracted_entities: dict[str, dict],
                 origins: dict[str, str] | None = None):
        self.target_dir = os.path.abspath(target_dir)
        self.internal: dict[str, dict] = {}          # rel_path → entity
        self.origins = origins or {}                  # rel_path → 'app'|'vendor'
        self._fqcn: dict[str, tuple[str, dict]] = {}  # norm fqcn → (rel, class)
        self._simple: dict[str, list[str]] = {}       # norm simple → [fqcn]
        self._lazy_by_path: dict[str, dict | None] = {}
        self._lazy_fqcn: dict[str, tuple[str, dict]] = {}  # norm fqcn → (abs, class)
        self._children: dict[str, list[str]] = {}     # norm parent fqcn → [child fqcn]

        self._functions: dict[str, tuple[str, dict]] = {}  # norm fq fn → (rel, method)

        for abs_fp, ent in extracted_entities.items():
            rel = self.rel(abs_fp)
            self.internal[rel] = ent
            ns = ent.get("namespace", "")
            for cls in ent.get("classes", []):
                fqcn = cls.get("fqcn") or ""
                if cls.get("name") == "(global)":
                    for m in cls.get("methods", []):
                        if m.get("member_kind") == "function":
                            fq = f"{ns}\\{m['name']}" if ns else m["name"]
                            self._functions.setdefault(_norm(fq), (rel, m))
                    continue
                if not fqcn:
                    continue
                key = _norm(fqcn)
                if key not in self._fqcn:
                    self._fqcn[key] = (rel, cls)
                self._simple.setdefault(_norm(cls["name"]), []).append(fqcn)

        # Reverse-inheritance map (internal, parser-level name resolution) —
        # used for the `static::` late-static-binding ambiguity check.
        for rel, ent in self.internal.items():
            ctx = FileCtx(ent)
            for cls in ent.get("classes", []):
                child = cls.get("fqcn") or ""
                if not child:
                    continue
                for h in cls.get("heritage", []):
                    if h["relation"] != "extends":
                        continue
                    for cand in ctx.candidates(h["qualified"]):
                        if _norm(cand) in self._fqcn:
                            self._children.setdefault(_norm(cand), []).append(child)
                            break

    def rel(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self.target_dir)

    def abs(self, rel: str) -> str:
        return os.path.join(self.target_dir, rel)

    def is_internal_path(self, abs_path: str) -> bool:
        rel = self.rel(abs_path)
        return rel in self.internal

    # ── class lookups ────────────────────────────────────────────

    def find_internal(self, fqcn: str) -> tuple[str, dict] | None:
        return self._fqcn.get(_norm(fqcn))

    def exists(self, fqcn: str) -> bool:
        return _norm(fqcn) in self._fqcn or _norm(fqcn) in self._lazy_fqcn

    def simple_matches(self, simple: str) -> list[str]:
        return self._simple.get(_norm(simple), [])

    def find_function(self, fq_name: str) -> tuple[str, dict] | None:
        """Internal global/namespaced function by fully-qualified name."""
        return self._functions.get(_norm(fq_name))

    def methods_named(self, name: str, limit: int = 50) -> list[dict]:
        """Internal methods with this simple name (targets for dynamic-call
        candidate sets). Bounded — callers cap fan-out anyway."""
        out: list[dict] = []
        for rel, ent in self.internal.items():
            for cls in ent.get("classes", []):
                if cls.get("name") == "(global)":
                    continue
                for m in cls.get("methods", []):
                    if m.get("member_kind") != "method":
                        continue
                    if _norm(m["name"]) == _norm(name):
                        out.append(_target(cls.get("fqcn") or cls.get("name", ""),
                                           m["name"], rel,
                                           m.get("start_line", 0), "method"))
                        if len(out) >= limit:
                            return out
        return out

    def subclasses_of(self, fqcn: str) -> list[str]:
        return self._children.get(_norm(fqcn), [])

    # ── lazy (vendor/out-of-scan) parsing ────────────────────────

    def lazy_entity(self, abs_path: str) -> dict | None:
        abs_path = os.path.abspath(abs_path)
        if abs_path in self._lazy_by_path:
            return self._lazy_by_path[abs_path]
        ent = None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                ent = parse_entities(abs_path, f.read())
        except OSError:
            ent = None
        self._lazy_by_path[abs_path] = ent
        if ent:
            for cls in ent.get("classes", []):
                fqcn = cls.get("fqcn") or ""
                if fqcn and _norm(fqcn) not in self._lazy_fqcn:
                    self._lazy_fqcn[_norm(fqcn)] = (abs_path, cls)
        return ent

    def find_class(self, fqcn: str,
                   locate_path: Callable[[str], str | None] | None = None
                   ) -> tuple[dict, dict, str, bool] | None:
        """→ (entity, class_dict, abs_path, is_internal) for a FQCN.

        Internal first; then previously lazy-parsed vendor classes; then, if
        `locate_path` (typically workspace/symbol) can name a file, parse it.
        """
        hit = self._fqcn.get(_norm(fqcn))
        if hit:
            rel, cls = hit
            return self.internal[rel], cls, self.abs(rel), True
        lazy = self._lazy_fqcn.get(_norm(fqcn))
        if lazy:
            abs_path, cls = lazy
            ent = self._lazy_by_path.get(abs_path)
            if ent:
                return ent, cls, abs_path, False
        if locate_path is not None:
            path = locate_path(fqcn)
            if path:
                ent = self.lazy_entity(path)
                if ent:
                    for cls in ent.get("classes", []):
                        if _norm(cls.get("fqcn") or "") == _norm(fqcn):
                            return ent, cls, path, False
        return None

    # ── span lookups ─────────────────────────────────────────────

    def locate(self, abs_path: str, line: int) -> dict | None:
        """Map a definition location to {fqcn, member, member_kind, path,
        internal} using declaration spans. `member` is "" when the location
        is the class declaration itself."""
        rel = self.rel(abs_path)
        internal = rel in self.internal
        ent = self.internal.get(rel) or self.lazy_entity(abs_path)
        if not ent:
            return None
        best_cls = None
        for cls in ent.get("classes", []):
            s, e = cls.get("start_line", 0), cls.get("end_line", 0)
            if s <= line <= e or cls.get("name") == "(global)":
                if cls.get("name") == "(global)" and not (s <= line <= e):
                    continue
                if best_cls is None or (s >= best_cls.get("start_line", 0)
                                        and cls.get("name") != "(global)"):
                    best_cls = cls
        if best_cls is None:
            # Global functions live in the (global) pseudo-class
            for cls in ent.get("classes", []):
                if cls.get("name") == "(global)":
                    best_cls = cls
                    break
        if best_cls is None:
            return None
        member, kind = "", ""
        for m in best_cls.get("methods", []):
            if m.get("start_line", 0) <= line <= m.get("end_line", 0):
                member, kind = m["name"], m.get("member_kind", "method")
                break
        fqcn = best_cls.get("fqcn") or ""
        if best_cls.get("name") == "(global)":
            fqcn = ""
            if not member:
                return None
            # Global functions answer in namespaced form (`App\Util\helper`)
            ns = ent.get("namespace", "")
            if ns:
                member = f"{ns}\\{member}"
        return {"fqcn": fqcn, "member": member, "member_kind": kind,
                "path": rel if internal else abs_path, "internal": internal,
                "class": best_cls, "entity": ent}


# ═══════════════════════════════════════════════════════════════════
# Per-file name resolution (use map + namespace rules)
# ═══════════════════════════════════════════════════════════════════


class FileCtx:
    """PHP name-resolution context for one file."""

    def __init__(self, entity: dict):
        self.namespace = entity.get("namespace", "")
        self.aliases: dict[str, str] = {}
        self.fn_aliases: dict[str, str] = {}
        for u in entity.get("uses", []):
            if u.get("kind") == "function":
                self.fn_aliases[_norm(u["alias"])] = u["fqcn"]
            elif u.get("kind") == "class":
                self.aliases[_norm(u["alias"])] = u["fqcn"]

    def candidates(self, text: str) -> list[str]:
        """Possible FQCNs for a (possibly qualified) class reference."""
        text = text.strip()
        if not text:
            return []
        if text.startswith("\\"):
            return [text.lstrip("\\")]
        if text.lower().startswith("namespace\\"):
            rest = text[len("namespace\\"):]
            return [f"{self.namespace}\\{rest}"] if self.namespace else [rest]
        head, sep, rest = text.partition("\\")
        alias = self.aliases.get(_norm(head))
        if alias:
            return [f"{alias}\\{rest}"] if sep else [alias]
        out = []
        if self.namespace:
            out.append(f"{self.namespace}\\{text}")
        out.append(text)
        return out

    def function_candidates(self, text: str) -> list[str]:
        """Function-name resolution: alias → current namespace → global."""
        text = text.strip()
        if text.startswith("\\"):
            return [text.lstrip("\\")]
        if "\\" not in text:
            alias = self.fn_aliases.get(_norm(text))
            if alias:
                return [alias]
            out = []
            if self.namespace:
                out.append(f"{self.namespace}\\{text}")
            out.append(text)
            return out
        return self.candidates(text)


# ═══════════════════════════════════════════════════════════════════
# Resolver
# ═══════════════════════════════════════════════════════════════════


def _target(fqcn: str, member: str, path: str, line: int = 0,
            member_kind: str = "") -> dict:
    return {"fqcn": fqcn, "member": member, "path": path, "line": line,
            "member_kind": member_kind}


class Resolver:
    """Resolution machinery for one workspace run."""

    def __init__(self, index: EntityIndex, lsp=None, printer=print):
        self.index = index
        self.lsp = lsp
        self.print = printer
        self.stats: dict[str, int] = {}
        self._sym_path_cache: dict[str, str | None] = {}
        self._behaviors_cache: dict[str, set[str]] = {}

    def _count(self, key: str):
        self.stats[key] = self.stats.get(key, 0) + 1

    # ── vendor class location via workspace/symbol ───────────────

    def locate_class_path(self, fqcn: str) -> str | None:
        """Find the defining file of a FQCN through workspace/symbol."""
        key = _norm(fqcn)
        if key in self._sym_path_cache:
            return self._sym_path_cache[key]
        path = None
        if self.lsp is not None:
            simple = fqcn.rsplit("\\", 1)[-1]
            container = fqcn.rsplit("\\", 1)[0] if "\\" in fqcn else ""
            try:
                for item in self.lsp.workspace_symbol(simple):
                    if _norm(item["name"]) != _norm(simple):
                        continue
                    if container and _norm(item.get("container", "")) != _norm(container):
                        continue
                    if not container and item.get("container"):
                        continue
                    path = item["path"]
                    break
            except Exception:
                path = None
        self._sym_path_cache[key] = path
        return path

    def find_class(self, fqcn: str):
        return self.index.find_class(fqcn, locate_path=self.locate_class_path)

    # ── member chain walk (class → parents/traits → behaviors → __call) ──

    def behaviors_of(self, fqcn: str) -> set[str]:
        """CakePHP behaviors registered on a class via addBehavior('P.X'),
        including registrations inherited from internal parent classes."""
        key = _norm(fqcn)
        if key in self._behaviors_cache:
            return self._behaviors_cache[key]
        self._behaviors_cache[key] = set()  # cycle guard
        out: set[str] = set()
        found = self.find_class(fqcn)
        if found:
            ent, cls, _abs, _internal = found
            ctx = FileCtx(ent)
            root_ns = (cls.get("fqcn") or "").split("\\")[0]
            for m in cls.get("methods", []):
                for site in m.get("call_sites", []):
                    if _norm(site.get("name") or "") != "addbehavior":
                        continue
                    args = site.get("str_args") or []
                    s0 = args[0] if args else None
                    if not isinstance(s0, str):
                        continue
                    for _rule, fq in conv.loader_class_candidates("addBehavior", s0, root_ns):
                        if self.find_class(fq):
                            out.add(fq)
                            break
            for h in cls.get("heritage", []):
                if h["relation"] != "extends":
                    continue
                for cand in ctx.candidates(h["qualified"]):
                    f2 = self.find_class(cand)
                    if f2:
                        if f2[3]:  # internal parents only
                            out |= self.behaviors_of(cand)
                        break
        self._behaviors_cache[key] = out
        return out

    def walk_member(self, fqcn: str, member: str, *, static: bool = False,
                    depth: int = 10) -> tuple[dict, str] | None:
        """Find `member` on `fqcn` or its parent/trait chain.

        Returns (target, rule) where rule ∈ {chain, behavior, magic_call}.
        Confirmed declarations only — never a guess.
        """
        visited: set[str] = set()
        queue = [fqcn]
        chain: list[tuple[dict, str, bool]] = []  # (class_dict, abs_path, internal)

        while queue and len(visited) < depth * 4:
            cur = queue.pop(0)
            if _norm(cur) in visited:
                continue
            visited.add(_norm(cur))
            found = self.find_class(cur)
            if not found:
                continue
            ent, cls, abs_path, internal = found
            chain.append((cls, abs_path, internal))
            for m in cls.get("methods", []):
                if _norm(m["name"]) == _norm(member) \
                        and m.get("member_kind") in ("method", "function"):
                    path = self.index.rel(abs_path) if internal else abs_path
                    return (_target(cls.get("fqcn", cur), m["name"], path,
                                    m.get("start_line", 0), "method"), "chain")
            ctx = FileCtx(ent)
            for h in cls.get("heritage", []):
                if h["relation"] not in ("extends", "uses"):
                    continue
                for cand in ctx.candidates(h["qualified"]):
                    if self.find_class(cand):
                        queue.append(cand)
                        break

        # Behavior mixins (addBehavior) registered on internal chain classes
        if depth > 0:
            for cls, _abs, internal in chain:
                if not internal:
                    continue
                for beh in self.behaviors_of(cls.get("fqcn") or ""):
                    hit = self.walk_member(beh, member, depth=depth - 1)
                    if hit:
                        return (hit[0], "behavior")

        # Magic __call / __callStatic anywhere along the chain
        magic = "__callStatic" if static else "__call"
        for cls, abs_path, internal in chain:
            for m in cls.get("methods", []):
                if _norm(m["name"]) == _norm(magic):
                    path = self.index.rel(abs_path) if internal else abs_path
                    return (_target(cls.get("fqcn", ""), m["name"], path,
                                    m.get("start_line", 0), "method"), "magic_call")
        return None

    def property_declared(self, fqcn: str, prop: str, depth: int = 10) -> bool:
        visited: set[str] = set()
        queue = [fqcn]
        while queue and len(visited) < depth * 4:
            cur = queue.pop(0)
            if _norm(cur) in visited:
                continue
            visited.add(_norm(cur))
            found = self.find_class(cur)
            if not found:
                continue
            ent, cls, _, _ = found
            for m in cls.get("methods", []):
                if m.get("member_kind") == "property" and _norm(m["name"]) == _norm(prop):
                    return True
            ctx = FileCtx(ent)
            for h in cls.get("heritage", []):
                if h["relation"] in ("extends", "uses"):
                    for cand in ctx.candidates(h["qualified"]):
                        if self.find_class(cand):
                            queue.append(cand)
                            break
        return False

    def member_return_type_fqcn(self, target: dict) -> str | None:
        """Resolve a target method's declared return type to a FQCN (for
        fluent-chain fallback when the LSP is unavailable)."""
        found = self.find_class(target.get("fqcn", ""))
        if not found:
            return None
        ent, cls, _abs, _internal = found
        for m in cls.get("methods", []):
            if _norm(m["name"]) == _norm(target.get("member", "")):
                rt = (m.get("return_type") or "").lstrip("?").strip()
                if not rt or rt.lower() in ("void", "static", "self", "$this",
                                            "bool", "int", "float", "string",
                                            "array", "mixed", "null", "callable",
                                            "iterable", "object", "never", "true",
                                            "false"):
                    if rt.lower() in ("static", "self", "$this"):
                        return cls.get("fqcn") or None
                    return None
                ctx = FileCtx(ent)
                for cand in ctx.candidates(rt):
                    if self.find_class(cand):
                        return cand
                return None
        return None

    def forward_types_of(self, fqcn: str) -> list[str]:
        """Classes a magic proxy plausibly forwards to: typed properties,
        typed constructor params (incl. promoted), and classes instantiated
        inside the magic methods themselves."""
        found = self.find_class(fqcn)
        if not found:
            return []
        ent, cls, _abs, _internal = found
        ctx = FileCtx(ent)
        types: list[str] = []

        def _add(text: str):
            for cand in ctx.candidates(text.lstrip("?")):
                if self.find_class(cand):
                    if _norm(cand) not in {_norm(t) for t in types}:
                        types.append(cand)
                    return

        for m in cls.get("methods", []):
            if m.get("member_kind") == "property" and m.get("return_type"):
                _add(m["return_type"])
            if m.get("name") == "__construct":
                for t in (m.get("param_types") or {}).values():
                    _add(t)
            if m.get("name") in ("__call", "__callStatic", "__get", "__set"):
                for site in m.get("call_sites", []):
                    if site.get("kind") == "new" and site.get("name"):
                        _add(site.get("qualified") or site["name"])
        return types

    def magic_forward(self, recv_type: str, member: str) -> dict | None:
        """When a call lands on __call/__callStatic/__get, try to identify
        the single concrete class the proxy forwards to. Only fires when
        exactly one forward-type declares `member` as a real method —
        otherwise the magic method itself remains the answer."""
        hits: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for t in self.forward_types_of(recv_type):
            h = self.walk_member(t, member, depth=6)
            if h and h[1] == "chain":
                key = (_norm(h[0]["fqcn"]), _norm(h[0]["member"]))
                if key not in seen:
                    seen.add(key)
                    hits.append(h[0])
        return hits[0] if len(hits) == 1 else None

    def descendants_overriding(self, fqcn: str, member: str) -> list[str]:
        """Internal subclasses (transitively) that redeclare `member` —
        the late-static-binding candidate set for `static::member()`."""
        out: list[str] = []
        seen: set[str] = set()
        queue = list(self.index.subclasses_of(fqcn))
        while queue:
            child = queue.pop(0)
            if _norm(child) in seen:
                continue
            seen.add(_norm(child))
            hit = self.index.find_internal(child)
            if hit:
                _rel, cls = hit
                if any(_norm(m["name"]) == _norm(member)
                       and m.get("member_kind") in ("method", "function")
                       for m in cls.get("methods", [])):
                    out.append(cls.get("fqcn") or child)
            queue.extend(self.index.subclasses_of(child))
        return out


# ═══════════════════════════════════════════════════════════════════
# Per-class context and receiver type tracking
# ═══════════════════════════════════════════════════════════════════


class ClassCtx:
    """Per-class resolution context: property types, loader registrations,
    controller default table — the receiver-type seeds conventions need."""

    def __init__(self, resolver: Resolver, file_ctx: FileCtx, cls: dict):
        self.cls = cls
        self.fqcn = cls.get("fqcn") or ""
        self.root_ns = self.fqcn.split("\\")[0] if "\\" in self.fqcn else "App"
        self.prop_types: dict[str, str] = {}  # norm prop name → fqcn

        # Declared property types (properties are pseudo-members with the
        # declared type in return_type)
        for m in cls.get("methods", []):
            if m.get("member_kind") == "property" and m.get("return_type"):
                for cand in file_ctx.candidates(m["return_type"].lstrip("?")):
                    if resolver.find_class(cand):
                        self.prop_types[_norm(m["name"])] = cand
                        break

        # Loader registrations anywhere in the class (typically initialize()):
        # fetchTable/loadComponent/loadHelper give `$this-><Alias>` a type.
        for m in cls.get("methods", []):
            for site in m.get("call_sites", []):
                name = site.get("name") or ""
                args = site.get("str_args") or []
                s0 = args[0] if args else None
                if not name or not isinstance(s0, str):
                    continue
                if name.lower() not in ("fetchtable", "loadmodel",
                                        "loadcomponent", "loadhelper"):
                    continue
                for _rule, fq in conv.loader_class_candidates(name, s0, self.root_ns):
                    if resolver.find_class(fq):
                        _plugin, alias = conv.split_plugin(s0)
                        self.prop_types.setdefault(_norm(alias), fq)
                        break

        # CakePHP controller default-table magic property ($this->Articles
        # in ArticlesController)
        dt = conv.default_table_fqcn(self.fqcn)
        if dt and resolver.find_class(dt):
            alias = dt.rsplit("\\", 1)[-1][:-len("Table")]
            self.prop_types.setdefault(_norm(alias), dt)


class TypeTracker:
    """Lexical receiver-type tracking inside one method body.

    Intelephense types `fetchTable()` results as generic `Cake\\ORM\\Table`,
    so magic finders / behavior mixins / entity virtuals are unreachable via
    LSP alone — this tracker follows literal-string loaders and simple
    assignments to recover the concrete class.
    """

    def __init__(self, resolver: Resolver, file_ctx: FileCtx, class_ctx: ClassCtx,
                 method: dict, site_records: dict):
        self.res = resolver
        self.ctx = file_ctx
        self.cls = class_ctx
        self.method = method
        self.site_records = site_records  # (line, col) → call record

    def resolve_class_text(self, text: str) -> str | None:
        for cand in self.ctx.candidates(text):
            if self.res.find_class(cand):
                return cand
        return None

    def type_of(self, recv: str, at_line: int, depth: int = 0) -> str | None:
        if depth > 6 or not recv:
            return None
        recv = recv.strip()
        if recv in ("$this", "self", "static", "self()", "static()"):
            return self.cls.fqcn or None
        if recv == "parent":
            for h in self.cls.cls.get("heritage", []):
                if h["relation"] == "extends":
                    return self.resolve_class_text(h["qualified"])
            return None
        if recv.startswith("$this->"):
            prop = recv[len("$this->"):]
            if _WORD.match(prop):
                return self.cls.prop_types.get(_norm(prop))
            return None
        if recv.startswith("$"):
            if not _WORD.match(recv[1:]):
                return None
            env: dict[str, str | None] = {}
            for a in self.method.get("assignments", []):
                line = a.get("line")
                if line is not None and line > at_line:
                    break
                var = a.get("var")
                if var:
                    env[var] = self._rhs_type(a, env, at_line, depth)
            if env.get(recv):
                return env[recv]
            pt = (self.method.get("param_types") or {}).get(recv)
            if pt:
                return self.resolve_class_text(pt.lstrip("?"))
            return None
        # `Foo::class` receiver texts (array callables)
        if recv.endswith("::class"):
            return self.resolve_class_text(recv[: -len("::class")])
        # Bare (possibly qualified) class name — static receiver
        return self.resolve_class_text(recv)

    def _rhs_type(self, a: dict, env: dict, at_line: int, depth: int) -> str | None:
        kind = a.get("rhs_kind")
        if kind == "dynamic":
            return DYNAMIC_TYPE
        if kind == "new":
            return self.resolve_class_text(a.get("qualified") or a.get("name", ""))
        if kind == "call":
            rec = self.site_records.get((a.get("line"), a.get("col")))
            if not rec:
                return None
            st = rec.get("string_target")
            if st:
                return st.get("fqcn")
            tgt = rec.get("target")
            if tgt and tgt.get("member"):
                if _norm(tgt["member"]) in conv.TABLE_ENTITY_RETURNING:
                    rtype = self.type_of(rec.get("receiver", ""),
                                         a.get("line") or at_line, depth + 1)
                    if rtype and "\\Model\\Table\\" in rtype:
                        ent_cls = conv.entity_class_for_table(rtype)
                        if ent_cls and self.res.find_class(ent_cls):
                            return ent_cls
                return self.res.member_return_type_fqcn(tgt)
            return None
        if kind == "prop":
            if a.get("receiver") == "$this":
                return self.cls.prop_types.get(_norm(a.get("name", "")))
            return None
        if kind == "var":
            return env.get(a.get("name", ""))
        return None


# ═══════════════════════════════════════════════════════════════════
# Per-site resolution flow
# ═══════════════════════════════════════════════════════════════════


def _status_for(located: dict) -> str:
    return "resolved" if located.get("internal") else "external"


def _target_from_located(loc: dict) -> dict:
    return _target(loc.get("fqcn", ""), loc.get("member", ""),
                   loc.get("path", ""), 0, loc.get("member_kind", ""))


def _lsp_definitions(res: Resolver, lsp, abs_path: str, line: int, col: int) -> list[dict]:
    """Definition locations mapped through the span index; [] on any failure."""
    if lsp is None:
        return []
    try:
        locs = lsp.definition(abs_path, line, col)
    except Exception:
        return []
    out = []
    for loc in locs:
        located = res.index.locate(loc["path"], loc["line"])
        if located:
            out.append(located)
    return out


def _unique_members(located: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out = []
    for loc in located:
        key = (_norm(loc.get("fqcn", "")), _norm(loc.get("member", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
    return out


def _resolve_heritage(res: Resolver, cls: dict, h: dict, ctx: FileCtx,
                      abs_path: str, lsp) -> dict:
    rec = {
        "class": cls.get("name", ""), "class_fqcn": cls.get("fqcn", ""),
        "ref": h.get("name", ""), "qualified": h.get("qualified", ""),
        "relation": h.get("relation", ""), "line": h.get("line", 0),
        "status": "unresolved", "via": "none", "target": None,
    }
    located = _lsp_definitions(res, lsp, abs_path, h.get("line", 0), h.get("col", 0))
    classes = [loc for loc in located if not loc.get("member")]
    if not classes and located:
        classes = located  # e.g. constructor location for the class
    if classes:
        loc = classes[0]
        rec["status"] = _status_for(loc)
        rec["via"] = "lsp"
        rec["target"] = _target(loc.get("fqcn", ""), "", loc.get("path", ""))
        return rec
    for cand in ctx.candidates(h.get("qualified", "")):
        found = res.find_class(cand)
        if found:
            _ent, cls_d, path, internal = found
            rec["status"] = "resolved" if internal else "external"
            rec["via"] = "parser"
            rec["target"] = _target(cls_d.get("fqcn", cand), "",
                                    res.index.rel(path) if internal else path)
            return rec
    return rec


def _resolve_import(res: Resolver, u: dict, abs_path: str, lsp) -> dict:
    rec = {"fqcn": u.get("fqcn", ""), "alias": u.get("alias", ""),
           "kind": u.get("kind", "class"), "line": u.get("line", 0),
           "status": "unresolved", "target_path": None}
    if u.get("kind") == "function":
        fn = res.index.find_function(u.get("fqcn", ""))
        if fn:
            rec["status"] = "resolved"
            rec["target_path"] = fn[0]
            return rec
    hit = res.index.find_internal(u.get("fqcn", ""))
    if hit:
        rec["status"] = "resolved"
        rec["target_path"] = hit[0]
        return rec
    located = _lsp_definitions(res, lsp, abs_path, u.get("line", 0), u.get("col", 0))
    if located:
        loc = located[0]
        rec["status"] = "resolved" if loc.get("internal") else "external"
        rec["target_path"] = loc.get("path")
    return rec


def _string_conventions(res: Resolver, site: dict, class_ctx: ClassCtx,
                        rec: dict) -> None:
    """Attach string-convention targets: loader class refs + callable literals."""
    name = site.get("name") or ""
    args = site.get("str_args") or []
    s0 = args[0] if args else None
    if name and isinstance(s0, str):
        cands = conv.loader_class_candidates(name, s0, class_ctx.root_ns)
        if not cands and _norm(name) == "get":
            cands = conv.locator_get_candidates(site.get("receiver", ""), s0,
                                                class_ctx.root_ns)
        for rule, fq in cands:
            found = res.find_class(fq)
            if found:
                _ent, cls_d, path, internal = found
                rec["string_target"] = _target(
                    cls_d.get("fqcn", fq), "",
                    res.index.rel(path) if internal else path)
                rec["string_rule"] = rule
                rec["string_internal"] = internal
                break
    # 'FQCN::method' literal callables in any string argument
    for s in args:
        if isinstance(s, str) and conv.CALLABLE_LITERAL.match(s):
            cls_txt, member = s.lstrip("\\").rsplit("::", 1)
            hit = res.walk_member(cls_txt, member)
            if hit:
                tgt, _rule = hit
                internal = tgt["path"] in res.index.internal
                rec["callable_target"] = tgt
                rec["callable_internal"] = internal
            break


def _resolve_call(res: Resolver, cls: dict, method: dict, site: dict,
                  ctx: FileCtx, class_ctx: ClassCtx, tracker: TypeTracker,
                  abs_path: str, lsp) -> dict:
    rec: dict[str, Any] = {
        "site": {
            "class": cls.get("name", ""), "class_fqcn": cls.get("fqcn", ""),
            "method": method.get("name", ""), "name": site.get("name", ""),
            "line": site.get("line", 0), "col": site.get("col", 0),
            "kind": site.get("kind", ""),
        },
        "receiver": site.get("receiver", ""),
        "status": "unresolved", "via": "none",
        "target": None, "candidates": [],
    }
    _string_conventions(res, site, class_ctx, rec)

    if site.get("dynamic"):
        rec["status"] = "dynamic"
        return rec

    # -- object creation: resolve the class, point at __construct if present --
    if site.get("kind") == "new":
        located = _lsp_definitions(res, lsp, abs_path, site["line"], site["col"])
        loc = located[0] if located else None
        if loc is None:
            cand_txt = site.get("qualified") or site.get("name", "")
            for cand in ctx.candidates(cand_txt):
                found = res.find_class(cand)
                if found:
                    _ent, cls_d, path, internal = found
                    loc = {"fqcn": cls_d.get("fqcn", cand), "member": "",
                           "path": res.index.rel(path) if internal else path,
                           "internal": internal}
                    rec["via"] = "parser"
                    break
        else:
            rec["via"] = "lsp"
        if loc is not None:
            fqcn = loc.get("fqcn", "")
            member = ""
            found = res.find_class(fqcn)
            if found:
                _ent, cls_d, _path, _internal = found
                if any(_norm(m["name"]) == "__construct" for m in cls_d.get("methods", [])):
                    member = "__construct"
            rec["status"] = "resolved" if loc.get("internal") else "external"
            rec["target"] = _target(fqcn, member, loc.get("path", ""))
        return rec

    # -- named function/method/static calls --
    located = _unique_members(
        [loc for loc in _lsp_definitions(res, lsp, abs_path, site["line"], site["col"])
         if loc.get("member") and loc.get("member_kind") in ("method", "function")])

    if len(located) == 1:
        loc = located[0]
        rec["status"] = _status_for(loc)
        rec["via"] = "lsp"
        rec["target"] = _target_from_located(loc)
        _refine_interface_target(res, rec, site, tracker)
    elif len(located) > 1:
        rec["status"] = "ambiguous"
        rec["via"] = "lsp"
        rec["candidates"] = [_target_from_located(loc) for loc in located]
    else:
        _resolve_by_convention(res, site, ctx, class_ctx, tracker, rec)

    # Late static binding: static::m() resolved to the declaring class is
    # genuinely ambiguous when internal subclasses override m.
    if rec["status"] in ("resolved", "external") and site.get("kind") == "static" \
            and site.get("receiver", "").strip() == "static" and rec["target"]:
        overrides = res.descendants_overriding(rec["target"]["fqcn"],
                                               rec["target"]["member"])
        if overrides:
            base = rec["target"]
            rec["status"] = "ambiguous"
            rec["candidates"] = [base] + [
                _target(fq, base["member"], "") for fq in overrides]
            rec["target"] = None
    return rec


def _mark_dynamic(res: Resolver, site: dict, rec: dict):
    """Runtime-computed receiver: report DYNAMIC with same-name candidates."""
    rec["status"] = "dynamic"
    rec["target"] = None
    cands = res.index.methods_named(site.get("name", ""))
    if 0 < len(cands) <= config.POSSIBLY_CALLS_MAX_CANDIDATES:
        rec["candidates"] = cands


def _refine_interface_target(res: Resolver, rec: dict, site: dict,
                             tracker: TypeTracker) -> None:
    """When the LSP resolves a call to an interface method, refine to the
    concrete declaring class/trait via the tracked receiver.

    Intelephense returns the interface declaration for interface-typed
    receivers (`EntityInterface::set`), but the graph wants the class/trait
    that actually defines the body (`EntityTrait::set`). Only replaces the
    target when a concrete (non-interface) declaration is confirmed — so it
    can never introduce a wrong edge.
    """
    tgt = rec.get("target") or {}
    if not tgt.get("member") or not tgt.get("fqcn"):
        return
    found = res.find_class(tgt["fqcn"])
    if not found or found[1].get("kind") != "interface":
        return
    recv_type = tracker.type_of(site.get("receiver", ""), site.get("line", 0))
    if not recv_type or recv_type == DYNAMIC_TYPE:
        return
    hit = res.walk_member(recv_type, tgt["member"])
    if not hit or hit[1] != "chain":
        return
    conc = hit[0]
    f2 = res.find_class(conc.get("fqcn", ""))
    if f2 and f2[1].get("kind") == "interface":
        return  # still an interface — no improvement
    rec["target"] = conc
    rec["via"] = f"{rec['via']}+concrete"
    rec["status"] = "resolved" if conc["path"] in res.index.internal else "external"


def _resolve_by_convention(res: Resolver, site: dict, ctx: FileCtx,
                           class_ctx: ClassCtx, tracker: TypeTracker,
                           rec: dict) -> None:
    """Fallback chain when the LSP produced nothing for a named call."""
    name = site.get("name", "")
    kind = site.get("kind", "")
    receiver = site.get("receiver", "").strip()

    if kind == "function":
        for cand in ctx.function_candidates(site.get("qualified") or name):
            fn = res.index.find_function(cand)
            if fn:
                rel, m = fn
                rec["status"] = "resolved"
                rec["via"] = "convention:function_fallback"
                rec["target"] = _target("", cand, rel,
                                        m.get("start_line", 0), "function")
                return
        return

    # `$class::create()` — variable static scope is inherently runtime
    if kind == "static" and receiver.startswith("$"):
        recv_type = tracker.type_of(receiver, site.get("line", 0))
        if recv_type is None or recv_type == DYNAMIC_TYPE:
            _mark_dynamic(res, site, rec)
            return

    recv_type = tracker.type_of(receiver, site.get("line", 0))
    if recv_type == DYNAMIC_TYPE:
        _mark_dynamic(res, site, rec)
        return
    if not recv_type:
        return
    static = kind == "static" and receiver not in ("parent", "self", "static")
    hit = res.walk_member(recv_type, name, static=static)
    if hit:
        tgt, rule = hit
        if rule == "magic_call":
            # Proxy pattern: __call forwarding to a uniquely-identifiable
            # concrete class (typed prop / promoted ctor param / new inside
            # the magic body). Existence-confirmed; else __call stands.
            fwd = res.magic_forward(recv_type, name)
            if fwd is not None:
                tgt, rule = fwd, "magic_proxy"
            elif conv.MAGIC_FINDER.match(name):
                rule = "magic_finder"
        internal = tgt["path"] in res.index.internal
        rec["status"] = "resolved" if internal else "external"
        rec["via"] = f"convention:{rule}"
        rec["target"] = tgt


def _resolve_prop(res: Resolver, cls: dict, method: dict, pr: dict,
                  tracker: TypeTracker) -> dict | None:
    """Entity virtual fields (`_getX`) and `__get` magic for property reads.

    Declared properties are skipped entirely — only magic accessors produce
    a resolution record (and later, potentially, a call edge).
    """
    recv_type = tracker.type_of(pr.get("receiver", ""), pr.get("line", 0))
    if not recv_type:
        return None
    if res.property_declared(recv_type, pr.get("name", "")):
        return None
    base = {
        "site": {"class": cls.get("name", ""), "class_fqcn": cls.get("fqcn", ""),
                 "method": method.get("name", ""), "name": pr.get("name", ""),
                 "line": pr.get("line", 0), "col": pr.get("col", 0),
                 "kind": "property_access"},
        "receiver": pr.get("receiver", ""),
        "status": "unresolved", "via": "none", "target": None, "candidates": [],
    }
    getter = conv.virtual_getter_name(pr.get("name", ""))
    hit = res.walk_member(recv_type, getter)
    if hit and hit[1] == "chain":
        tgt, _rule = hit
        internal = tgt["path"] in res.index.internal
        base["status"] = "resolved" if internal else "external"
        base["via"] = "convention:entity_virtual"
        base["target"] = tgt
        return base
    hit = res.walk_member(recv_type, "__get")
    if hit and hit[1] in ("chain", "magic_call"):
        tgt, _rule = hit
        # __get proxy forward: a uniquely-identifiable forwarded class with a
        # plain `get{Prop}()` getter beats reporting __get itself.
        prop = pr.get("name", "")
        plain_getter = "get" + prop[:1].upper() + prop[1:]
        fwd = res.magic_forward(recv_type, plain_getter)
        via = "convention:magic_get"
        if fwd is not None:
            tgt, via = fwd, "convention:magic_proxy"
        internal = tgt["path"] in res.index.internal
        base["status"] = "resolved" if internal else "external"
        base["via"] = via
        base["target"] = tgt
        return base
    return None


def _resolve_class_ref(res: Resolver, cls: dict, method: dict, cr: dict,
                       ctx: FileCtx, tracker: TypeTracker) -> dict | None:
    rec = {
        "site": {"class": cls.get("name", ""), "class_fqcn": cls.get("fqcn", ""),
                 "method": method.get("name", ""), "name": cr.get("name", ""),
                 "line": cr.get("line", 0), "col": cr.get("col", 0),
                 "kind": cr.get("kind", "")},
        "status": "unresolved", "via": "none", "target": None,
    }
    if cr.get("kind") == "class_literal":
        for cand in ctx.candidates(cr.get("text", "")):
            found = res.find_class(cand)
            if found:
                _ent, cls_d, path, internal = found
                rec["status"] = "resolved" if internal else "external"
                rec["via"] = "parser"
                rec["target"] = _target(cls_d.get("fqcn", cand), "",
                                        res.index.rel(path) if internal else path)
                return rec
        return rec
    if cr.get("kind") == "array_callable":
        recv_type = tracker.type_of(cr.get("text", ""), cr.get("line", 0))
        if not recv_type or recv_type == DYNAMIC_TYPE:
            return None
        hit = res.walk_member(recv_type, cr.get("name", ""))
        if hit:
            tgt, _rule = hit
            internal = tgt["path"] in res.index.internal
            rec["status"] = "resolved" if internal else "external"
            rec["via"] = "convention:array_callable"
            rec["target"] = tgt
            return rec
        return rec
    if cr.get("kind") == "callable_string":
        # PHP callable strings are always fully qualified — no alias rules.
        hit = res.walk_member(cr.get("text", "").lstrip("\\"), cr.get("name", ""))
        if hit:
            tgt, _rule = hit
            internal = tgt["path"] in res.index.internal
            rec["status"] = "resolved" if internal else "external"
            rec["via"] = "convention:callable_literal"
            rec["target"] = tgt
        return rec
    return None


def resolve_file(res: Resolver, rel: str, entity: dict, lsp) -> dict:
    abs_path = res.index.abs(rel)
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        mtime = 0.0
    ctx = FileCtx(entity)
    out: dict[str, Any] = {"mtime": mtime, "inherits": [], "imports": [],
                           "calls": [], "props": [], "class_refs": []}
    if lsp is not None:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lsp.did_open(abs_path, f.read())
        except Exception:
            pass

    for cls in entity.get("classes", []):
        class_ctx = ClassCtx(res, ctx, cls)
        for h in cls.get("heritage", []):
            out["inherits"].append(_resolve_heritage(res, cls, h, ctx, abs_path, lsp))
        for m in cls.get("methods", []):
            if m.get("member_kind") not in ("method", "function"):
                continue
            site_records: dict[tuple[int, int], dict] = {}
            tracker = TypeTracker(res, ctx, class_ctx, m, site_records)
            for site in sorted(m.get("call_sites", []),
                               key=lambda s: (s.get("line", 0), s.get("col", 0))):
                r = _resolve_call(res, cls, m, site, ctx, class_ctx, tracker,
                                  abs_path, lsp)
                site_records[(site.get("line", 0), site.get("col", 0))] = r
                out["calls"].append(r)
            for pr in m.get("prop_sites", []):
                r = _resolve_prop(res, cls, m, pr, tracker)
                if r:
                    out["props"].append(r)
            for cr in m.get("class_refs", []):
                r = _resolve_class_ref(res, cls, m, cr, ctx, tracker)
                if r:
                    out["class_refs"].append(r)

    for u in entity.get("uses", []):
        out["imports"].append(_resolve_import(res, u, abs_path, lsp))

    if lsp is not None:
        try:
            lsp.did_close(abs_path)
        except Exception:
            pass
    return out


# ═══════════════════════════════════════════════════════════════════
# Phase entry point + checkpoint
# ═══════════════════════════════════════════════════════════════════


def _checkpoint_path() -> str:
    return os.path.join(os.getcwd(), config.OUTPUT_DIR, "resolutions.json")


def _load_resolution_checkpoint(target_dir: str, engine: str) -> dict:
    path = _checkpoint_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if (payload.get("version") == RESOLUTIONS_VERSION
                and payload.get("engine", {}).get("entities_version") == ENTITIES_VERSION
                and payload.get("engine", {}).get("intelephense") == engine
                and payload.get("target_dir") == os.path.abspath(target_dir)):
            return payload.get("files", {})
    except Exception:
        pass
    return {}


def _save_resolution_checkpoint(target_dir: str, engine: str,
                                files: dict, stats: dict):
    path = _checkpoint_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": RESOLUTIONS_VERSION,
        "engine": {"intelephense": engine, "entities_version": ENTITIES_VERSION},
        "target_dir": os.path.abspath(target_dir),
        "files": files,
        "stats": stats,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def collect_stats(files: dict) -> dict:
    stats: dict[str, int] = {}
    for rec in files.values():
        for r in rec.get("calls", []):
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        for r in rec.get("inherits", []):
            key = f"inherit_{r['status']}"
            stats[key] = stats.get(key, 0) + 1
    return stats


def run_resolution(data, printer=print, lsp_client_factory=None) -> dict:
    """Phase 1.6 entry point. Returns stats; sets data.resolutions.

    `lsp_client_factory` is injectable for tests; default builds an
    Intelephense LspClient from config.
    """
    from .lsp_client import LspClient, LspUnavailable

    target_dir = os.path.abspath(data.target_dir)
    origins = getattr(data, "file_origins", {}) or {}
    index = EntityIndex(target_dir, data.extracted_entities, origins)

    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    lsp = None
    engine = "unavailable"
    try:
        if lsp_client_factory is not None:
            lsp = lsp_client_factory()
        else:
            lsp = LspClient(
                command=[config.INTELEPHENSE_PATH, "--stdio"],
                root_dir=target_dir,
                storage_dir=os.path.join(out_root, "lsp_cache"),
                index_timeout=config.LSP_INDEX_TIMEOUT,
                request_timeout=config.LSP_REQUEST_TIMEOUT,
            )
        lsp.start()
        engine = getattr(lsp, "server_version", "") or "unknown"
        if getattr(lsp, "indexing_partial", False):
            printer("  [Phase 1.6] WARNING: workspace indexing did not finish "
                    "within LSP_INDEX_TIMEOUT — definitions may be partial "
                    "(unresolved calls increase; no wrong edges).")
    except LspUnavailable as e:
        lsp = None
        printer("  " + "!" * 66)
        printer(f"  [Phase 1.6] WARNING: Intelephense unavailable ({e}).")
        printer("  [Phase 1.6] Falling back to convention/parser resolution only —")
        printer("  [Phase 1.6] unresolved calls go to PossiblyCalls, never to MethodCalls.")
        printer(f"  [Phase 1.6] Install: npm i -g intelephense  (or set INTELEPHENSE_PATH)")
        printer("  " + "!" * 66)

    res = Resolver(index, lsp, printer)
    prev = _load_resolution_checkpoint(target_dir, engine)
    files_out: dict[str, Any] = {}
    app_files = [rel for rel in sorted(index.internal)
                 if origins.get(rel, "app") == "app"]

    reused = 0
    try:
        for i, rel in enumerate(app_files):
            abs_path = index.abs(rel)
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError:
                mtime = 0.0
            cached = prev.get(rel)
            if cached and cached.get("mtime") == mtime:
                files_out[rel] = cached
                reused += 1
                continue
            files_out[rel] = resolve_file(res, rel, index.internal[rel], lsp)
            if (i + 1) % 200 == 0:
                _save_resolution_checkpoint(target_dir, engine, files_out,
                                            collect_stats(files_out))
                printer(f"  [Phase 1.6] {i + 1}/{len(app_files)} files resolved "
                        f"(checkpointed)")
    finally:
        if lsp is not None:
            try:
                lsp.close()
            except Exception:
                pass

    stats = collect_stats(files_out)
    stats["files"] = len(files_out)
    stats["files_reused"] = reused
    stats["engine"] = engine
    _save_resolution_checkpoint(target_dir, engine, files_out,
                                {k: v for k, v in stats.items() if k != "engine"})
    data.resolutions = {"engine": engine, "files": files_out}
    return stats
