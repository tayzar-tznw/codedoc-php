"""Verify cross-repo derivation against the committed test_codes/multi_repo/
fixtures + cross_ground_truth.json (framework-free, no GCP).

Unlike test_crossref.py (synthetic tmp repos for the logic), this locks the
feature's behavior against realistic, committed, human-reviewable projects.
"""

import json
import os

from graph_generator import config, crossref
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities, _make_id,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "test_codes", "multi_repo")


def _ingest(repo, tmp_path):
    root = os.path.join(FIXTURE, repo)
    orig = config.OUTPUT_DIR
    config.OUTPUT_DIR = str(tmp_path / repo)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    try:
        d = PipelineData(target_dir=root)
        d.repo = repo
        phase1_scan(d)
        phase1b_treesitter_entities(d)
    finally:
        config.OUTPUT_DIR = orig
    return {"repo": repo, "target_dir": root, "entities": d.extracted_entities}


def _ground_truth():
    with open(os.path.join(FIXTURE, "cross_ground_truth.json"), encoding="utf-8") as f:
        return json.load(f)


def _derive(tmp_path):
    gt = _ground_truth()
    repos = [_ingest(r["name"], tmp_path) for r in gt["repos"]]
    reg = crossref.build_registry(repos)
    return gt, crossref.derive_cross_repo(repos, reg, printer=lambda *a: None)


def test_cross_repo_refs_match_ground_truth_exactly(tmp_path):
    gt, derived = _derive(tmp_path)
    got = {(r[3], r[4], r[5], r[6]) for r in derived["CrossRepoRef"]}
    want = {(c["source_repo"], c["target_repo"], c["target_fqcn"], c["kind"])
            for c in gt["cross_repo_refs"]}
    assert got == want, f"\nmissing={want - got}\nunexpected={got - want}"


def test_cross_repo_ref_row_count_matches(tmp_path):
    gt, derived = _derive(tmp_path)
    assert len(derived["CrossRepoRef"]) == gt["cross_repo_ref_row_count"]


def test_cross_repo_file_refs_match_ground_truth_exactly(tmp_path):
    """Classless files (bootstrap wiring) surface as File→Class edges."""
    gt, derived = _derive(tmp_path)
    got = {(r[3], r[4], r[5], r[6]) for r in derived["CrossRepoFileRef"]}
    want = {(c["source_repo"], c["target_repo"], c["target_fqcn"], c["kind"])
            for c in gt["cross_repo_file_refs"]}
    assert got == want, f"\nmissing={want - got}\nunexpected={got - want}"
    boot_fid = _make_id("file", "web", "bootstrap.php")
    assert all(r[1] == boot_fid for r in derived["CrossRepoFileRef"])


def test_cross_repo_calls_match_ground_truth_exactly(tmp_path):
    gt, derived = _derive(tmp_path)
    got = {(c[3], c[4], c[5]) for c in derived["CrossRepoCalls"]}
    want = {(c["source_repo"], c["target_repo"], c["target_fqmn"])
            for c in gt["cross_repo_calls"]}
    assert got == want, f"\nmissing={want - got}\nunexpected={got - want}"


def test_cross_repo_di_edges_match_ground_truth(tmp_path):
    gt, derived = _derive(tmp_path)
    got_binds = {(b[3], b[4], b[5]) for b in derived["DiBinds"]}
    want_binds = {(b["source_repo"], b["target_repo"], b["target_fqcn"])
                  for b in gt["cross_repo_binds"]}
    assert got_binds == want_binds, f"\nmissing={want_binds - got_binds}\nunexpected={got_binds - want_binds}"

    got_inj = {(i[3], i[4], i[5]) for i in derived["DiInjects"]}
    want_inj = {(i["source_repo"], i["target_repo"], i["target_fqcn"])
                for i in gt["cross_repo_injects"]}
    assert got_inj == want_inj, f"\nmissing={want_inj - got_inj}\nunexpected={got_inj - want_inj}"


