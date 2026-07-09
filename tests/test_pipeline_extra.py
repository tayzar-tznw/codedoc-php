"""Pipeline helpers + orchestrators: throttle, chunking, JSON/schema rendering,
run_docs_pipeline end-to-end (fake genai)."""

import asyncio
import os

import pytest

from graph_generator import config, pipeline
from graph_generator.pipeline import (
    _AdaptiveThrottle, _chunk_content, _truncate, _strip_markdown_fences,
    _extract_json_substring, _coerce_schema, _render_er_diagram,
    _render_schema_table, _make_id, run_docs_pipeline,
)
from tests.conftest import FakeGenaiClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(REPO_ROOT, "test_codes", "php_plain")


def test_make_id_deterministic_and_prefixed():
    a = _make_id("file", "src/A.php")
    b = _make_id("file", "src/A.php")
    c = _make_id("file", "src/B.php")
    assert a == b != c
    assert a.startswith(config.ID_PREFIX + "_")


def test_truncate_and_strip_fences():
    long = "x" * 100
    out = _truncate(long, 40)
    assert len(out) == 40 and out.endswith("[TRUNCATED]")
    assert _truncate("ab", 10) == "ab"  # under the limit → unchanged
    assert _strip_markdown_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert _strip_markdown_fences("plain") == "plain"


def test_chunk_content_line_boundaries():
    text = "\n".join(f"line{i}" for i in range(1000))
    chunks = _chunk_content(text, chunk_size=100)
    assert len(chunks) > 1
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_extract_json_substring():
    assert _extract_json_substring('noise {"a": 1} trailing') == {"a": 1}
    assert _extract_json_substring("prefix [1, 2, 3] suffix") == [1, 2, 3]
    with pytest.raises((ValueError, Exception)):
        _extract_json_substring("no json here")


def test_coerce_schema_shapes():
    # bare list of tables
    s = _coerce_schema([{"name": "users", "columns": []}])
    assert s["tables"][0]["name"] == "users"
    # wrapper dict
    s2 = _coerce_schema({"tables": [{"name": "t", "columns": []}], "overview": "o"})
    assert s2["overview"] == "o"
    # unnamed tables dropped
    s3 = _coerce_schema({"tables": [{"columns": []}, {"name": "ok"}]})
    assert [t["name"] for t in s3["tables"]] == ["ok"]


def test_render_er_and_table():
    extracted = {
        "overview": "概要",
        "tables": [
            {"name": "users", "description": "u",
             "columns": [{"name": "id", "type": "int", "constraints": "PK"}],
             "indexes": [], "foreign_keys": []},
            {"name": "articles", "description": "a",
             "columns": [{"name": "user_id", "type": "int"}],
             "indexes": [{"columns": ["user_id"], "unique": False}],
             "foreign_keys": [{"column": "user_id", "references": "users.id"}]},
        ],
        "relationships": [
            {"from": "articles", "to": "users", "cardinality": "many-to-one",
             "via": "user_id", "description": "belongs to"}],
    }
    er = _render_er_diagram(extracted)
    assert "erDiagram" in er and "users" in er and "articles" in er
    tbl = _render_schema_table(extracted["tables"][1], extracted["relationships"])
    assert "articles" in tbl and "user_id" in tbl


def test_adaptive_throttle_ramps_and_breaks():
    t = _AdaptiveThrottle()

    async def exercise():
        await t.wait()
        for _ in range(5):
            await t.on_success()
        await t.on_rate_limit()  # should back off
        await t.wait()
    asyncio.run(exercise())
    # reset restores baseline without error
    t.reset()


def test_run_docs_pipeline_end_to_end(tmp_path, monkeypatch):
    """The whole docs track (Phases 1-6) with a fake genai client."""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(pipeline.genai, "Client", lambda **kw: FakeGenaiClient())
    data = asyncio.run(run_docs_pipeline(PLAIN, include_vendor=False))
    assert data.file_summaries
    assert data.extracted_entities
    # index written
    assert os.path.isfile(os.path.join(tmp_path, "out", "index.md"))
