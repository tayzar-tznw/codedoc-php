"""Cross-repository dependency & coupling analysis.

The per-repo graph track keeps repositories as isolated islands — a reference
from repo B into a symbol defined in repo A is dropped as external/unresolved,
so nothing records how repos depend on each other. This module adds that layer
*after* repos are ingested, without merging them:

  - build a global symbol registry (FQCN / function name → owning repo +
    deterministic node ID) from every ingested repo's committed entities.json,
  - detect references in repo B that resolve to a class/method/function defined
    in a DIFFERENT repo A, across every statically-visible reference kind:
    `use` imports, `::class` literals, callable strings, `instanceof`,
    `extends`/`implements`/trait-`use` heritage, `new` expressions, parameter/
    return type hints, method calls (typed/injected/chained receivers), plain
    function calls, and DI container wiring, and
  - emit CrossRepoRef (Class→Class), CrossRepoFileRef (File→Class, for files
    that define no classes — bootstrap/config wiring), CrossRepoCalls
    (Method→Method), DiBinds / DiInjects edges plus a repo×repo coupling
    matrix and an explicit report of everything that was DROPPED and why.

Correctness rules (the zero-wrong-edge contract for this layer):
  - Class references resolve to exactly ONE FQCN per PHP semantics
    (FileCtx.resolve_class_strict — no global-namespace fallback; callable
    strings are fully qualified verbatim). Functions use PHP's real
    current-namespace-then-global fallback.
  - Vendor-origin entities are mirrors, not owners: they never register
    symbols and never act as reference sources. A consumer's vendor copy of an
    ingested library therefore defers to the library's source repo.
  - A committed (app-origin) definition in the referencing repo itself wins —
    no cross edge (matches composer runtime).
  - An FQCN owned by more than one other repo is ambiguous: no edge, and the
    FQCN is REPORTED (never silently dropped).

Derivation is pure and local (no GCP); only run_crossref() touches Spanner.
Node IDs are recomputed with the same `pipeline._make_id` used at ingest, so
they match the rows Phase 8 wrote exactly (discover_repos enforces matching
ID_SCHEME and ID_PREFIX).
"""

from __future__ import annotations

import glob
import json
import os

from . import config
from .pipeline import _make_id, ID_SCHEME
from .resolution import (EntityIndex, FileCtx, Resolver, ClassCtx, TypeTracker,
                         DYNAMIC_TYPE)
from .treesitter_parser import ENTITIES_VERSION


def _norm(s: str) -> str:
    return (s or "").lower()


def _origin(rel: str) -> str:
    """'vendor' if the relative path passes through a vendor/ dir, else 'app'.
    (Same rule as pipeline scanning — vendor copies are mirrors.)"""
    return "vendor" if "vendor" in rel.replace("/", os.sep).split(os.sep) else "app"


# PHP built-in / pseudo type names that are never class references.
_BUILTIN_TYPES = {
    "int", "integer", "float", "double", "string", "bool", "boolean", "array",
    "void", "mixed", "null", "callable", "iterable", "object", "never", "true",
    "false", "self", "static", "parent", "this", "resource", "numeric",
}


def _split_type(text: str) -> list[str]:
    """Class-name parts of a type expression: strips `?`, splits `|`/`&`
    unions, drops built-ins."""
    out: list[str] = []
    for part in text.replace("&", "|").split("|"):
        part = part.strip().lstrip("?").strip()
        if not part:
            continue
        if part.lstrip("\\").rsplit("\\", 1)[-1].lower() in _BUILTIN_TYPES:
            continue
        if part.lstrip("\\") and part.lstrip("\\")[0].isalpha() or part.startswith("\\"):
            out.append(part)
    return out


# ═══════════════════════════════════════════════════════════════════
# Repo discovery + global registry
# ═══════════════════════════════════════════════════════════════════


