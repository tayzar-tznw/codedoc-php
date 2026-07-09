"""Phase 8/9/10 writers + pure node/edge derivation + checkpoints.

The pure builders (build_node_rows / derive_edge_rows) are asserted directly
for the zero-wrong-edge invariants; the writers run against FakeSpannerDb and
fake vertexai so no GCP is touched.
"""

import json
import os

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import (
    PipelineData, build_node_rows, derive_edge_rows,
    phase8_write_nodes, phase9_write_edges, phase10_generate_embeddings,
    _save_graph_checkpoint, _load_graph_checkpoint, ID_SCHEME,
)


def _data_with_two_files(tmp_path):
    """Two files defining classes A and B where A::run calls B::help,
    resolved (internal) — the canonical single-edge case."""
    target = str(tmp_path / "proj")
    os.makedirs(target)
    data = PipelineData(target_dir=target)
    a = os.path.join(target, "A.php")
    b = os.path.join(target, "B.php")
    data.file_list = [a, b]
    data.dir_tree = {target: {"files": [a, b], "subdirs": []}}
    data.dir_queue = [target]
    data.file_origins = {"A.php": "app", "B.php": "app"}
    data.extracted_entities = {
        a: {"file_path": a, "namespace": "App", "uses": [],
            "classes": [{"name": "A", "kind": "class", "fqcn": "App\\A",
                         "start_line": 1, "end_line": 10, "base_classes": [],
                         "interfaces": [], "heritage": [],
                         "methods": [{"name": "run", "member_kind": "method",
                                      "modifiers": "public", "return_type": "void",
                                      "parameters": "", "start_line": 3, "end_line": 5,
                                      "param_types": {}, "calls": ["help"],
                                      "call_sites": [], "prop_sites": [],
                                      "class_refs": [], "assignments": []}]}]},
        b: {"file_path": b, "namespace": "App", "uses": [],
            "classes": [{"name": "B", "kind": "class", "fqcn": "App\\B",
                         "start_line": 1, "end_line": 8, "base_classes": [],
                         "interfaces": [], "heritage": [],
                         "methods": [{"name": "help", "member_kind": "method",
                                      "modifiers": "public", "return_type": "void",
                                      "parameters": "", "start_line": 3, "end_line": 5,
                                      "param_types": {}, "calls": [],
                                      "call_sites": [], "prop_sites": [],
                                      "class_refs": [], "assignments": []}]}]},
    }
    # A::run resolves to B::help
    data.resolutions = {"engine": "test", "files": {
        "A.php": {"mtime": 0, "inherits": [], "imports": [], "props": [],
                  "class_refs": [], "calls": [{
                      "site": {"class": "A", "class_fqcn": "App\\A", "method": "run",
                               "name": "help", "line": 4, "col": 8, "kind": "method"},
                      "receiver": "$b", "status": "resolved", "via": "lsp",
                      "target": {"fqcn": "App\\B", "member": "help", "path": "B.php",
                                 "line": 3, "member_kind": "method"},
                      "candidates": []}]},
        "B.php": {"mtime": 0, "inherits": [], "imports": [], "props": [],
                  "class_refs": [], "calls": []},
    }}
    return data, a, b


def test_build_node_rows_ids_are_relative_and_fqcn(tmp_path):
    data, a, b = _data_with_two_files(tmp_path)
    built = build_node_rows(data)
    # one Class row per class, carrying namespace + fqcn
    classes = {r[3]: r for r in built["rows"]["Classes"]}  # keyed by fqcn
    assert "App\\A" in classes and "App\\B" in classes
    # Methods carry fqmn
    fqmns = {r[4] for r in built["rows"]["Methods"]}
    assert "App\\A::run" in fqmns and "App\\B::help" in fqmns
    # IDs are stable across runs (relative-path hashing)
    built2 = build_node_rows(data)
    assert built["file_id_map"] == built2["file_id_map"]


def test_derive_edges_only_resolved_become_method_calls(tmp_path):
    data, a, b = _data_with_two_files(tmp_path)
    built = build_node_rows(data)
    for k in ("file_id_map", "class_id_map", "method_id_map",
              "module_id_map", "dir_id_map", "dbtable_id_map"):
        setattr(data, k, built[k])
    derived = derive_edge_rows(data)
    mc = derived["rows"]["MethodCalls"]
    assert len(mc) == 1
    caller_id = data.method_id_map[f"{a}|A|run"]
    callee_id = data.method_id_map[f"{b}|B|help"]
    assert mc[0][1] == caller_id and mc[0][2] == callee_id
    assert mc[0][4] == "lsp"  # resolution provenance
    # structural edges present
    assert derived["rows"]["FileDefinesClass"]
    assert derived["rows"]["ClassDefinesMethod"]


