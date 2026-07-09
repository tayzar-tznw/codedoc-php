"""DDL generation + idempotent schema migration (mocked Spanner admin API)."""

import types

import pytest

from graph_generator import setup_spanner_graph as s


# ── pure DDL generation ──────────────────────────────────────────


def test_write_columns_excludes_embedding():
    cols = s.write_columns("Files")
    assert "embedding" not in cols
    assert cols[0] == "file_id"
    assert "path" in cols and "origin" in cols


def test_node_and_edge_ddl_cover_all_tables():
    node_ddl = s.node_table_ddl()
    edge_ddl = s.edge_table_ddl()
    assert len(node_ddl) == len(s.NODE_TABLES) == 6
    assert len(edge_ddl) == len(s.EDGE_TABLES) == 11
    joined = "\n".join(node_ddl + edge_ddl)
    for t in s.NODE_TABLES + s.EDGE_TABLES:
        assert f"CREATE TABLE {t} " in joined
    # INT64 columns render correctly
    assert "start_line   INT64" in joined or "start_line INT64" in "\n".join(node_ddl)


def test_graph_ddl_references_pks():
    ddl = s.graph_ddl()
    assert ddl.startswith("CREATE OR REPLACE PROPERTY GRAPH")
    assert "DbTables" in ddl and "PossiblyCalls" in ddl
    assert "TableReferences" in ddl and "ClassMapsToTable" in ddl
    # edge endpoints reference node PKs
    assert "SOURCE KEY (caller_method) REFERENCES Methods (method_id)" in ddl
    assert "SOURCE KEY (source_table) REFERENCES DbTables (table_id)" in ddl


# ── migrate_schema against a fake database ───────────────────────


class _FakeOp:
    def result(self, timeout=None):
        return None


class _FakeSnapshot:
    def __init__(self, tables, columns):
        self._tables = tables
        self._columns = columns

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute_sql(self, sql, params=None, param_types=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [[t] for t in self._tables]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            tbl = params["t"]
            return [[c] for c in self._columns.get(tbl, [])]
        return [[0]]


class _FakeDatabase:
    def __init__(self, tables, columns):
        self._tables = tables
        self._columns = columns
        self.applied_ddl = []
        self.exists_flag = True

    def exists(self):
        return self.exists_flag

    def snapshot(self):
        return _FakeSnapshot(self._tables, self._columns)

    def update_ddl(self, ddl):
        self.applied_ddl.extend(ddl)
        return _FakeOp()


def test_migrate_adds_missing_table_and_columns(capsys):
    # DB has an old Files table missing 'path'/'origin' and no DbTables table.
    existing_tables = ["Files", "Classes", "Methods", "Modules", "Directories",
                       "FileImports", "FileDependsOn", "ClassInherits",
                       "MethodCalls", "FileDefinesClass", "ClassDefinesMethod",
                       "FileBelongsToModule", "DirContainsFile"]
    columns = {t: [c for c in s.write_columns(t)] for t in existing_tables}
    # simulate old Files/Classes/Methods lacking the new columns
    columns["Files"] = ["file_id", "file_name", "extension", "directory", "summary"]
    columns["MethodCalls"] = ["edge_id", "caller_method", "callee_method", "callee_name"]

    db = _FakeDatabase(existing_tables, columns)
    s.migrate_schema(db)
    ddl = "\n".join(db.applied_ddl)
    # new tables created
    assert "CREATE TABLE DbTables" in ddl
    assert "CREATE TABLE PossiblyCalls" in ddl
    assert "CREATE TABLE TableReferences" in ddl
    # missing columns added
    assert "ALTER TABLE Files ADD COLUMN path" in ddl
    assert "ALTER TABLE Files ADD COLUMN origin" in ddl
    assert "ALTER TABLE MethodCalls ADD COLUMN resolution" in ddl
    assert "ALTER TABLE MethodCalls ADD COLUMN call_line" in ddl


def test_migrate_noop_when_up_to_date(capsys):
    all_tables = s.NODE_TABLES + s.EDGE_TABLES
    columns = {t: s.write_columns(t) + ["embedding"] for t in s.NODE_TABLES}
    columns.update({t: s.write_columns(t) for t in s.EDGE_TABLES})
    db = _FakeDatabase(all_tables, columns)
    s.migrate_schema(db)
    assert db.applied_ddl == []
    assert "up to date" in capsys.readouterr().out


def test_create_database_existing_migrates(monkeypatch):
    all_tables = s.NODE_TABLES + s.EDGE_TABLES
    columns = {t: s.write_columns(t) + ["embedding"] for t in s.NODE_TABLES}
    columns.update({t: s.write_columns(t) for t in s.EDGE_TABLES})
    db = _FakeDatabase(all_tables, columns)

    fake_instance = types.SimpleNamespace(database=lambda name: db)
    fake_client = types.SimpleNamespace(instance=lambda iid: fake_instance)
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    # existing DB path: migrate + graph refresh, no CREATE DATABASE
    s.create_database("proj", "inst", "db")
    assert any("PROPERTY GRAPH" in d for d in db.applied_ddl)


def test_verify_reports_missing(monkeypatch, capsys):
    db = _FakeDatabase(["Files"], {})  # only one table exists
    fake_instance = types.SimpleNamespace(database=lambda name: db)
    fake_client = types.SimpleNamespace(instance=lambda iid: fake_instance)
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: fake_client)
    s.verify("proj", "inst", "db")
    out = capsys.readouterr().out
    assert "MISSING tables" in out
