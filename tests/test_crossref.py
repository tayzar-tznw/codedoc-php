"""Cross-repo dependency derivation: real detection, no false cross-repo edges."""

import os

from graph_generator import config, crossref
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities, _make_id,
)


def _ingest(tmp_path, repo, files: dict[str, str]) -> dict:
    """Write a tiny repo, scan+parse it, and return a discover_repos-style dict.

    OUTPUT_DIR is isolated to a fresh per-repo dir so phase1_scan's checkpoint
    load doesn't pull in the real project's entities."""
    root = tmp_path / repo
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    orig = config.OUTPUT_DIR
    config.OUTPUT_DIR = str(tmp_path / "out" / repo)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    try:
        data = PipelineData(target_dir=str(root))
        data.repo = repo
        phase1_scan(data)
        phase1b_treesitter_entities(data)
    finally:
        config.OUTPUT_DIR = orig
    return {"repo": repo, "target_dir": str(root), "entities": data.extracted_entities}


SHARED = {
    "src/Money/Price.php": (
        "<?php\nnamespace Shared\\Money;\n"
        "class Price {\n"
        "    public function add(Price $other): Price { return $this; }\n"
        "    public function amount(): int { return 0; }\n"
        "}\n"
    ),
}

WEB = {
    "src/Web/OrderService.php": (
        "<?php\nnamespace App\\Web;\n"
        "use Shared\\Money\\Price;\n"
        "class OrderService {\n"
        "    public function total(Price $price): int {\n"
        "        $price->add($price);\n"
        "        return $price->amount();\n"
        "    }\n"
        "}\n"
    ),
}

# a repo that references nothing cross-repo
SOLO = {
    "src/Solo/Thing.php": (
        "<?php\nnamespace Solo;\nclass Thing { public function go(): void {} }\n"
    ),
}


def test_cross_repo_ref_and_call_detected(tmp_path):
    shared = _ingest(tmp_path, "shared", SHARED)
    web = _ingest(tmp_path, "web", WEB)
    reg = crossref.build_registry([shared, web])
    derived = crossref.derive_cross_repo([shared, web], reg, printer=lambda *a: None)

    # row = [edge_id, source_class, target_class, source_repo, target_repo, symbol, kind]
    refs = derived["CrossRepoRef"]
    assert any(r[3] == "web" and r[4] == "shared" and r[5] == "Shared\\Money\\Price"
               for r in refs), refs
    # endpoints are the deterministic node IDs of the two repos
    web_cls = _make_id("class", "web", "src/Web/OrderService.php", "App\\Web\\OrderService")
    shared_cls = _make_id("class", "shared", "src/Money/Price.php", "Shared\\Money\\Price")
    assert any(r[1] == web_cls and r[2] == shared_cls for r in refs)

    # method-call level: web calls Shared\Money\Price::add (typed param receiver)
    # row = [edge_id, caller_method, callee_method, source_repo, target_repo, symbol]
    calls = derived["CrossRepoCalls"]
    assert any(c[3] == "web" and c[4] == "shared"
               and c[5] == "Shared\\Money\\Price::add" for c in calls), calls

    # coupling matrix records web → shared
    assert derived["coupling"].get(("web", "shared"), {}).get("refs", 0) >= 1
    assert derived["coupling"].get(("web", "shared"), {}).get("calls", 0) >= 1


def test_no_edge_within_a_single_repo(tmp_path):
    shared = _ingest(tmp_path, "shared", SHARED)
    web = _ingest(tmp_path, "web", WEB)
    reg = crossref.build_registry([shared, web])
    derived = crossref.derive_cross_repo([shared, web], reg, printer=lambda *a: None)
    # every edge must connect two DIFFERENT repos (source_repo=r[3], target=r[4])
    for r in derived["CrossRepoRef"]:
        assert r[3] != r[4]
    for c in derived["CrossRepoCalls"]:
        assert c[3] != c[4]


def test_unrelated_repo_produces_no_cross_edges(tmp_path):
    shared = _ingest(tmp_path, "shared", SHARED)
    solo = _ingest(tmp_path, "solo", SOLO)
    reg = crossref.build_registry([shared, solo])
    derived = crossref.derive_cross_repo([shared, solo], reg, printer=lambda *a: None)
    assert derived["CrossRepoRef"] == []
    assert derived["CrossRepoCalls"] == []
    assert derived["coupling"] == {}


def test_same_fqcn_in_two_repos_is_ambiguous_skipped(tmp_path):
    """If two repos define the same FQCN, a reference to it is ambiguous → no
    edge (never guessed)."""
    a = _ingest(tmp_path, "a", SHARED)
    b = _ingest(tmp_path, "b", SHARED)  # same Shared\Money\Price
    consumer = _ingest(tmp_path, "c", WEB)  # references Shared\Money\Price
    reg = crossref.build_registry([a, b, consumer])
    derived = crossref.derive_cross_repo([a, b, consumer], reg, printer=lambda *a: None)
    # the reference target is defined in both a and b → ambiguous, skipped
    # (no CrossRepoRef should point at the ambiguous Shared\Money\Price)
    assert all(r[5] != "Shared\\Money\\Price" for r in derived["CrossRepoRef"])
    assert derived["stats"]["ambiguous_targets"] >= 1


def test_format_coupling_matrix():
    assert "no cross-repo" in crossref.format_coupling_matrix({})
    out = crossref.format_coupling_matrix({("web", "shared"): {"refs": 3, "calls": 7}})
    assert "web → shared" in out and "3 / 7" in out


def test_run_crossref_needs_two_repos(tmp_path, monkeypatch):
    from graph_generator import config
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(crossref, "discover_repos", lambda *a, **k: [{"repo": "solo"}])
    derived = crossref.run_crossref(printer=lambda *a: None, write=True)
    assert derived["CrossRepoRef"] == [] and derived["coupling"] == {}


def test_run_crossref_writes_edges(tmp_path, monkeypatch):
    from graph_generator import config, pipeline
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    shared = _ingest(tmp_path, "shared", SHARED)
    web = _ingest(tmp_path, "web", WEB)
    monkeypatch.setattr(crossref, "discover_repos", lambda *a, **k: [shared, web])

    from tests.conftest import FakeSpannerDb
    db = FakeSpannerDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    derived = crossref.run_crossref(printer=lambda *a: None, write=True)
    assert derived["CrossRepoRef"] and derived["CrossRepoCalls"]
    # edges were batch-written to the fake Spanner
    assert db.mutations.get("CrossRepoRef")
    assert db.mutations.get("CrossRepoCalls")


def test_discover_repos_reads_meta(tmp_path, monkeypatch):
    from graph_generator import config, pipeline
    import json
    # simulate two ingested repos on disk under OUTPUT_DIR/repos/<r>/
    out = tmp_path / "out"
    for repo in ("web", "shared"):
        d = out / "repos" / repo
        d.mkdir(parents=True)
        (d / "graph_meta.json").write_text(json.dumps(
            {"repo": repo, "target_dir": f"/src/{repo}",
             "id_scheme": pipeline.ID_SCHEME, "id_prefix": config.ID_PREFIX}))
        (d / "entities.json").write_text(json.dumps(
            {"version": __import__("graph_generator.treesitter_parser",
                                   fromlist=["ENTITIES_VERSION"]).ENTITIES_VERSION,
             "entities": {}}))
    repos = crossref.discover_repos(str(out), printer=lambda *a: None)
    assert {r["repo"] for r in repos} == {"web", "shared"}