def test_derive_edges_unresolved_goes_to_possibly_calls(tmp_path):
    data, a, b = _data_with_two_files(tmp_path)
    # flip A::run's call to unresolved, name 'help' (B::help exists → 1 candidate)
    data.resolutions["files"]["A.php"]["calls"][0].update(
        {"status": "unresolved", "via": "none", "target": None})
    built = build_node_rows(data)
    for k in ("file_id_map", "class_id_map", "method_id_map",
              "module_id_map", "dir_id_map", "dbtable_id_map"):
        setattr(data, k, built[k])
    derived = derive_edge_rows(data)
    assert derived["rows"]["MethodCalls"] == []
    pc = derived["rows"]["PossiblyCalls"]
    assert len(pc) == 1 and pc[0][4] == "name-heuristic"


def test_derive_edges_ambiguous_capped(tmp_path, monkeypatch):
    data, a, b = _data_with_two_files(tmp_path)
    # ambiguous with 2 candidates but cap = 1 → skipped, no edges
    monkeypatch.setattr(config, "POSSIBLY_CALLS_MAX_CANDIDATES", 1)
    data.resolutions["files"]["A.php"]["calls"][0].update({
        "status": "ambiguous", "via": "lsp", "target": None,
        "candidates": [
            {"fqcn": "App\\B", "member": "help", "path": "B.php"},
            {"fqcn": "App\\A", "member": "run", "path": "A.php"}],
    })
    built = build_node_rows(data)
    for k in ("file_id_map", "class_id_map", "method_id_map",
              "module_id_map", "dir_id_map", "dbtable_id_map"):
        setattr(data, k, built[k])
    derived = derive_edge_rows(data)
    assert derived["rows"]["MethodCalls"] == []
    assert derived["rows"]["PossiblyCalls"] == []  # over the cap → dropped


def test_derive_edges_external_no_edge(tmp_path):
    data, a, b = _data_with_two_files(tmp_path)
    data.resolutions["files"]["A.php"]["calls"][0].update({
        "status": "external", "via": "lsp",
        "target": {"fqcn": "Cake\\ORM\\Table", "member": "get",
                   "path": "vendor/cakephp/x.php", "member_kind": "method"}})
    built = build_node_rows(data)
    for k in ("file_id_map", "class_id_map", "method_id_map",
              "module_id_map", "dir_id_map", "dbtable_id_map"):
        setattr(data, k, built[k])
    derived = derive_edge_rows(data)
    assert derived["rows"]["MethodCalls"] == []
    assert derived["rows"]["PossiblyCalls"] == []


def test_phase8_9_write_to_fake_spanner(tmp_path, fake_spanner, out_dir):
    data, a, b = _data_with_two_files(tmp_path)
    phase8_write_nodes(data)
    assert "Files" in fake_spanner.mutations
    assert len(fake_spanner.mutations["Files"]) == 2
    assert data.method_id_map  # populated as a side effect
    phase9_write_edges(data)
    assert len(fake_spanner.mutations.get("MethodCalls", [])) == 1


def test_phase10_embeddings_fake(tmp_path, fake_spanner, fake_vertexai, out_dir):
    data, a, b = _data_with_two_files(tmp_path)
    data.file_summaries = {a: "summary A", b: "summary B"}
    phase8_write_nodes(data)
    phase10_generate_embeddings(data)
    assert fake_vertexai["n"] > 0
    assert "Files" in fake_spanner.updates


def test_checkpoint_roundtrip_and_id_scheme(tmp_path, out_dir):
    data, a, b = _data_with_two_files(tmp_path)
    built = build_node_rows(data)
    for k in ("file_id_map", "class_id_map", "method_id_map",
              "module_id_map", "dir_id_map"):
        setattr(data, k, built[k])
    _save_graph_checkpoint(data, "phase8")

    fresh = PipelineData(target_dir=data.target_dir)
    phase = _load_graph_checkpoint(fresh)
    assert phase == "phase8"
    assert fresh.file_id_map == data.file_id_map

    # An old-scheme checkpoint is rejected (returns None → rebuild from Phase 8)
    cp_path = os.path.join(out_dir, "graph_checkpoint.json")
    cp = json.load(open(cp_path))
    cp["id_scheme"] = ID_SCHEME - 1
    json.dump(cp, open(cp_path, "w"))
    assert _load_graph_checkpoint(PipelineData(target_dir=data.target_dir)) is None