def discover_repos(output_dir: str, printer=print) -> list[dict]:
    """Find every ingested repo under <output_dir>/repos/*/ that has both
    graph_meta.json and a current-version entities.json, ingested under the
    CURRENT ID_SCHEME and ID_PREFIX (otherwise recomputed edge endpoints would
    not match the written nodes)."""
    repos: list[dict] = []
    for meta_path in sorted(glob.glob(os.path.join(output_dir, "repos", "*", "graph_meta.json"))):
        rdir = os.path.dirname(meta_path)
        name = os.path.basename(rdir)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            with open(os.path.join(rdir, "entities.json"), "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("id_scheme") != ID_SCHEME or payload.get("version") != ENTITIES_VERSION:
            printer(f"  [crossref] skipping {name} "
                    "(old id_scheme/entities version — re-ingest)")
            continue
        if meta.get("id_prefix") != config.ID_PREFIX:
            printer(f"  [crossref] skipping {name} (ingested under "
                    f"ID_PREFIX={meta.get('id_prefix')!r}, current is "
                    f"{config.ID_PREFIX!r} — edges would not match its nodes)")
            continue
        repos.append({
            "repo": meta.get("repo") or name,
            "target_dir": meta.get("target_dir", ""),
            "entities": payload.get("entities", {}),
        })
    return repos


class Registry:
    """Global symbol registry across all ingested repos (app-origin only)."""

    def __init__(self):
        # norm fqcn → list of entries; >1 entry means the FQCN is defined in
        # multiple places → ambiguous when referenced from a third party.
        self.by_fqcn: dict[str, list[dict]] = {}
        # norm fully-qualified function name → list of entries
        self.functions: dict[str, list[dict]] = {}
        self._merged: dict[str, dict] = {}
        self.index: EntityIndex | None = None
        self.resolver: Resolver | None = None

    def owner_entries(self, fqcn: str) -> list[dict]:
        return self.by_fqcn.get(_norm(fqcn), [])


def build_registry(repos: list[dict]) -> Registry:
    reg = Registry()
    for r in repos:
        repo, target_dir = r["repo"], r["target_dir"]
        for abs_fp, ent in r["entities"].items():
            rel = os.path.relpath(abs_fp, target_dir) if target_dir else abs_fp
            if _origin(rel) == "vendor":
                continue  # vendor copies are mirrors — they own nothing
            reg._merged[abs_fp] = ent
            ns = ent.get("namespace", "")
            for cls in ent.get("classes", []):
                if cls.get("name") == "(global)":
                    for m in cls.get("methods", []):
                        if m.get("member_kind") != "function":
                            continue
                        fq = f"{ns}\\{m['name']}" if ns else m["name"]
                        reg.functions.setdefault(_norm(fq), []).append({
                            "repo": repo, "rel": rel, "fq_name": fq,
                            "method_id": _make_id("method", repo, rel,
                                                  "(global)", m["name"]),
                        })
                    continue
                fqcn = cls.get("fqcn") or ""
                if not fqcn:
                    continue
                class_id = _make_id("class", repo, rel, fqcn)
                method_ids = {
                    _norm(m["name"]): _make_id("method", repo, rel, fqcn, m["name"])
                    for m in cls.get("methods", []) if m.get("name")
                }
                reg.by_fqcn.setdefault(_norm(fqcn), []).append({
                    "repo": repo, "rel": rel, "fqcn": fqcn,
                    "class_id": class_id, "method_ids": method_ids,
                })
    # Global (app-only) index for walking parent chains / return types during
    # method-level resolution.
    reg.index = EntityIndex("/", reg._merged)
    reg.resolver = Resolver(reg.index, lsp=None, printer=lambda *a: None)
    return reg


# ═══════════════════════════════════════════════════════════════════
# Pure derivation
# ═══════════════════════════════════════════════════════════════════


def derive_cross_repo(repos: list[dict], registry: Registry, printer=print) -> dict:
    """Return {"CrossRepoRef", "CrossRepoFileRef", "CrossRepoCalls", "DiBinds",
    "DiInjects", "coupling", "stats", "drops"}.

    An edge is emitted only when the reference resolves (per strict PHP name
    rules) to a symbol defined in exactly one OTHER repo — never guessed.
    Local app-origin definitions win; ambiguous/unowned targets are recorded
    in `drops` so under-reporting is always visible.
    """
    assert registry.resolver is not None and registry.index is not None, \
        "build_registry() must run before derive_cross_repo()"
    ref_rows: list[list] = []
    file_ref_rows: list[list] = []
    call_rows: list[list] = []
    bind_rows: list[list] = []
    inject_rows: list[list] = []
    coupling: dict[tuple[str, str], dict[str, int]] = {}
    stats = {"ref_edges": 0, "file_ref_edges": 0, "call_edges": 0, "di_edges": 0,
             "local_definition": 0, "unowned_refs": 0, "use_function_imports": 0,
             "di_unowned": 0, "di_ambiguous": 0, "ambiguous_targets": 0}
    ambiguous: dict[str, list[str]] = {}   # fqcn → sorted repos defining it
    unowned_sample: set[str] = set()
    seen_ref: set[tuple] = set()
    seen_file_ref: set[tuple] = set()
    seen_call: set[tuple] = set()
    seen_di: set[tuple] = set()

    def _bump(src_repo, tgt_repo, key):
        c = coupling.setdefault((src_repo, tgt_repo),
                                {"refs": 0, "calls": 0, "di": 0})
        c[key] += 1

    def _record_ambiguous(fqcn: str, entries: list[dict]):
        if fqcn not in ambiguous:
            ambiguous[fqcn] = sorted({e["repo"] for e in entries})
            stats["ambiguous_targets"] = len(ambiguous)

    def _cross_owner(fqcn: str, src_repo: str) -> dict | None:
        """Owning entry iff defined in exactly one OTHER repo. A committed
        definition in the source repo wins (None); multiple owners → None +
        reported; unowned → None + sampled."""
        if not fqcn:
            return None
        entries = registry.owner_entries(fqcn)
        if not entries:
            stats["unowned_refs"] += 1
            if len(unowned_sample) < 200:
                unowned_sample.add(fqcn)
            return None
        if any(e["repo"] == src_repo for e in entries):
            stats["local_definition"] += 1
            return None
        if len(entries) > 1:
            _record_ambiguous(fqcn, entries)
            return None
        return entries[0]

    def _endpoint_owner(fqcn: str, src_repo: str) -> dict | None:
        """DI endpoints may be local or remote. The wiring repo's own
        definition is preferred; otherwise the unique definition; multiple
        foreign definitions → ambiguous (reported)."""
        entries = registry.owner_entries(fqcn)
        if not entries:
            stats["di_unowned"] += 1
            return None
        local = [e for e in entries if e["repo"] == src_repo]
        if len(local) == 1:
            return local[0]
        if len(entries) == 1:
            return entries[0]
        stats["di_ambiguous"] += 1
        _record_ambiguous(fqcn, entries)
        return None

    def _emit_ref(src_cid: str, owner: dict, kind: str, src_repo: str):
        key = (src_cid, owner["class_id"], kind)
        if key in seen_ref:
            return
        seen_ref.add(key)
        eid = _make_id("xref", src_cid, owner["class_id"], kind)
        ref_rows.append([eid, src_cid, owner["class_id"], src_repo,
                         owner["repo"], owner["fqcn"], kind])
        _bump(src_repo, owner["repo"], "refs")
        stats["ref_edges"] += 1

    def _class_reference_list(ctx: FileCtx, cls: dict) -> list[tuple[str, str]]:
        """All (strict FQCN, kind) class references made by one class."""
        refs: list[tuple[str, str]] = []
        for h in cls.get("heritage", []):
            fq = ctx.resolve_class_strict(h.get("qualified", ""))
            if fq:
                refs.append((fq, h.get("relation", "extends")))
        for m in cls.get("methods", []):
            for cr in m.get("class_refs", []):
                k = cr.get("kind")
                if k == "class_literal":
                    refs.append((ctx.resolve_class_strict(cr.get("text", "")), "class_ref"))
                elif k == "instanceof":
                    refs.append((ctx.resolve_class_strict(cr.get("text", "")), "instanceof"))
                elif k == "callable_string":
                    # PHP callable strings are always fully qualified
                    refs.append((cr.get("text", "").lstrip("\\"), "class_ref"))
                # array_callable: receiver text, not a class name — excluded
            for site in m.get("call_sites", []):
                if site.get("kind") == "new" and not site.get("dynamic") \
                        and (site.get("qualified") or site.get("name")):
                    refs.append((ctx.resolve_class_strict(
                        site.get("qualified") or site.get("name", "")), "new"))
            for t in (m.get("param_types") or {}).values():
                for part in _split_type(t):
                    refs.append((ctx.resolve_class_strict(part), "type_hint"))
            rt = m.get("return_type") or ""
            for part in _split_type(rt):
                refs.append((ctx.resolve_class_strict(part), "type_hint"))
        return [(f, k) for f, k in refs if f]

    def _mention_tokens(cls: dict) -> set[str]:
        """Normalized first-segment tokens a class textually mentions — used
        to attribute file-level `use` imports to the classes that use them."""
        toks: set[str] = set()

        def _add(text: str):
            text = (text or "").strip().lstrip("?\\")
            if text:
                toks.add(_norm(text.split("\\", 1)[0]))

        for h in cls.get("heritage", []):
            _add(h.get("qualified", ""))
        for m in cls.get("methods", []):
            for site in m.get("call_sites", []):
                _add(site.get("qualified", ""))
                _add(site.get("receiver", ""))
                if site.get("kind") == "new":
                    _add(site.get("name", ""))
            for cr in m.get("class_refs", []):
                _add(cr.get("text", ""))
            for t in (m.get("param_types") or {}).values():
                for part in _split_type(t):
                    _add(part)
            for part in _split_type(m.get("return_type") or ""):
                _add(part)
        return toks

    for r in repos:
        src_repo, target_dir = r["repo"], r["target_dir"]
        for abs_fp, ent in r["entities"].items():
            rel = os.path.relpath(abs_fp, target_dir) if target_dir else abs_fp
            if _origin(rel) == "vendor":
                continue  # vendor files are mirrors — never reference sources
            ctx = FileCtx(ent)

            local_classes = [c for c in ent.get("classes", [])
                             if (c.get("fqcn") or "") and c.get("name") != "(global)"]
            global_classes = [c for c in ent.get("classes", [])
                              if c.get("name") == "(global)"]

            # ---- class-level references, attributed per owning class ----
            class_infos = []
            for cls in local_classes:
                cid = _make_id("class", src_repo, rel, cls["fqcn"])
                class_infos.append((cls, cid, _class_reference_list(ctx, cls),
                                    _mention_tokens(cls)))

            for cls, cid, refs, _toks in class_infos:
                for fq, kind in refs:
                    owner = _cross_owner(fq, src_repo)
                    if owner is not None:
                        _emit_ref(cid, owner, kind, src_repo)

            # file-level `use` imports → the classes that mention the alias
            # (fallback: all classes in the file; classless files → file ref)
            class_use_refs = []
            for u in ent.get("uses", []):
                if u.get("kind") == "function":
                    stats["use_function_imports"] += 1
                    continue
                if u.get("kind") != "class" or not u.get("fqcn"):
                    continue
                class_use_refs.append((u["fqcn"], u.get("alias", "")))

            for fqcn, alias in class_use_refs:
                owner = _cross_owner(fqcn, src_repo)
                if owner is None:
                    continue
                if class_infos:
                    users = [(cls, cid) for cls, cid, _r, toks in class_infos
                             if _norm(alias) in toks]
                    if not users:
                        users = [(cls, cid) for cls, cid, _r, _t in class_infos]
                    for _cls, cid in users:
                        _emit_ref(cid, owner, "import", src_repo)
                else:
                    key = (rel, owner["class_id"], "import")
                    if key not in seen_file_ref:
                        seen_file_ref.add(key)
                        src_fid = _make_id("file", src_repo, rel)
                        eid = _make_id("xfref", src_fid, owner["class_id"], "import")
                        file_ref_rows.append([eid, src_fid, owner["class_id"],
                                              src_repo, owner["repo"],
                                              owner["fqcn"], "import"])
                        _bump(src_repo, owner["repo"], "refs")
                        stats["file_ref_edges"] += 1

            # classless files: class refs made inside (global) functions also
            # become file-level refs (bootstrap/config wiring)
            if not local_classes:
                for gcls in global_classes:
                    for fq, kind in _class_reference_list(ctx, gcls):
                        owner = _cross_owner(fq, src_repo)
                        if owner is None:
                            continue
                        key = (rel, owner["class_id"], kind)
                        if key in seen_file_ref:
                            continue
                        seen_file_ref.add(key)
                        src_fid = _make_id("file", src_repo, rel)
                        eid = _make_id("xfref", src_fid, owner["class_id"], kind)
                        file_ref_rows.append([eid, src_fid, owner["class_id"],
                                              src_repo, owner["repo"],
                                              owner["fqcn"], kind])
                        _bump(src_repo, owner["repo"], "refs")
                        stats["file_ref_edges"] += 1

            # ---- method/function-call level ----
            for cls in local_classes + global_classes:
                cls_key = cls.get("fqcn") or cls.get("name", "")
                class_ctx = ClassCtx(registry.resolver, ctx, cls)
                # Constructor-promoted properties (DI pattern) seed $this->prop
                # types — the tracker can't see them without LSP.
                for cm in cls.get("methods", []):
                    if cm.get("name") == "__construct":
                        for pname, ptype in (cm.get("param_types") or {}).items():
                            prop = _norm(pname.lstrip("$"))
                            if prop in class_ctx.prop_types:
                                continue
                            for cand in ctx.candidates(ptype.lstrip("?")):
                                if registry.resolver.find_class(cand):
                                    class_ctx.prop_types[prop] = cand
                                    break
                for meth in cls.get("methods", []):
                    if meth.get("member_kind") not in ("method", "function"):
                        continue
                    caller_mid = _make_id("method", src_repo, rel, cls_key,
                                          meth.get("name", ""))
                    site_records: dict = {}
                    tracker = TypeTracker(registry.resolver, ctx, class_ctx,
                                          meth, site_records)
                    for site in sorted(meth.get("call_sites", []),
                                       key=lambda s: (s.get("line", 0), s.get("col", 0))):
                        if site.get("dynamic") or not site.get("name"):
                            continue
                        kind = site.get("kind")
                        if kind == "function":
                            # PHP functions DO fall back current-ns → global
                            for cand in ctx.function_candidates(
                                    site.get("qualified") or site["name"]):
                                fns = registry.functions.get(_norm(cand), [])
                                if not fns:
                                    continue
                                if any(f["repo"] == src_repo for f in fns):
                                    break  # local function — per-repo domain
                                if len(fns) > 1:
                                    _record_ambiguous(cand, fns)
                                    break
                                fn = fns[0]
                                key = (caller_mid, fn["method_id"])
                                if key not in seen_call:
                                    seen_call.add(key)
                                    eid = _make_id("xcall", caller_mid, fn["method_id"])
                                    call_rows.append([eid, caller_mid,
                                                      fn["method_id"], src_repo,
                                                      fn["repo"], fn["fq_name"]])
                                    _bump(src_repo, fn["repo"], "calls")
                                    stats["call_edges"] += 1
                                break
                            continue
                        if kind not in ("method", "nullsafe", "static"):
                            continue
                        recv_type = tracker.type_of(site.get("receiver", ""),
                                                    site.get("line", 0))
                        resolved = None
                        if recv_type and recv_type != DYNAMIC_TYPE:
                            hit = registry.resolver.walk_member(recv_type, site["name"])
                            if hit:
                                resolved = hit[0]
                        # Record every resolution (incl. same-repo) so chained
                        # receivers ($sum = $a->add($b); $sum->amount()) type.
                        site_records[(site.get("line", 0), site.get("col", 0))] = {
                            "receiver": site.get("receiver", ""),
                            "target": resolved, "string_target": None,
                            "candidates": [],
                        }
                        if resolved is None:
                            continue
                        owner = _cross_owner(resolved.get("fqcn", ""), src_repo)
                        if owner is None:
                            continue
                        callee_mid = owner["method_ids"].get(
                            _norm(resolved.get("member", "")))
                        if not callee_mid:
                            continue
                        key = (caller_mid, callee_mid)
                        if key in seen_call:
                            continue
                        seen_call.add(key)
                        eid = _make_id("xcall", caller_mid, callee_mid)
                        call_rows.append([eid, caller_mid, callee_mid, src_repo,
                                          owner["repo"],
                                          f"{resolved.get('fqcn')}::{resolved.get('member')}"])
                        _bump(src_repo, owner["repo"], "calls")
                        stats["call_edges"] += 1

            # ---- DI wiring ----
            for b in ent.get("di_bindings", []):
                kind = b.get("kind")
                if kind not in ("bind", "inject"):
                    continue
                s = _endpoint_owner(ctx.resolve_class_strict(b.get("source", "")), src_repo)
                t = _endpoint_owner(ctx.resolve_class_strict(b.get("target", "")), src_repo)
                if not s or not t:
                    continue
                if s["repo"] == src_repo and t["repo"] == src_repo:
                    continue  # both endpoints in the wiring repo → per-repo pass
                key = (kind, s["class_id"], t["class_id"])
                if key in seen_di:
                    continue
                seen_di.add(key)
                rows_out = bind_rows if kind == "bind" else inject_rows
                eid = _make_id("x" + kind, s["class_id"], t["class_id"])
                rows_out.append([eid, s["class_id"], t["class_id"],
                                 s["repo"], t["repo"], t["fqcn"]])
                _bump(s["repo"], t["repo"], "di")
                stats["di_edges"] += 1

    drops = {"ambiguous": ambiguous, "unowned_sample": sorted(unowned_sample)}
    return {"CrossRepoRef": ref_rows, "CrossRepoFileRef": file_ref_rows,
            "CrossRepoCalls": call_rows, "DiBinds": bind_rows,
            "DiInjects": inject_rows, "coupling": coupling, "stats": stats,
            "drops": drops}


# ═══════════════════════════════════════════════════════════════════
# Report + write
# ═══════════════════════════════════════════════════════════════════


def format_coupling_matrix(coupling: dict) -> str:
    if not coupling:
        return "  (no cross-repo references found)"
    lines = ["  source_repo → target_repo :  refs / calls / di"]
    for (src, tgt) in sorted(coupling):
        c = coupling[(src, tgt)]
        lines.append(f"    {src} → {tgt} :  {c['refs']} / {c['calls']} / {c.get('di', 0)}")
    return "\n".join(lines)


def format_drops(drops: dict, stats: dict, limit: int = 20) -> str:
    """Human-readable account of everything that was NOT edged and why —
    silent under-reporting is how impact analysis lies."""
    lines: list[str] = []
    ambiguous = drops.get("ambiguous", {})
    if ambiguous:
        lines.append(f"  AMBIGUOUS targets ({len(ambiguous)}) — defined in multiple "
                     "repos; ALL references to them were skipped:")
        for fqcn in sorted(ambiguous)[:limit]:
            lines.append(f"    {fqcn}  — defined in {ambiguous[fqcn]} "
                         "(rename/exclude one, or drop --include-vendor duplicates)")
        if len(ambiguous) > limit:
            lines.append(f"    ... and {len(ambiguous) - limit} more")
    unowned = stats.get("unowned_refs", 0)
    if unowned:
        prefixes: dict[str, int] = {}
        for fq in drops.get("unowned_sample", []):
            p = "\\".join(fq.split("\\")[:2])
            prefixes[p] = prefixes.get(p, 0) + 1
        top = ", ".join(f"{p} ({n})" for p, n in
                        sorted(prefixes.items(), key=lambda kv: -kv[1])[:10])
        lines.append(f"  UNOWNED references: {unowned} (targets not in any ingested "
                     f"repo — vendor libs, PHP built-ins, or repos not yet ingested). "
                     f"Top namespaces: {top}")
    if stats.get("di_unowned") or stats.get("di_ambiguous"):
        lines.append(f"  DI drops: {stats.get('di_unowned', 0)} unowned, "
                     f"{stats.get('di_ambiguous', 0)} ambiguous")
    if stats.get("local_definition"):
        lines.append(f"  (references satisfied by the repo's own committed code: "
                     f"{stats['local_definition']} — no cross edge, by design)")
    return "\n".join(lines) if lines else "  (nothing dropped)"


def run_crossref(printer=print, write: bool = True) -> dict:
    """Discover ingested repos, derive cross-repo edges, optionally write them
    to Spanner, print the coupling matrix + drop report, and persist
    OUTPUT_DIR/crossref_report.json for CI diffing. Returns the derivation."""
    from .pipeline import _get_spanner_db, _batch_insert
    from .setup_spanner_graph import write_columns

    empty = {"CrossRepoRef": [], "CrossRepoFileRef": [], "CrossRepoCalls": [],
             "DiBinds": [], "DiInjects": [], "coupling": {}, "stats": {},
             "drops": {"ambiguous": {}, "unowned_sample": []}}
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    repos = discover_repos(out_root, printer)
    if len(repos) < 2:
        printer(f"  [crossref] found {len(repos)} ingested repo(s) under "
                f"{out_root}/repos/ — need at least 2 for cross-repo analysis.")
        return empty

    printer(f"  [crossref] repos: {', '.join(r['repo'] for r in repos)}")
    registry = build_registry(repos)
    derived = derive_cross_repo(repos, registry, printer)

    st = derived["stats"]
    printer(f"  [crossref] {st.get('ref_edges', 0)} class-level + "
            f"{st.get('file_ref_edges', 0)} file-level + "
            f"{st.get('call_edges', 0)} method-level + {st.get('di_edges', 0)} "
            f"DI cross-repo edges")
    printer("  [crossref] coupling matrix:")
    printer(format_coupling_matrix(derived["coupling"]))
    printer("  [crossref] dropped / unresolved:")
    printer(format_drops(derived["drops"], st))

    # Persist the report (stats + coupling + drops) for CI diffing.
    try:
        report = {
            "stats": st,
            "coupling": {f"{s}->{t}": c for (s, t), c in derived["coupling"].items()},
            "drops": derived["drops"],
        }
        with open(os.path.join(out_root, "crossref_report.json"), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

    if write:
        db = _get_spanner_db()
        for table in ("CrossRepoRef", "CrossRepoFileRef", "CrossRepoCalls",
                      "DiBinds", "DiInjects"):
            rows = derived[table]
            if rows:
                _batch_insert(db, table, write_columns(table), rows)
                printer(f"  [crossref] wrote {len(rows)} {table} edges")
    return derived
