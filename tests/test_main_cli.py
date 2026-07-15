"""CLI dispatch, vendor resolution, and validate (no GCP)."""

import os
import sys
import types

import pytest

from graph_generator import __main__ as m
from graph_generator import config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAKE = os.path.join(REPO_ROOT, "test_codes", "php_cakephp")


def _args(**kw):
    return types.SimpleNamespace(**kw)


def test_resolve_include_vendor_flags():
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=True, exclude_vendor=False)) is True
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=False, exclude_vendor=True)) is False


def test_resolve_include_vendor_env(monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_VENDOR", "true")
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=False, exclude_vendor=False)) is True
    monkeypatch.setattr(config, "INCLUDE_VENDOR", "false")
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=False, exclude_vendor=False)) is False


def test_resolve_include_vendor_non_tty_defaults_exclude(monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_VENDOR", "")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    # cakephp has a vendor tree → prompt path, non-tty → exclude
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=False, exclude_vendor=False)) is False


def test_resolve_include_vendor_prompt_yes(monkeypatch):
    monkeypatch.setattr(config, "INCLUDE_VENDOR", "")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert m._resolve_include_vendor(CAKE, _args(include_vendor=False, exclude_vendor=False)) is True


def test_resolve_include_vendor_no_vendor_dir(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.php").write_text("<?php class X {}", encoding="utf-8")
    assert m._resolve_include_vendor(str(tmp_path), _args(include_vendor=False, exclude_vendor=False)) is False


def test_main_dispatch_no_command_prints_help(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 1
    assert "Available commands" in capsys.readouterr().out


def test_cmd_evaluate_exit_code(monkeypatch):
    called = {}

    def fake_run(fixtures, repo_root, dump_edges=False):
        called["fixtures"] = fixtures
        return 0
    monkeypatch.setattr("graph_generator.evaluate.run_evaluation", fake_run)
    with pytest.raises(SystemExit) as ei:
        m.cmd_evaluate(_args(fixture="all", dump_edges=False))
    assert ei.value.code == 0
    assert called["fixtures"] == ["php_plain", "php_cakephp"]


def test_cmd_validate_with_fake_db(monkeypatch, capsys):
    from tests.conftest import FakeSpannerDb
    # GROUP BY repo marker must precede the plain COUNT(*) marker (the group
    # query contains both substrings; first match wins).
    db = FakeSpannerDb(sql_rows={
        "GROUP BY source_repo": [["web", "shared", 4]],  # cross-repo coupling
        "GROUP BY repo": [["web", 10], ["api", 7]],
        "COUNT(*)": [[3]],
    })
    monkeypatch.setattr(m, "_get_spanner_db", lambda: db)
    m.cmd_validate(_args())
    out = capsys.readouterr().out
    assert "GRAPH VALIDATION" in out
    assert "Files" in out and "MethodCalls" in out
    assert "Orphan checks" in out
    assert "Nodes by repo" in out
    assert "web=10" in out and "api=7" in out
    assert "Cross-repo coupling" in out
    assert "web → shared" in out


def test_load_pipeline_data_backfills_missing_fields(tmp_path, monkeypatch):
    """An old pickle lacking new dataclass fields is backfilled, not crashed."""
    import pickle
    from graph_generator.pipeline import PipelineData
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))

    # Simulate an old PipelineData missing the newer fields.
    old = PipelineData(target_dir=str(tmp_path))
    delattr(old, "resolutions")
    delattr(old, "file_origins")
    delattr(old, "dbtable_id_map")
    path = m._pipeline_data_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(old, f)

    loaded = m._load_pipeline_data(str(tmp_path))
    assert loaded is not None
    assert loaded.resolutions == {}
    assert loaded.file_origins == {}
    assert loaded.dbtable_id_map == {}