def test_coupling_matrix_matches(tmp_path):
    gt, derived = _derive(tmp_path)
    for pair, exp in gt["coupling"].items():
        src, tgt = pair.split("->")
        actual = derived["coupling"].get((src, tgt), {"refs": 0, "calls": 0, "di": 0})
        assert actual["refs"] == exp["refs"], f"{pair} refs {actual} != {exp}"
        assert actual["calls"] == exp["calls"], f"{pair} calls {actual} != {exp}"
        assert actual.get("di", 0) == exp["di"], f"{pair} di {actual} != {exp}"


def test_no_edge_crosses_or_stays_within_wrong_repo(tmp_path):
    _, derived = _derive(tmp_path)
    for row in (derived["CrossRepoRef"] + derived["CrossRepoFileRef"]
                + derived["CrossRepoCalls"]):
        assert row[3] != row[4]  # source_repo != target_repo


def test_drops_match_ground_truth(tmp_path):
    """Everything not edged is REPORTED — ambiguous FQCNs and unowned refs."""
    gt, derived = _derive(tmp_path)
    assert derived["drops"]["ambiguous"] == gt["expected_drops"]["ambiguous"]
    assert (derived["drops"]["unowned_sample"]
            == gt["expected_drops"]["unowned_sample"])


def test_chained_receiver_call_is_captured(tmp_path):
    """$sum = $base->add($tax); $sum->amount() — the chained receiver is typed
    via add()'s declared return type, so Price::amount gets a call edge."""
    _, derived = _derive(tmp_path)
    total = _make_id("method", "web", "src/Order/OrderService.php",
                     "Web\\Order\\OrderService", "total")
    amount = _make_id("method", "shared", "src/Money/Price.php",
                      "Shared\\Money\\Price", "amount")
    assert any(c[1] == total and c[2] == amount
               for c in derived["CrossRepoCalls"])


def test_namespaced_function_call_edge_endpoints(tmp_path):
    """web_boot() (classless bootstrap) → Shared\\Util\\money_round, keyed by
    the same (global) pseudo-class IDs Phase 8 writes."""
    _, derived = _derive(tmp_path)
    web_boot = _make_id("method", "web", "bootstrap.php", "(global)", "web_boot")
    money_round = _make_id("method", "shared", "src/functions.php",
                           "(global)", "money_round")
    assert any(c[1] == web_boot and c[2] == money_round
               for c in derived["CrossRepoCalls"])


def test_endpoint_node_ids_are_deterministic(tmp_path):
    """Edge endpoints equal the node IDs Phase 8 would write for those repos."""
    _, derived = _derive(tmp_path)
    web_order = _make_id("class", "web", "src/Order/OrderService.php",
                         "Web\\Order\\OrderService")
    shared_price = _make_id("class", "shared", "src/Money/Price.php",
                            "Shared\\Money\\Price")
    assert any(r[1] == web_order and r[2] == shared_price
               for r in derived["CrossRepoRef"]), "OrderService→Price edge id mismatch"


def test_same_simple_name_produces_no_cross_edge(tmp_path):
    """Web\\Support\\Logger (local) shares the name 'Logger' with
    Shared\\Logging\\Logger but must not create a cross-repo edge."""
    _, derived = _derive(tmp_path)
    # no CrossRepoCalls should originate from LocalReporter
    reporter = _make_id("method", "web", "src/Support/LocalReporter.php",
                        "Web\\Support\\LocalReporter", "report")
    assert all(c[1] != reporter for c in derived["CrossRepoCalls"])


def test_dynamic_receiver_produces_no_cross_edge(tmp_path):
    _, derived = _derive(tmp_path)
    dispatcher = _make_id("method", "web", "src/Order/DynamicDispatcher.php",
                          "Web\\Order\\DynamicDispatcher", "run")
    assert all(c[1] != dispatcher for c in derived["CrossRepoCalls"])
