#!/usr/bin/env python3
"""Validate the PHP fixture ground-truth manifests against the fixture files.

Checks, per fixture (php_plain, php_cakephp):
  a. schema: required fields, enum values, candidates iff AMBIGUOUS/DYNAMIC/interface
  b. every `file` and every `defined_in` (case-level and candidate-level) exists
  c. `expr` occurs in `file` and its `occurrence`-th match starts on `line`
  d. PSR-4 truthfulness: namespace declarations match composer.json psr-4 maps
  e. per-category case counts

Deliberately standalone: no imports from graph_generator — fixtures are validated
on their own terms, independent of how the pipeline currently scans or parses.

Exit code 0 = all green, 1 = any failure. Stdlib only.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

KINDS = {
    "method_call", "static_call", "function_call", "new_object",
    "property_access", "class_ref", "callable_ref", "callback_definition",
}
LOCATIONS = {"app", "plugin", "vendor", "mixed", "none"}

FAILURES = []


def fail(fixture, case_id, message):
    FAILURES.append(f"[{fixture}] {case_id}: {message}")


def expr_line(root, relpath, expr, occurrence):
    """Return the 1-based line of the occurrence-th match of expr, or None."""
    path = os.path.join(root, relpath)
    seen = 0
    with open(path, encoding="utf-8") as handle:
        for lineno, text in enumerate(handle, 1):
            seen += text.count(expr)
            if text.count(expr) and seen >= occurrence:
                return lineno
    return None


def psr4_maps(root, include_vendor_manifests):
    """Collect psr-4 maps and files-autoload exemptions from authored composer.json manifests.

    Vendor package manifests are only trusted for php_plain, whose vendor tree is hand-authored;
    the real composer vendor tree in php_cakephp is not ours to lint.
    """
    maps = []
    exempt = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "composer" and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        if not include_vendor_manifests and (rel_dir == "vendor" or rel_dir.startswith("vendor/")):
            dirnames[:] = []
            continue
        if "composer.json" not in filenames:
            continue
        base = "" if rel_dir == "." else rel_dir + "/"
        try:
            manifest = json.load(open(os.path.join(dirpath, "composer.json"), encoding="utf-8"))
        except Exception:
            continue
        autoload = manifest.get("autoload", {})
        for prefix, target in autoload.get("psr-4", {}).items():
            targets = target if isinstance(target, list) else [target]
            for one in targets:
                maps.append((prefix, base + one))
        for f in autoload.get("files", []):
            exempt.add(base + f)
    return maps, exempt


def file_namespace(path):
    with open(path, encoding="utf-8") as handle:
        for text in handle:
            stripped = text.strip()
            if stripped.startswith("namespace ") and stripped.endswith(";"):
                return stripped[len("namespace "):-1].strip()
    return None


def check_psr4(fixture, root):
    maps, exempt = psr4_maps(root, include_vendor_manifests=fixture == "php_plain")
    checked = 0
    for prefix, target in maps:
        target_dir = os.path.join(root, target)
        if not os.path.isdir(target_dir):
            fail(fixture, "psr4", f"psr-4 target missing: {target}")
            continue
        for dirpath, dirnames, filenames in os.walk(target_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.endswith(".php"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if rel in exempt or rel.startswith("vendor/composer/"):
                    continue
                sub = os.path.relpath(dirpath, target_dir)
                expected_ns = prefix.rstrip("\\") + ("" if sub == "." else "\\" + sub.replace("/", "\\"))
                actual_ns = file_namespace(os.path.join(root, rel))
                if actual_ns != expected_ns:
                    fail(fixture, "psr4", f"{rel}: namespace {actual_ns!r} != psr-4 expected {expected_ns!r}")
                checked += 1
    return checked


def validate_fixture(name):
    root = os.path.join(HERE, name)
    gt_path = os.path.join(root, "ground_truth.json")
    if not os.path.isfile(gt_path):
        fail(name, "-", "ground_truth.json missing")
        return

    data = json.load(open(gt_path, encoding="utf-8"))
    cases = data.get("cases", [])
    categories = {}
    seen_ids = set()

    for case in cases:
        cid = case.get("id", "<no-id>")
        if cid in seen_ids:
            fail(name, cid, "duplicate id")
        seen_ids.add(cid)

        for field in ("id", "category", "file", "line", "expr", "kind", "expected", "answer_location", "why_hard"):
            if field not in case:
                fail(name, cid, f"missing field {field}")
        if case.get("kind") not in KINDS:
            fail(name, cid, f"bad kind {case.get('kind')!r}")
        if case.get("answer_location") not in LOCATIONS:
            fail(name, cid, f"bad answer_location {case.get('answer_location')!r}")

        expected = case.get("expected", "")
        if expected in ("AMBIGUOUS", "DYNAMIC"):
            if not case.get("candidates"):
                fail(name, cid, f"{expected} requires candidates")
            if "defined_in" in case:
                fail(name, cid, f"{expected} must not carry defined_in")
        else:
            if "defined_in" not in case:
                fail(name, cid, "resolved case missing defined_in")
        for cand in case.get("candidates", []):
            if "target" not in cand or "defined_in" not in cand:
                fail(name, cid, "candidate missing target/defined_in")

        relfile = case.get("file", "")
        if not os.path.isfile(os.path.join(root, relfile)):
            fail(name, cid, f"file not found: {relfile}")
            continue

        for ref in [case.get("defined_in")] + [c.get("defined_in") for c in case.get("candidates", [])]:
            if ref and not os.path.isfile(os.path.join(root, ref)):
                fail(name, cid, f"defined_in not found: {ref}")

        occurrence = case.get("occurrence", 1)
        actual = expr_line(root, relfile, case.get("expr", ""), occurrence)
        if actual is None:
            fail(name, cid, f"expr not found (occurrence {occurrence}): {case.get('expr')!r}")
        elif actual != case.get("line"):
            fail(name, cid, f"expr occurrence {occurrence} is on line {actual}, ground truth says {case.get('line')}")

        categories[case.get("category", "?")] = categories.get(case.get("category", "?"), 0) + 1

    psr4_checked = check_psr4(name, root)
    print(f"{name}: {len(cases)} cases, {len(categories)} categories, {psr4_checked} files psr-4 checked")
    for cat in sorted(categories):
        print(f"    {cat:<24} {categories[cat]}")


def main():
    for fixture in ("php_plain", "php_cakephp"):
        validate_fixture(fixture)
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for line in FAILURES:
            print("  " + line)
        return 1
    print("\nAll ground-truth checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
