"""setup_spanner_graph instance lifecycle + main() dispatch (mocked client)."""

import sys
import types

import pytest

from graph_generator import setup_spanner_graph as s


class _Op:
    def result(self, timeout=None):
        return None


def _fake_client(instance_exists=True, db=None):
    inst = types.SimpleNamespace(
        exists=lambda: instance_exists,
        database=lambda name=None, ddl_statements=None: db,
        delete=lambda: None,
    )
    admin_api = types.SimpleNamespace(create_instance=lambda **kw: _Op())
    return types.SimpleNamespace(instance=lambda iid: inst,
                                 instance_admin_api=admin_api)


def test_create_instance_already_exists(monkeypatch, capsys):
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: _fake_client(True))
    s.create_instance("proj", "inst", "asia-northeast1")
    assert "already exists" in capsys.readouterr().out


def test_create_instance_creates(monkeypatch, capsys):
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: _fake_client(False))
    s.create_instance("proj", "inst", "asia-northeast1")
    out = capsys.readouterr().out
    assert "Creating instance" in out


def test_destroy_all(monkeypatch, capsys):
    dropped = {}
    db = types.SimpleNamespace(exists=lambda: True, drop=lambda: dropped.setdefault("db", True))
    monkeypatch.setattr(s.spanner, "Client", lambda **kw: _fake_client(True, db))
    s.destroy_all("proj", "inst", "db")
    assert dropped.get("db")
    assert "dropped" in capsys.readouterr().out.lower()


def test_main_verify(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--verify"])
    monkeypatch.setattr(s, "verify", lambda *a, **k: print("VERIFY-CALLED"))
    s.main()
    assert "VERIFY-CALLED" in capsys.readouterr().out


def test_main_migrate(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--migrate"])
    called = {}

    class _DB:
        pass
    fake_inst = types.SimpleNamespace(database=lambda name: _DB())
    monkeypatch.setattr(s.spanner, "Client",
                        lambda **kw: types.SimpleNamespace(instance=lambda i: fake_inst))
    monkeypatch.setattr(s, "migrate_schema", lambda db: called.setdefault("mig", True))
    monkeypatch.setattr(s, "_add_graphs", lambda db: called.setdefault("graph", True))
    s.main()
    assert called.get("mig") and called.get("graph")


def test_main_destroy_aborts_without_confirm(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--destroy"])
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    ran = {}
    monkeypatch.setattr(s, "destroy_all", lambda *a: ran.setdefault("x", True))
    s.main()
    assert "Aborted" in capsys.readouterr().out
    assert "x" not in ran


def test_main_full_setup(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--skip-instance"])
    monkeypatch.setattr(s, "create_database", lambda *a: None)
    monkeypatch.setattr(s, "verify", lambda *a: None)
    s.main()
    assert "Skipping instance" in capsys.readouterr().out
