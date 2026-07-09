"""cmd_* command bodies with the GCP-touching entrypoints mocked out."""

import os
import types

import pytest

from graph_generator import __main__ as m
from graph_generator import config
from graph_generator.pipeline import PipelineData


def _args(**kw):
    return types.SimpleNamespace(**kw)


def test_cmd_init_writes_env(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *a: "")  # accept defaults
    m.cmd_init(_args(force=False))
    env = (tmp_path / ".env").read_text()
    assert "GOOGLE_CLOUD_PROJECT=" in env
    assert "SPANNER_INSTANCE=" in env
    # second call without --force is a no-op
    m.cmd_init(_args(force=False))


def test_cmd_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    (tmp_path / ".env").write_text("OLD", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *a: "myproj")
    m.cmd_init(_args(force=True))
    assert "myproj" in (tmp_path / ".env").read_text()


def test_print_timing_report_and_banner(capsys):
    data = PipelineData(target_dir="/x")
    data.timings = {"phase1_scan": 1.2, "phase1c_lsp_resolution": 3.4,
                    "phase8_write_nodes": 0.5}
    m._print_timing_report(data, 5.1)
    m._print_banner("Test", "/x")
    out = capsys.readouterr().out
    assert "TIMING REPORT" in out
    assert "Phase 1.6" in out


def test_cmd_docs_mocked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    target = tmp_path / "proj"
    target.mkdir()

    async def fake_docs(target_dir, include_vendor=False, repo=""):
        d = PipelineData(target_dir=str(target))
        d.repo = repo
        d.timings = {"phase1_scan": 0.1}
        return d
    monkeypatch.setattr(m, "run_docs_pipeline", fake_docs)
    m.cmd_docs(_args(target_dir=str(target), include_vendor=False,
                     exclude_vendor=True, repo_name=None, repos=None))
    # pkl is written under the per-repo output dir (repo defaults to basename)
    assert os.path.isfile(os.path.join(str(tmp_path / "out"), "repos", "proj",
                                       "pipeline_data.pkl"))


def test_cmd_docs_batch_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    (tmp_path / "web").mkdir()
    (tmp_path / "api").mkdir()
    manifest = tmp_path / "repos.json"
    import json as _json
    manifest.write_text(_json.dumps([
        {"name": "web", "path": str(tmp_path / "web"), "include_vendor": False},
        {"name": "api", "path": str(tmp_path / "api"), "include_vendor": False},
    ]))
    seen = []

    async def fake_docs(target_dir, include_vendor=False, repo=""):
        seen.append(repo)
        d = PipelineData(target_dir=target_dir)
        d.repo = repo
        return d
    monkeypatch.setattr(m, "run_docs_pipeline", fake_docs)
    m.cmd_docs(_args(target_dir=None, repos=str(manifest),
                     repo_name=None, include_vendor=False, exclude_vendor=False))
    assert seen == ["web", "api"]
    # each repo got its own isolated output dir
    for r in ("web", "api"):
        assert os.path.isdir(os.path.join(str(tmp_path / "out"), "repos", r))


def test_resolve_repo_name_default_and_flag(tmp_path):
    d = tmp_path / "myrepo"
    d.mkdir()
    assert m._resolve_repo_name(str(d), _args(repo_name=None)) == "myrepo"
    assert m._resolve_repo_name(str(d), _args(repo_name="custom")) == "custom"


def test_cmd_run_mocked(tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()

    async def fake_pipeline(target_dir, include_vendor=False):
        return PipelineData(target_dir=str(target))
    monkeypatch.setattr(m, "run_pipeline", fake_pipeline)
    m.cmd_run(_args(target_dir=str(target), include_vendor=False, exclude_vendor=True))


def test_cmd_graph_no_pkl_runs_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    target = tmp_path / "proj"
    target.mkdir()
    (target / "A.php").write_text("<?php\nnamespace App;\nclass A { function f(){} }",
                                  encoding="utf-8")
    ran = {}
    monkeypatch.setattr(m, "run_graph_pipeline", lambda data: ran.setdefault("ok", True))
    m.cmd_graph(_args(target_dir=str(target), include_vendor=False, exclude_vendor=True))
    assert ran.get("ok")


def test_cmd_analyze_mocked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    target = tmp_path / "proj"
    target.mkdir()

    async def fake_docs(target_dir, include_vendor=False, repo=""):
        d = PipelineData(target_dir=str(target))
        return d
    monkeypatch.setattr(m, "run_docs_pipeline", fake_docs)
    monkeypatch.setattr(m, "run_graph_pipeline", lambda data: None)
    m.cmd_analyze(_args(target_dir=str(target), include_vendor=False, exclude_vendor=True))


def test_cmd_setup_spanner_mocked(monkeypatch, capsys):
    import graph_generator.setup_spanner_graph as s
    monkeypatch.setattr(s, "create_instance", lambda *a, **k: None)
    monkeypatch.setattr(s, "create_database", lambda *a, **k: None)
    monkeypatch.setattr(s, "verify", lambda *a, **k: None)
    m.cmd_setup_spanner(_args(instance=None, database=None, region=None, skip_instance=False))
    assert "Setup Spanner Graph" in capsys.readouterr().out


def test_check_config_missing_project(monkeypatch):
    monkeypatch.setattr(config, "GCP_PROJECT", "")
    with pytest.raises(SystemExit):
        m._check_config()
