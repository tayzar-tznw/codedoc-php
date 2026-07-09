"""Extra pipeline coverage: map-reduce summarization + phase10 failure path."""

import asyncio
import os

from graph_generator import config, pipeline
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities,
    phase2_file_summaries, phase10_generate_embeddings, phase8_write_nodes,
)
from tests.conftest import FakeGenaiClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(REPO_ROOT, "test_codes", "php_plain")


def test_phase2_map_reduce_on_large_file(out_dir):
    """A 400 INVALID_ARGUMENT on the whole-file call falls back to chunked
    map-reduce summarization (chunk summaries → merge)."""
    data = PipelineData(target_dir=PLAIN)
    phase1_scan(data)
    phase1b_treesitter_entities(data)
    data.file_list = data.file_list[:1]  # one file, keep it fast

    calls = {"n": 0}

    def responder(model, contents, config_):
        calls["n"] += 1
        # First call = the whole-file prompt → force the too-large error once,
        # subsequent chunk/merge calls succeed.
        if calls["n"] == 1:
            return Exception("400 INVALID_ARGUMENT: request too large")
        return "チャンク要約"
    client = FakeGenaiClient(responder=responder)
    asyncio.run(phase2_file_summaries(data, client))
    assert len(data.file_summaries) == 1  # produced via map-reduce


def test_phase10_reports_failed_embeddings(out_dir, fake_spanner, monkeypatch):
    """A hard embedding failure is logged and counted, not fatal."""
    import sys, types

    class _Model:
        @classmethod
        def from_pretrained(cls, name):
            return cls()

        def get_embeddings(self, texts):
            raise RuntimeError("permanent embedding failure")

    vmod = types.ModuleType("vertexai")
    vmod.init = lambda **kw: None
    lm = types.ModuleType("vertexai.language_models")
    lm.TextEmbeddingModel = _Model
    vmod.language_models = lm
    monkeypatch.setitem(sys.modules, "vertexai", vmod)
    monkeypatch.setitem(sys.modules, "vertexai.language_models", lm)

    data = PipelineData(target_dir=PLAIN)
    phase1_scan(data)
    phase1b_treesitter_entities(data)
    data.file_list = data.file_list[:2]
    data.file_summaries = {fp: "summary" for fp in data.file_list}
    phase8_write_nodes(data)
    # should not raise even though every embedding batch fails
    phase10_generate_embeddings(data)


def test_run_docs_pipeline_resume_skips_done(out_dir, monkeypatch):
    """A second docs run reuses on-disk summaries/entities (resume path)."""
    monkeypatch.setattr(pipeline.genai, "Client", lambda **kw: FakeGenaiClient())
    from graph_generator.pipeline import run_docs_pipeline
    d1 = asyncio.run(run_docs_pipeline(PLAIN, repo="r"))
    n_files = len(d1.file_summaries)
    d2 = asyncio.run(run_docs_pipeline(PLAIN, repo="r"))
    assert len(d2.file_summaries) == n_files  # loaded from disk, not regenerated
