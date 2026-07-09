"""End-to-end local evaluation on the committed fixtures (no GCP).

Running evaluate_fixture exercises the whole local stack — Phase 1 scan,
tree-sitter extraction, LSP resolution, migration schema, and the pure
node/edge derivation — plus the scoring/reporting in evaluate.py. It is the
single highest-coverage test and doubles as the acceptance gate: both fixtures
must clear 85% completeness, 85% entity coverage, and zero wrong edges.
"""

import os
import shutil

import pytest

from graph_generator import config
from graph_generator.evaluate import evaluate_fixture, print_report, run_evaluation

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HAS_INTELEPHENSE = shutil.which(config.INTELEPHENSE_PATH) is not None


@pytest.fixture
def eval_out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "eval"))
    return str(tmp_path / "eval")


@pytest.mark.skipif(not _HAS_INTELEPHENSE, reason="intelephense not installed")
@pytest.mark.parametrize("fixture,min_complete", [
    ("php_plain", 85.0),
    ("php_cakephp", 85.0),
])
def test_fixture_meets_gates(fixture, min_complete, eval_out):
    if fixture == "php_cakephp" and not os.path.isfile(
            os.path.join(REPO_ROOT, "test_codes", fixture, "vendor", "autoload.php")):
        pytest.skip("composer vendor not installed for php_cakephp")

    report = evaluate_fixture(fixture, REPO_ROOT)
    assert "error" not in report, report.get("error")

    # The hard guarantees.
    assert report["completeness"] >= min_complete, \
        f"{fixture} completeness {report['completeness']:.1f}% < {min_complete}%"
    assert report["coverage"] >= 85.0, \
        f"{fixture} coverage {report['coverage']:.1f}% < 85%"
    assert report["wrong_edges"] == [], \
        f"{fixture} has wrong edges: {report['wrong_edges']}"

    # print_report returns the pass/fail verdict and covers the reporting path.
    assert print_report(report, printer=lambda *a: None) is True


@pytest.mark.skipif(not _HAS_INTELEPHENSE, reason="intelephense not installed")
def test_php_plain_resolves_vendor_twins(eval_out):
    """The headline case: identically named vendor classes/methods must not
    collapse — each generate() call resolves to its own package."""
    report = evaluate_fixture("php_plain", REPO_ROOT)
    by_id = {r["id"]: r for r in report["case_results"]}
    assert by_id["S01-01"]["correct"]
    assert by_id["S01-02"]["correct"]
    # S01-01/02 are byte-identical call sites in the same file — different answers
    assert by_id["S01-01"]["answer"] != by_id["S01-02"]["answer"]


@pytest.mark.skipif(not _HAS_INTELEPHENSE, reason="intelephense not installed")
def test_run_evaluation_exit_code(eval_out):
    code = run_evaluation(["php_plain"], REPO_ROOT, printer=lambda *a: None)
    assert code == 0


def test_evaluate_missing_vendor_reports_error(tmp_path, monkeypatch):
    """php_cakephp without composer vendor → structured error, not a crash."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "eval"))
    fake_repo = tmp_path / "repo"
    (fake_repo / "test_codes" / "php_cakephp").mkdir(parents=True)
    # minimal ground_truth so the vendor check is what fails
    (fake_repo / "test_codes" / "php_cakephp" / "ground_truth.json").write_text(
        '{"cases": []}', encoding="utf-8")
    report = evaluate_fixture("php_cakephp", str(fake_repo))
    assert report.get("error") == "composer vendor missing"
    assert "hint" in report
    # print_report should render the error path and return False
    assert print_report(report, printer=lambda *a: None) is False


def test_evaluate_missing_ground_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "eval"))
    fake_repo = tmp_path / "repo"
    (fake_repo / "test_codes" / "php_plain").mkdir(parents=True)
    report = evaluate_fixture("php_plain", str(fake_repo))
    assert "error" in report
