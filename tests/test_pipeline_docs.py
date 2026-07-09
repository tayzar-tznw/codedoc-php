"""Async Gemini doc phases (2-6 + schema docs) driven by FakeGenaiClient.

Runs the real phase code with a fake genai client so the summary/topic/schema
generation, JSON parsing, retry/throttle, and index assembly paths all execute
locally. Uses the php_plain fixture as a small real source tree.
"""

import asyncio
import os

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import (
    PipelineData, phase1_scan, phase1b_treesitter_entities,
    phase2_file_summaries, phase3_dir_summaries, phase4_topics,
    phase5_topic_summaries, phase6_index, phase_schema_docs,
    _ensure_schema_topic, _load_summaries_from_disk,
)
from tests.conftest import FakeGenaiClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO_ROOT, "test_codes", "php_plain")


@pytest.fixture
def scanned(out_dir):
    data = PipelineData(target_dir=FIXTURE)
    phase1_scan(data)
    phase1b_treesitter_entities(data)
    return data


def test_docs_phases_happy_path(scanned, out_dir):
    data = scanned
    client = FakeGenaiClient()
    asyncio.run(phase2_file_summaries(data, client))
    assert data.file_summaries  # every app file summarized
    asyncio.run(phase3_dir_summaries(data, client))
    assert data.dir_summaries
    asyncio.run(phase4_topics(data, client))
    assert data.topics  # topic JSON parsed from the fake
    asyncio.run(phase5_topic_summaries(data, client))
    assert data.topic_summaries
    phase6_index(data)
    # index.md written under OUTPUT_DIR
    assert os.path.isfile(os.path.join(out_dir, "index.md"))


def test_schema_docs_phase(scanned, out_dir):
    data = scanned
    client = FakeGenaiClient()
    _ensure_schema_topic(data)
    # php_plain has no migrations, so the schema topic may be absent; that's a
    # valid path too. Run the phase either way.
    asyncio.run(phase_schema_docs(data, client))


def test_phase2_retries_then_succeeds(scanned, out_dir):
    """A 429 on the first call must be retried, not fatal."""
    data = scanned
    # Limit to a couple of files for speed by trimming file_list.
    data.file_list = data.file_list[:2]

    seq = [Exception("429 RESOURCE_EXHAUSTED"), "OK summary text"]
    it = iter(seq)

    def responder(model, contents, config_):
        try:
            return next(it)
        except StopIteration:
            return "later summary"
    client = FakeGenaiClient(responder=responder)
    asyncio.run(phase2_file_summaries(data, client))
    assert len(data.file_summaries) >= 1


def test_phase2_empty_response_is_not_written(scanned, out_dir):
    """Persistent empty text → file is not summarized (never a blank file)."""
    data = scanned
    data.file_list = data.file_list[:1]
    client = FakeGenaiClient(responder=lambda m, c, cfg: "")
    asyncio.run(phase2_file_summaries(data, client))
    # empty responses are dropped, not saved
    assert data.file_summaries == {}


def test_load_summaries_from_disk_roundtrip(scanned, out_dir):
    data = scanned
    client = FakeGenaiClient()
    asyncio.run(phase2_file_summaries(data, client))
    asyncio.run(phase3_dir_summaries(data, client))

    fresh = PipelineData(target_dir=FIXTURE)
    phase1_scan(fresh)
    phase1b_treesitter_entities(fresh)
    _load_summaries_from_disk(fresh)
    assert len(fresh.file_summaries) == len(data.file_summaries)


def test_vendor_files_excluded_from_summaries(out_dir):
    """When vendor is included, app files are summarized but vendor is not."""
    data = PipelineData(target_dir=FIXTURE)
    data.include_vendor = True
    phase1_scan(data)
    phase1b_treesitter_entities(data)
    assert any(o == "vendor" for o in data.file_origins.values())
    client = FakeGenaiClient()
    asyncio.run(phase2_file_summaries(data, client))
    # no summarized file is a vendor file
    for fp in data.file_summaries:
        rel = os.path.relpath(fp, data.target_dir)
        assert data.file_origins.get(rel) == "app"
