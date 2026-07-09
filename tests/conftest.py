"""Shared pytest fixtures and fakes for the graph_generator test suite.

The pipeline's three external services — Vertex Gemini (google.genai), Cloud
Spanner, and Vertex embeddings — are replaced with in-process fakes so every
phase runs locally with no GCP. The fakes are deliberately small: they record
what the pipeline sends and return programmable responses.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════════════════════════════════════════════════════
# Fake google.genai client
# ═══════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, text: str, finish_reason=None):
        self._text = text
        if finish_reason is not None:
            cand = types.SimpleNamespace(finish_reason=finish_reason)
            self.candidates = [cand]
        else:
            self.candidates = []

    @property
    def text(self):
        return self._text


def default_responder(model: str, contents: str, config) -> str:
    """Return content shaped for whichever phase issued the prompt."""
    p = contents if isinstance(contents, str) else str(contents)
    wants_json = ("JSON形式" in p or "JSON配列" in p or "valid JSON" in p
                  or "ONLY valid JSON" in p)
    is_topic = "モジュール" in p or "トピック" in p
    is_schema = "スキーマ" in p or "テーブル" in p or "overview" in p

    if wants_json and is_schema:
        return ('{"overview":"テスト用スキーマ概要",'
                '"tables":[{"name":"users","description":"ユーザー",'
                '"columns":[{"name":"id","type":"integer","constraints":"PK","description":"主キー"},'
                '{"name":"email","type":"string","constraints":"unique","description":"メール"}],'
                '"indexes":[{"columns":["email"],"unique":true,"name":"email_idx"}],'
                '"foreign_keys":[]}],'
                '"relationships":[]}')
    if wants_json and is_topic:
        return ('[{"name":"サンプルモジュール",'
                '"linked_files":["UsersController.php"],'
                '"subtopics":[{"name":"サブ","linked_files":["UsersController.php"]}]}]')
    return "これはテスト用の日本語要約です。ファイルの目的を説明します。"


class _FakeModels:
    def __init__(self, owner):
        self._owner = owner

    async def generate_content(self, model=None, contents=None, config=None):
        self._owner.calls += 1
        responder = self._owner.responder
        result = responder(model, contents, config)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, _FakeResponse):
            return result
        return _FakeResponse(result)


class FakeGenaiClient:
    """Stand-in for genai.Client with an async aio.models.generate_content.

    `responder(model, contents, config)` may return a str, a `_FakeResponse`,
    or an Exception instance (which is raised — for testing retry/backoff).
    Pass a list to `script` to return/raise successive items per call.
    """

    def __init__(self, responder=default_responder, script=None):
        if script is not None:
            it = iter(script)

            def _scripted(model, contents, config):
                try:
                    return next(it)
                except StopIteration:
                    return default_responder(model, contents, config)
            responder = _scripted
        self.responder = responder
        self.calls = 0
        self.aio = types.SimpleNamespace(models=_FakeModels(self))


# ═══════════════════════════════════════════════════════════════════
# Fake Cloud Spanner database
# ═══════════════════════════════════════════════════════════════════


class _FakeBatch:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def insert_or_update(self, table, columns, values):
        self._db.mutations.setdefault(table, []).extend(values)
        self._db.columns[table] = columns

    def update(self, table, columns, values):
        self._db.updates.setdefault(table, []).extend(values)


class _FakeSnapshot:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_sql(self, sql, params=None, param_types=None):
        return self._db.sql_result(sql, params)


class FakeSpannerDb:
    """Records mutations; returns canned rows for snapshot queries."""

    def __init__(self, sql_rows=None):
        self.mutations: dict[str, list] = {}
        self.updates: dict[str, list] = {}
        self.columns: dict[str, list] = {}
        self._sql_rows = sql_rows or {}

    def batch(self):
        return _FakeBatch(self)

    def snapshot(self):
        return _FakeSnapshot(self)

    def sql_result(self, sql, params):
        for marker, rows in self._sql_rows.items():
            if marker in sql:
                return list(rows)
        return [[0]]  # default COUNT(*)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Point config.OUTPUT_DIR at an isolated tmp dir for the duration."""
    from graph_generator import config
    d = tmp_path / "out"
    d.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", str(d))
    return str(d)


@pytest.fixture
def fake_genai():
    return FakeGenaiClient()


@pytest.fixture
def fake_spanner(monkeypatch):
    """Install a FakeSpannerDb as pipeline._get_spanner_db()'s return."""
    from graph_generator import pipeline
    db = FakeSpannerDb()
    monkeypatch.setattr(pipeline, "_get_spanner_db", lambda: db)
    return db


@pytest.fixture
def fake_vertexai(monkeypatch):
    """Inject fake vertexai + vertexai.language_models into sys.modules so
    Phase 10's function-local imports resolve to programmable embeddings."""
    calls = {"n": 0}

    class _Emb:
        def __init__(self, values):
            self.values = values

    class _Model:
        @classmethod
        def from_pretrained(cls, name):
            return cls()

        def get_embeddings(self, texts):
            calls["n"] += 1
            return [_Emb([0.1, 0.2, 0.3]) for _ in texts]

    vertexai_mod = types.ModuleType("vertexai")
    vertexai_mod.init = lambda **kw: None
    lm_mod = types.ModuleType("vertexai.language_models")
    lm_mod.TextEmbeddingModel = _Model
    vertexai_mod.language_models = lm_mod
    monkeypatch.setitem(sys.modules, "vertexai", vertexai_mod)
    monkeypatch.setitem(sys.modules, "vertexai.language_models", lm_mod)
    return calls
