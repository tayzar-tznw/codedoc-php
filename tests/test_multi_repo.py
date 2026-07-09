"""Multi-repository isolation: same names across repos never merge, and no
edge ever crosses a repo boundary."""

import os

from graph_generator import config
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities, phase1d_db_schema,
    build_node_rows, derive_edge_rows, _make_id,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(REPO_ROOT, "test_codes", "php_plain")


def test_make_id_repo_scoped():
    a = _make_id("method", "repoA", "src/X.php", "App\\X", "run")
    b = _make_id("method", "repoB", "src/X.php", "App\\X", "run")
    assert a != b  # identical path+fqcn+member, different repo → different node


def _built_for_repo(repo, out_dir):
    """Scan php_plain once, then build nodes/edges tagged as `repo`."""
    orig = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(out_dir, repo)
    try:
        data = PipelineData(target_dir=PLAIN)
        data.repo = repo
        phase1_scan(data)
        phase1b_treesitter_entities(data)
        # resolution not required for the identity proof; skip LSP for speed
        data.resolutions = {"engine": "none", "files": {}}
        phase1d_db_schema(data)
        built = build_node_rows(data)
        for k in ("file_id_map", "class_id_map", "method_id_map",
                  "module_id_map", "dir_id_map", "dbtable_id_map"):
            setattr(data, k, built[k])
        derived = derive_edge_rows(data)
        return data, built, derived
    finally:
        config.OUTPUT_DIR = orig


def _node_ids(built):
    ids = set()
    for table_rows in built["rows"].values():
        for row in table_rows:
            ids.add(row[0])  # PK is first column of every node row
    return ids


def test_same_repo_twice_yields_disjoint_node_ids(tmp_path):
    """Two repos built from IDENTICAL source (worst case: every relpath+FQCN
    matches) must produce completely disjoint node-ID sets."""
    _, built_a, _ = _built_for_repo("shopweb", str(tmp_path))
    _, built_b, _ = _built_for_repo("shopapi", str(tmp_path))
    ids_a = _node_ids(built_a)
    ids_b = _node_ids(built_b)
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b), "same-named symbols merged across repos!"
    # and each row carries its repo
    assert all(r[-1] == "shopweb" for r in built_a["rows"]["Classes"])
    assert all(r[-1] == "shopapi" for r in built_b["rows"]["Classes"])


def test_no_edge_crosses_repo_boundary(tmp_path):
    _, built_a, derived_a = _built_for_repo("shopweb", str(tmp_path))
    _, built_b, _ = _built_for_repo("shopapi", str(tmp_path))
    ids_a = _node_ids(built_a)
    ids_b = _node_ids(built_b)

    # Every edge endpoint in repo A's derivation belongs to repo A, never B.
    for table, table_rows in derived_a["rows"].items():
        for row in table_rows:
            endpoints = row[1:3]  # (source, target) for every edge table
            for ep in endpoints:
                assert ep not in ids_b, f"{table} edge crossed into shopapi"
                assert ep in ids_a, f"{table} edge endpoint not in shopweb"


def test_repo_ids_differ_from_default_repo(tmp_path):
    """A class ingested under an explicit repo name has a different ID than the
    same class ingested under the default (basename) repo."""
    _, built_named, _ = _built_for_repo("custom", str(tmp_path))
    # default run (repo defaults to basename 'php_plain')
    orig = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(str(tmp_path), "default")
    try:
        d = PipelineData(target_dir=PLAIN)
        phase1_scan(d)
        phase1b_treesitter_entities(d)
        assert d.repo == "php_plain"
        built_default = build_node_rows(d)
    finally:
        config.OUTPUT_DIR = orig
    assert _node_ids(built_named).isdisjoint(_node_ids(built_default))
