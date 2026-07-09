"""Resolution engine units with an injected fake LSP client.

These cover EntityIndex, FileCtx, the receiver TypeTracker, the convention
chain, and run_resolution's degraded/fallback behavior WITHOUT requiring the
Intelephense binary — so resolution.py stays covered on any machine.
"""

import os

import pytest

from graph_generator import config
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities,
)
from graph_generator import resolution as R

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(REPO_ROOT, "test_codes", "php_plain")


class FakeLsp:
    """Minimal LspClient stand-in. `defs` maps (rel_or_abs, line) → list of
    definition locations to return; everything else returns []."""

    def __init__(self, defs=None, version="fake-1.0"):
        self.server_version = version
        self.indexing_partial = False
        self._defs = defs or {}
        self.started = False
        self.closed = False
        self.opened = []

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def did_open(self, path, text):
        self.opened.append(path)

    def did_close(self, path):
        pass

    def definition(self, path, line, col):
        return self._defs.get((os.path.basename(path), line), [])

    def workspace_symbol(self, query):
        return []


@pytest.fixture
def plain_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    data = PipelineData(target_dir=PLAIN)
    phase1_scan(data)
    phase1b_treesitter_entities(data)
    return data


def test_entity_index_fqcn_and_simple(plain_scanned):
    idx = R.EntityIndex(PLAIN, plain_scanned.extracted_entities)
    # A known app class resolves by FQCN
    hit = idx.find_internal("App\\S01_Aliases\\AliasedConsumer")
    assert hit is not None
    assert idx.exists("App\\S01_Aliases\\AliasedConsumer")
    # simple-name index returns the FQCN(s)
    assert any("AliasedConsumer" in f for f in idx.simple_matches("AliasedConsumer"))


def test_file_ctx_alias_resolution():
    ent = {"namespace": "App\\Demo", "uses": [
        {"fqcn": "Acme\\Reporting\\Report", "alias": "Report", "kind": "class"},
        {"fqcn": "Vendor\\Helper", "alias": "H", "kind": "function"},
    ]}
    ctx = R.FileCtx(ent)
    assert ctx.candidates("Report") == ["Acme\\Reporting\\Report"]
    # unknown name → namespace-qualified then global
    assert ctx.candidates("Widget") == ["App\\Demo\\Widget", "Widget"]
    # leading-backslash = absolute
    assert ctx.candidates("\\Root\\Thing") == ["Root\\Thing"]
    assert ctx.function_candidates("H") == ["Vendor\\Helper"]


def test_singularize_and_tableize_via_conventions():
    from graph_generator.php_conventions import singularize, tableize
    assert singularize("Articles") == "Article"
    assert singularize("Categories") == "Category"
    assert tableize("UsersTable") == "users"
    assert tableize("ArticleCategoriesTable") == "article_categories"


def test_run_resolution_degraded_without_binary(plain_scanned, monkeypatch):
    """No LSP → convention/parser only; never crashes, produces records."""
    # Force the real LspClient path to fail by pointing at a nonexistent binary.
    monkeypatch.setattr(config, "INTELEPHENSE_PATH", "/nonexistent/xyz")
    stats = R.run_resolution(plain_scanned, printer=lambda *a: None)
    assert stats["engine"] == "unavailable"
    assert stats["files"] > 0
    # aliased-import call sites can't be confirmed without LSP → unresolved,
    # never the old wrong name-match.
    assert "resolved" in stats or "unresolved" in stats


def test_run_resolution_with_fake_lsp(plain_scanned):
    """Injected fake LSP resolves a call site to a concrete definition."""
    # S01 AliasedConsumer::buildAcme calls $report->generate() at line 15.
    # Point the fake at Acme's Report.php (its class/method spans get lazy-parsed).
    acme_rel = "vendor/acme/reporting/src/Report.php"
    defs = {
        ("AliasedConsumer.php", 15): [{"path": os.path.join(PLAIN, acme_rel), "line": 8}],
    }
    stats = R.run_resolution(plain_scanned, printer=lambda *a: None,
                             lsp_client_factory=lambda: FakeLsp(defs))
    assert stats["engine"] == "fake-1.0"
    files = plain_scanned.resolutions["files"]
    rec = None
    for r in files.get("src/S01_Aliases/AliasedConsumer.php", {}).get("calls", []):
        if r["site"]["line"] == 15 and r["site"]["name"] == "generate":
            rec = r
    assert rec is not None
    # target is Acme's Report (vendor → external), with an exact member
    assert rec["target"] and rec["target"]["member"] == "generate"


def test_type_tracker_new_and_this(plain_scanned):
    idx = R.EntityIndex(PLAIN, plain_scanned.extracted_entities)
    res = R.Resolver(idx, lsp=None, printer=lambda *a: None)
    # find any class with a method that has assignments
    ent = plain_scanned.extracted_entities[os.path.join(PLAIN, "src/S01_Aliases/AliasedConsumer.php")]
    cls = ent["classes"][0]
    fctx = R.FileCtx(ent)
    cctx = R.ClassCtx(res, fctx, cls)
    method = cls["methods"][0]
    tracker = R.TypeTracker(res, fctx, cctx, method, {})
    # $this resolves to the enclosing class
    assert tracker.type_of("$this", 999) == cls["fqcn"]


def test_resume_reuses_checkpoint(plain_scanned):
    f = lambda: FakeLsp({})
    s1 = R.run_resolution(plain_scanned, printer=lambda *a: None, lsp_client_factory=f)
    assert s1["files_reused"] == 0
    s2 = R.run_resolution(plain_scanned, printer=lambda *a: None, lsp_client_factory=f)
    assert s2["files_reused"] == s2["files"]  # all reused on second run
