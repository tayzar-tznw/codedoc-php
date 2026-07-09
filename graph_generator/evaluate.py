"""Local evaluation harness for the PHP extractor + resolver (no GCP).

Runs Phases 1 → 1.5 → 1.6 plus pure node/edge derivation against the
committed fixtures in test_codes/, then scores:

  1. Entity coverage      — per ground_truth.json's `entity_schema_alignment`
                            rule: every case's defined_in parses to the
                            expected class+member; call-kind cases' callers
                            list the callee. Target ≥ 85%.
  2. GT completeness      — resolver answers == `expected` (or the candidate
                            set for AMBIGUOUS/DYNAMIC). Target ≥ 85%.
  3. QA completeness      — qa_questions.json checks against the derived
                            graph (when the file exists). Target ≥ 85%.
  4. Wrong edges          — MethodCalls edges contradicting ground truth.
                            Target = 0, structurally enforced by resolved-only
                            edge derivation and measured here.

Exit codes (via cmd_evaluate): 0 all targets met · 1 below target ·
2 environment problem (e.g. composer vendor missing).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from . import config
from .treesitter_parser import parse_entities

THRESHOLD = 85.0

_CALL_NAME = re.compile(r"(\w+)\s*\(")
_PROP_NAME = re.compile(r"->\s*(\w+)\s*$")


def _norm(s: str) -> str:
    return (s or "").lower()


# ═══════════════════════════════════════════════════════════════════
# Case-site location
# ═══════════════════════════════════════════════════════════════════


def _expr_position(fixture_dir: str, case: dict) -> tuple[int, int, int] | None:
    """(line, span_start, span_end) of the case's occurrence-th expr match.

    Columns are Python string indexes ≈ UTF-16 units for BMP text (fixtures
    are ASCII), directly comparable with resolution-record columns.
    """
    path = os.path.join(fixture_dir, case["file"])
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    expr = case["expr"]
    want = case.get("occurrence", 1)
    seen = 0
    for lineno, text in enumerate(lines, 1):
        count = text.count(expr)
        if seen + count >= want:
            idx = -1
            for _ in range(want - seen):
                idx = text.find(expr, idx + 1)
            return lineno, idx, idx + len(expr)
        seen += count
    return None


def _callee_name(case: dict) -> str:
    kind = case.get("kind", "")
    expr = case.get("expr", "")
    if kind == "property_access":
        m = _PROP_NAME.search(expr)
        return m.group(1) if m else ""
    matches = _CALL_NAME.findall(expr)
    if kind in ("method_call", "static_call", "new_object"):
        # The callee is the OUTER call — the first `name(` in the expr —
        # not a nested argument call (`$e->set('t', $req->getData('t'))`).
        return matches[0] if matches else ""
    if kind == "function_call":
        return matches[0].rsplit("\\", 1)[-1] if matches else ""
    if kind in ("class_ref", "callable_ref"):
        return matches[0] if matches else ""
    return ""


# Case kind → acceptable resolution-record site kinds (the parser's AST kind
# is authoritative — string heuristics can't tell the outer call from a nested
# argument call).
_KIND_SITE = {
    "method_call": {"method", "nullsafe"},
    "static_call": {"static"},
    "function_call": {"function"},
    "new_object": {"new"},
}


def _records_at(frec: dict, line: int, lists: tuple[str, ...]) -> list[dict]:
    out = []
    for key in lists:
        for r in frec.get(key, []):
            if r.get("site", {}).get("line") == line:
                out.append(r)
    return out


def _match_record(case: dict, frec: dict, pos: tuple[int, int, int]) -> dict | None:
    """Select the resolution record for a case by AST kind + expr span.

    Among records of the matching site-kind whose column falls inside the
    expr's span, the outer call is the leftmost (nested argument calls sit to
    the right; receiver constructors are a different site-kind), so the
    smallest-column record wins.
    """
    line, span_start, span_end = pos
    kind = case.get("kind", "")

    def _in_span(recs: list[dict]) -> list[dict]:
        hits = [r for r in recs
                if span_start <= r.get("site", {}).get("col", 0) <= span_end]
        return hits or recs

    def _leftmost(recs: list[dict]) -> dict | None:
        recs = _in_span(recs)
        if not recs:
            return None
        return min(recs, key=lambda r: r.get("site", {}).get("col", 0))

    if kind == "property_access":
        recs = _records_at(frec, line, ("props",))
        return _leftmost(recs) or _leftmost(_records_at(frec, line, ("calls",)))
    if kind == "class_ref":
        recs = [r for r in _records_at(frec, line, ("calls",)) if r.get("string_target")]
        return _leftmost(recs) or _leftmost(_records_at(frec, line, ("class_refs",)))
    if kind == "callable_ref":
        recs = _records_at(frec, line, ("class_refs",))
        recs = recs or [r for r in _records_at(frec, line, ("calls",))
                        if r.get("callable_target")]
        return _leftmost(recs) or _leftmost(_records_at(frec, line, ("calls",)))

    want = _KIND_SITE.get(kind, set())
    recs = [r for r in _records_at(frec, line, ("calls",))
            if r.get("site", {}).get("kind") in want]
    return _leftmost(recs) or _leftmost(_records_at(frec, line, ("calls",)))


# ═══════════════════════════════════════════════════════════════════
# Answers + scoring
# ═══════════════════════════════════════════════════════════════════


def _fq_answer(target: dict | None) -> str:
    if not target:
        return ""
    fqcn = target.get("fqcn") or ""
    member = target.get("member") or ""
    if fqcn and member:
        return f"{fqcn}::{member}"
    return fqcn or member


def _record_answer(case: dict, rec: dict) -> tuple[str, list[str]]:
    """(answer, candidate answers) in ground-truth notation."""
    kind = case.get("kind", "")
    if kind == "class_ref":
        st = rec.get("string_target")
        if st:
            return st.get("fqcn", ""), []
        return _fq_answer(rec.get("target")), []
    if kind == "callable_ref":
        ct = rec.get("callable_target")
        if ct:
            return _fq_answer(ct), []
    status = rec.get("status", "")
    if status == "ambiguous":
        return "AMBIGUOUS", [_fq_answer(c) for c in rec.get("candidates", [])]
    if status == "dynamic":
        return "DYNAMIC", [_fq_answer(c) for c in rec.get("candidates", [])]
    return _fq_answer(rec.get("target")), []


def score_case(case: dict, frec: dict | None, fixture_dir: str,
               entities_by_rel: dict[str, dict]) -> dict:
    """→ {correct: bool, answer: str, note: str} for one GT case."""
    expected = case.get("expected", "")
    result = {"id": case.get("id"), "category": case.get("category"),
              "expected": expected, "answer": "", "correct": False, "note": ""}

    if case.get("kind") == "callback_definition":
        # Reverse question ("who calls this") — correct when the callback
        # method exists as an extracted entity (framework calls it by name).
        ok = _entity_has(entities_by_rel, fixture_dir, case.get("defined_in", ""),
                         expected)
        result["answer"] = expected if ok else "(missing entity)"
        result["correct"] = ok
        return result

    if frec is None:
        result["note"] = "file has no resolution record"
        return result
    pos = _expr_position(fixture_dir, case)
    if pos is None:
        result["note"] = "expr not found at declared position"
        return result
    rec = _match_record(case, frec, pos)
    if rec is None:
        result["note"] = "no resolution record at site"
        return result

    answer, cand_answers = _record_answer(case, rec)
    result["answer"] = answer if answer not in ("AMBIGUOUS", "DYNAMIC") \
        else f"{answer}{sorted(cand_answers)}"

    if expected in ("AMBIGUOUS", "DYNAMIC"):
        gt_cands = {_norm(c.get("target", "")) for c in case.get("candidates", [])}
        if answer == expected:
            ours = {_norm(a) for a in cand_answers if a}
            # Correct when we report the status; candidate sets, when we have
            # them, must not contain anything outside the GT set.
            result["correct"] = (not ours) or ours <= gt_cands
            if ours and ours != gt_cands:
                result["note"] = "candidate set differs (subset ok)"
        return result

    result["correct"] = _norm(answer) == _norm(expected)
    return result


def _entity_has(entities_by_rel: dict[str, dict], fixture_dir: str,
                rel_path: str, expected: str) -> bool:
    """entity_schema_alignment check: defined_in contains the expected
    class (+ member) / function."""
    if not rel_path:
        return False
    ent = entities_by_rel.get(rel_path)
    if ent is None:
        abs_path = os.path.join(fixture_dir, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                ent = parse_entities(abs_path, f.read())
        except OSError:
            ent = None
        entities_by_rel[rel_path] = ent or {}
    if not ent:
        return False

    if "::" in expected:
        fqcn, member = expected.rsplit("::", 1)
        for cls in ent.get("classes", []):
            if _norm(cls.get("fqcn")) == _norm(fqcn):
                return any(_norm(m.get("name")) == _norm(member)
                           for m in cls.get("methods", []))
        return False
    # bare FQCN (class_ref) or function name
    for cls in ent.get("classes", []):
        if _norm(cls.get("fqcn")) == _norm(expected):
            return True
    fn_simple = expected.rsplit("\\", 1)[-1]
    ns = ent.get("namespace", "")
    for cls in ent.get("classes", []):
        if cls.get("name") != "(global)":
            continue
        for m in cls.get("methods", []):
            if _norm(m.get("name")) != _norm(fn_simple):
                continue
            fq = f"{ns}\\{m['name']}" if ns else m["name"]
            if _norm(fq) == _norm(expected) or _norm(m["name"]) == _norm(expected):
                return True
    return False


def coverage_case(case: dict, fixture_dir: str, data,
                  entities_by_rel: dict[str, dict]) -> bool:
    """Entity-coverage check for one case (all requirements must hold)."""
    expected = case.get("expected", "")
    if expected in ("AMBIGUOUS", "DYNAMIC"):
        return all(_entity_has(entities_by_rel, fixture_dir,
                               c.get("defined_in", ""), c.get("target", ""))
                   for c in case.get("candidates", []))
    if not _entity_has(entities_by_rel, fixture_dir,
                       case.get("defined_in", ""), expected):
        return False
    # Caller side: the case file's enclosing method must list the callee.
    # Use the callee name from `expected` (authoritative) rather than
    # re-parsing the expr, which cannot tell an outer call from a nested one.
    if case.get("kind") in ("method_call", "static_call", "function_call"):
        rel = case.get("file", "")
        abs_fp = os.path.join(fixture_dir, rel)
        ent = data.extracted_entities.get(abs_fp)
        if not ent:
            return False
        callee = expected.rsplit("::", 1)[-1].rsplit("\\", 1)[-1]
        line = case.get("line", 0)
        for cls in ent.get("classes", []):
            for m in cls.get("methods", []):
                if m.get("start_line", 0) <= line <= m.get("end_line", 0):
                    return callee in m.get("calls", [])
        # Top-level code (e.g. templates) has no enclosing method — the entity
        # existence check above already passed, so count it covered.
        return True
    return True


# ═══════════════════════════════════════════════════════════════════
# Wrong-edge measurement
# ═══════════════════════════════════════════════════════════════════


def count_wrong_edges(cases: list[dict], data, built: dict, derived: dict,
                      res_files: dict, fixture_dir: str) -> list[dict]:
    """MethodCalls edges that contradict ground truth (must be zero).

    Edges derive 1:1 from resolution records, so per-site correctness is
    checked on the record that generated (or would generate) the edge:
    a site with a definite `expected` is wrong when its record resolved to
    an internal target other than `expected`; an AMBIGUOUS/DYNAMIC site is
    wrong when its record resolved (i.e. produced a MethodCalls edge) at all.
    """
    internal_rels = {os.path.relpath(fp, data.target_dir)
                     for fp in data.extracted_entities}

    def _made_edge(rec: dict) -> bool:
        tgt = rec.get("target") or {}
        return (rec.get("status") == "resolved"
                and bool(tgt.get("member"))
                and tgt.get("path", "") in internal_rels)

    wrong: list[dict] = []
    for case in cases:
        kind = case.get("kind", "")
        if kind not in ("method_call", "static_call", "function_call",
                        "callable_ref", "property_access", "new_object"):
            continue
        frec = res_files.get(case.get("file", ""))
        if not frec:
            continue
        pos = _expr_position(fixture_dir, case)
        if pos is None:
            continue
        rec = _match_record(case, frec, pos)
        if rec is None:
            continue
        expected = case.get("expected", "")
        # The record actually carrying the edge for callable cases:
        edge_rec_answer = None
        if _made_edge(rec):
            edge_rec_answer = _fq_answer(rec.get("target"))
        ct = rec.get("callable_target")
        if ct and rec.get("callable_internal") and kind == "callable_ref":
            edge_rec_answer = _fq_answer(ct)
        if edge_rec_answer is None:
            continue
        if expected in ("AMBIGUOUS", "DYNAMIC"):
            wrong.append({"id": case.get("id"), "expected": expected,
                          "edge_to": edge_rec_answer})
        elif _norm(edge_rec_answer) != _norm(expected):
            wrong.append({"id": case.get("id"), "expected": expected,
                          "edge_to": edge_rec_answer})
    return wrong


# ═══════════════════════════════════════════════════════════════════
# QA questions
# ═══════════════════════════════════════════════════════════════════


def _build_graph_view(built: dict, derived: dict) -> dict:
    """Small in-memory views of the derived graph, keyed by FQMN/FQCN."""
    methods = built["rows"]["Methods"]
    classes = built["rows"]["Classes"]
    files = built["rows"]["Files"]
    fqmn_by_id = {r[0]: r[4] for r in methods}
    id_by_fqmn = {_norm(r[4]): r[0] for r in methods}
    fqcn_by_id = {r[0]: (r[3] or r[1]) for r in classes}
    id_by_fqcn = {_norm(r[3] or r[1]): r[0] for r in classes}
    path_by_fid = {r[0]: r[4] for r in files}
    fid_by_path = {_norm(r[4]): r[0] for r in files}

    def edges(table, a=1, b=2):
        return [(row[a], row[b]) for row in derived["rows"].get(table, [])]

    class_of_method = {r[0]: r[2] for r in methods}
    methods_of_class: dict[str, list[str]] = {}
    for r in methods:
        methods_of_class.setdefault(r[2], []).append(r[4])
    file_of_class = {r[0]: r[4] for r in classes}

    view = {
        "fqmn_by_id": fqmn_by_id, "id_by_fqmn": id_by_fqmn,
        "fqcn_by_id": fqcn_by_id, "id_by_fqcn": id_by_fqcn,
        "path_by_fid": path_by_fid, "fid_by_path": fid_by_path,
        "methods_of_class": methods_of_class,
        "class_of_method": class_of_method,
        "file_of_class": file_of_class,
        "calls": edges("MethodCalls"),
        "possibly": edges("PossiblyCalls"),
        "inherits": edges("ClassInherits"),
        "imports": edges("FileImports"),
        "maps_to_table": edges("ClassMapsToTable") if "ClassMapsToTable" in derived["rows"] else [],
        "table_refs": derived["rows"].get("TableReferences", []),
        "db_tables": {r[1]: r for r in derived["rows"].get("DbTables", [])} if "DbTables" in derived["rows"] else {},
    }
    return view


def answer_question(q: dict, view: dict) -> tuple[bool, str]:
    """Deterministically answer one QA question against the graph view."""
    check = q.get("check", {})
    ctype = check.get("type", "")
    subject = check.get("subject", "")
    expected = q.get("expected", [])
    match = q.get("match", "contains_all")

    def _final(actual: list[str]) -> tuple[bool, str]:
        act_set = {_norm(a) for a in actual}
        if match == "count_at_least":
            ok = len(actual) >= int(expected if isinstance(expected, int)
                                    else expected[0] if expected else 1)
        elif match == "exact_set" or match == "equals":
            exp_set = {_norm(e) for e in (expected if isinstance(expected, list)
                                          else [expected])}
            ok = act_set == exp_set
        else:  # contains_all
            exp_set = {_norm(e) for e in (expected if isinstance(expected, list)
                                          else [expected])}
            ok = exp_set <= act_set
        return ok, ", ".join(sorted(actual)[:8]) or "(empty)"

    if ctype == "node_exists":
        exists = _norm(subject) in view["id_by_fqmn"] \
            or _norm(subject) in view["id_by_fqcn"]
        return exists, "exists" if exists else "missing"
    if ctype in ("callees_of", "possibly_callees_of"):
        mid = view["id_by_fqmn"].get(_norm(subject), "")
        table = view["calls"] if ctype == "callees_of" else view["possibly"]
        actual = [view["fqmn_by_id"].get(t, "") for c, t in table if c == mid]
        return _final(actual)
    if ctype == "callers_of":
        mid = view["id_by_fqmn"].get(_norm(subject), "")
        actual = [view["fqmn_by_id"].get(c, "") for c, t in view["calls"] if t == mid]
        return _final(actual)
    if ctype == "parents_of":
        cid = view["id_by_fqcn"].get(_norm(subject), "")
        actual = [view["fqcn_by_id"].get(p, "") for c, p in view["inherits"] if c == cid]
        return _final(actual)
    if ctype == "children_of":
        cid = view["id_by_fqcn"].get(_norm(subject), "")
        actual = [view["fqcn_by_id"].get(c, "") for c, p in view["inherits"] if p == cid]
        return _final(actual)
    if ctype == "methods_of":
        cid = view["id_by_fqcn"].get(_norm(subject), "")
        actual = view["methods_of_class"].get(cid, [])
        return _final([a.rsplit("::", 1)[-1] for a in actual])
    if ctype == "class_file":
        cid = view["id_by_fqcn"].get(_norm(subject), "")
        fid = view["file_of_class"].get(cid, "")
        actual = [view["path_by_fid"].get(fid, "")]
        return _final(actual)
    if ctype == "imports_of":
        # imports view stores (source, target) file ids; expected are paths
        fid = view["fid_by_path"].get(_norm(subject), "")
        actual = [view["path_by_fid"].get(t, "")
                  for s, t in view["imports"] if s == fid]
        return _final(actual)
    if ctype == "class_table":
        cid = view["id_by_fqcn"].get(_norm(subject), "")
        actual = [t for c, t in view["maps_to_table"] if c == cid]
        return _final(actual)
    if ctype == "table_columns":
        row = view["db_tables"].get(subject)
        if row is None:
            return False, "(table missing)"
        try:
            cols = [c.get("name", "") for c in json.loads(row[2])]
        except Exception:
            cols = []
        return _final(cols)
    if ctype == "table_fks":
        actual = [f"{r[4]}->{r[2]}" for r in view["table_refs"] if r[1] == subject] \
            if view["table_refs"] and len(view["table_refs"][0]) > 4 else []
        return _final(actual)
    if ctype == "count_at_least":
        table = check.get("table", "MethodCalls")
        n = len(view.get({"MethodCalls": "calls", "PossiblyCalls": "possibly",
                          "ClassInherits": "inherits"}.get(table, "calls"), []))
        return n >= int(check.get("min", 1)), str(n)
    return False, f"(unknown check type {ctype})"


# ═══════════════════════════════════════════════════════════════════
# Fixture runner + report
# ═══════════════════════════════════════════════════════════════════


def evaluate_fixture(fixture: str, repo_root: str, printer=print,
                     dump_edges: bool = False) -> dict:
    from .pipeline import (PipelineData, build_node_rows, derive_edge_rows,
                           phase1_scan, phase1b_treesitter_entities,
                           phase1c_lsp_resolution, phase1d_db_schema)

    fixture_dir = os.path.join(repo_root, "test_codes", fixture)
    gt_path = os.path.join(fixture_dir, "ground_truth.json")
    if not os.path.isfile(gt_path):
        return {"error": f"ground_truth.json not found under {fixture_dir}"}

    if fixture == "php_cakephp" and not os.path.isfile(
            os.path.join(fixture_dir, "vendor", "autoload.php")):
        return {"error": "composer vendor missing",
                "hint": f"cd {fixture_dir} && composer install"}

    with open(gt_path, "r", encoding="utf-8") as f:
        gt = json.load(f)
    cases = gt.get("cases", [])

    # Isolate checkpoints per fixture under the configured OUTPUT_DIR
    orig_output = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(orig_output, f"eval_{fixture}")
    try:
        data = PipelineData(target_dir=fixture_dir)
        phase1_scan(data)
        phase1b_treesitter_entities(data)
        phase1c_lsp_resolution(data)
        phase1d_db_schema(data)
        built = build_node_rows(data)
        data.file_id_map = built["file_id_map"]
        data.class_id_map = built["class_id_map"]
        data.method_id_map = built["method_id_map"]
        data.module_id_map = built["module_id_map"]
        data.dir_id_map = built["dir_id_map"]
        data.dbtable_id_map = built["dbtable_id_map"]
        derived = derive_edge_rows(data)
    finally:
        config.OUTPUT_DIR = orig_output

    res_files = data.resolutions.get("files", {})
    entities_by_rel: dict[str, dict] = {}

    # 1) GT completeness
    case_results = []
    for case in cases:
        frec = res_files.get(case.get("file", ""))
        case_results.append(score_case(case, frec, fixture_dir, entities_by_rel))
    correct = sum(1 for r in case_results if r["correct"])
    completeness = 100.0 * correct / len(cases) if cases else 100.0

    # 2) Entity coverage
    cov_flags = [coverage_case(c, fixture_dir, data, entities_by_rel)
                 for c in cases]
    coverage = 100.0 * sum(cov_flags) / len(cases) if cases else 100.0
    parse_rate = (100.0 * len(data.extracted_entities) / len(data.file_list)
                  if data.file_list else 100.0)

    # 3) Wrong edges
    wrong = count_wrong_edges(cases, data, built, derived, res_files, fixture_dir)

    # 4) QA questions (optional file)
    qa_path = os.path.join(fixture_dir, "qa_questions.json")
    qa_results = []
    if os.path.isfile(qa_path):
        with open(qa_path, "r", encoding="utf-8") as f:
            qa = json.load(f)
        view = _build_graph_view(built, derived)
        for q in qa.get("questions", []):
            ok, actual = answer_question(q, view)
            qa_results.append({"id": q.get("id"), "question": q.get("question"),
                               "ok": ok, "actual": actual})
    qa_pct = (100.0 * sum(1 for r in qa_results if r["ok"]) / len(qa_results)
              if qa_results else None)

    report = {
        "fixture": fixture,
        "cases": len(cases),
        "completeness": completeness,
        "coverage": coverage,
        "parse_rate": parse_rate,
        "wrong_edges": wrong,
        "qa_pct": qa_pct,
        "qa_total": len(qa_results),
        "case_results": case_results,
        "qa_results": qa_results,
        "edge_counts": {t: len(r) for t, r in derived["rows"].items() if r},
        "resolution_stats": derived["stats"],
        "engine": data.resolutions.get("engine", "?"),
    }
    if dump_edges:
        report["edges"] = {t: derived["rows"][t] for t in ("MethodCalls",
                                                           "PossiblyCalls",
                                                           "ClassInherits")}
    return report


def print_report(report: dict, printer=print, verbose: bool = True) -> bool:
    """Pretty-print one fixture report; returns pass/fail."""
    if "error" in report:
        printer(f"  ERROR: {report['error']}")
        if "hint" in report:
            printer(f"  HINT:  {report['hint']}")
        return False

    fx = report["fixture"]
    printer("=" * 70)
    printer(f"  EVALUATION — {fx}  (engine: intelephense {report['engine']})")
    printer("=" * 70)
    printer(f"  Ground-truth completeness : {report['completeness']:6.1f}%  "
            f"({sum(1 for r in report['case_results'] if r['correct'])}/{report['cases']})"
            f"   [target ≥ {THRESHOLD:.0f}%]")
    printer(f"  Entity coverage           : {report['coverage']:6.1f}%  "
            f"  [target ≥ {THRESHOLD:.0f}%]   (parse success {report['parse_rate']:.1f}%)")
    if report["qa_pct"] is not None:
        printer(f"  QA completeness           : {report['qa_pct']:6.1f}%  "
                f"({sum(1 for r in report['qa_results'] if r['ok'])}/{report['qa_total']})"
                f"   [target ≥ {THRESHOLD:.0f}%]")
    else:
        printer("  QA completeness           :   n/a   (no qa_questions.json)")
    printer(f"  Wrong edges               : {len(report['wrong_edges'])}        [target = 0]")
    printer(f"  Edges: {report['edge_counts']}")
    printer(f"  Resolution: {report['resolution_stats']}")

    # per-category breakdown
    by_cat: dict[str, list[dict]] = {}
    for r in report["case_results"]:
        by_cat.setdefault(r["category"], []).append(r)
    printer("  ── per category ──")
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        n_ok = sum(1 for r in rs if r["correct"])
        printer(f"    {cat:<24} {n_ok}/{len(rs)}")
    misses = [r for r in report["case_results"] if not r["correct"]]
    if misses and verbose:
        printer("  ── misses ──")
        for r in misses:
            printer(f"    {r['id']:<8} expected={r['expected']}")
            printer(f"             got     ={r['answer'] or '(none)'}  {r['note']}")
    for w in report["wrong_edges"]:
        printer(f"  WRONG EDGE {w['id']}: expected {w['expected']} but edge → {w['edge_to']}")
    qa_misses = [r for r in report.get("qa_results", []) if not r["ok"]]
    if qa_misses and verbose:
        printer("  ── QA misses ──")
        for r in qa_misses:
            printer(f"    {r['id']}: {r['question']}")
            printer(f"       actual: {r['actual']}")

    ok = (report["completeness"] >= THRESHOLD
          and report["coverage"] >= THRESHOLD
          and (report["qa_pct"] is None or report["qa_pct"] >= THRESHOLD)
          and not report["wrong_edges"])
    printer(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def run_evaluation(fixtures: list[str], repo_root: str, printer=print,
                   dump_edges: bool = False) -> int:
    """Evaluate fixtures; return process exit code (0 pass / 1 fail / 2 env)."""
    all_ok = True
    for fixture in fixtures:
        report = evaluate_fixture(fixture, repo_root, printer, dump_edges)
        if "error" in report:
            print_report(report, printer)
            return 2
        if not print_report(report, printer):
            all_ok = False
    return 0 if all_ok else 1
