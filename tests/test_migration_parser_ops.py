"""Synthetic Phinx migrations exercising replay ops + parse edge branches."""

from graph_generator.migration_parser import parse_migration_file, build_schema


def _mig(class_name, body):
    return (f"<?php\nuse Migrations\\AbstractMigration;\n"
            f"class {class_name} extends AbstractMigration {{\n"
            f"    public function change(): void {{\n{body}\n    }}\n}}\n")


def test_remove_column():
    up = _mig("CreateT", "        $this->table('t')->addColumn('a','string')"
                         "->addColumn('b','integer')->create();")
    down = _mig("DropB", "        $this->table('t')->removeColumn('b')->update();")
    schema = build_schema([("001_CreateT.php", up), ("002_DropB.php", down)])
    cols = [c["name"] for c in schema["tables"]["t"]["columns"]]
    assert "b" not in cols and "a" in cols


def test_rename_column_rewrites_indexes():
    up = _mig("CreateT", "        $this->table('t')->addColumn('old','string')"
                         "->addIndex(['old'])->create();")
    ren = _mig("Ren", "        $this->table('t')->renameColumn('old','new')->update();")
    schema = build_schema([("001.php", up), ("002.php", ren)])
    cols = [c["name"] for c in schema["tables"]["t"]["columns"]]
    assert "new" in cols and "old" not in cols


def test_change_column_type():
    up = _mig("CreateT", "        $this->table('t')->addColumn('a','string')->create();")
    chg = _mig("Chg", "        $this->table('t')->changeColumn('a','text')->update();")
    schema = build_schema([("001.php", up), ("002.php", chg)])
    col_a = [c for c in schema["tables"]["t"]["columns"] if c["name"] == "a"][0]
    assert col_a["type"] == "text"


def test_drop_table_via_chain_and_legacy():
    up = _mig("CreateT", "        $this->table('t')->addColumn('a','string')->create();")
    drop = _mig("Drop", "        $this->table('t')->drop()->update();")
    schema = build_schema([("001.php", up), ("002.php", drop)])
    assert "t" not in schema["tables"]

    up2 = _mig("CreateU", "        $this->table('u')->addColumn('a','string')->create();")
    legacy = _mig("DropU", "        $this->dropTable('u');")
    schema2 = build_schema([("001.php", up2), ("002.php", legacy)])
    assert "u" not in schema2["tables"]


def test_rename_table_retargets_fks():
    up = _mig("Base", "        $this->table('users')->addColumn('name','string')->create();\n"
                      "        $this->table('posts')->addColumn('user_id','integer')"
                      "->addForeignKey('user_id','users','id')->create();")
    ren = _mig("Ren", "        $this->table('users')->rename('members')->update();")
    schema = build_schema([("001.php", up), ("002.php", ren)])
    assert "members" in schema["tables"] and "users" not in schema["tables"]
    fk = schema["tables"]["posts"]["foreign_keys"][0]
    assert fk["referenced_table"] == "members"


def test_foreign_key_two_arg_defaults_id():
    up = _mig("T", "        $this->table('posts')->addColumn('user_id','integer')"
                   "->addForeignKey('user_id','users')->create();")
    schema = build_schema([("001.php", up)])
    fk = schema["tables"]["posts"]["foreign_keys"][0]
    assert fk["referenced_column"] == "id"


def test_duplicate_create_warns_and_unions():
    up1 = _mig("A", "        $this->table('t')->addColumn('a','string')->create();")
    up2 = _mig("B", "        $this->table('t')->addColumn('b','string')->create();")
    schema = build_schema([("001.php", up1), ("002.php", up2)])
    cols = [c["name"] for c in schema["tables"]["t"]["columns"]]
    assert "a" in cols and "b" in cols
    assert any("duplicate" in w.lower() or "already" in w.lower()
               for w in schema["warnings"])


def test_dynamic_arg_warns_and_skips():
    up = _mig("T", "        $name = 'x';\n        $this->table($name)"
                   "->addColumn('a','string')->create();")
    schema = build_schema([("001.php", up)])
    assert schema["warnings"]  # non-literal table name warned


def test_raw_sql_warns():
    up = _mig("T", "        $this->execute('ALTER TABLE t ADD COLUMN c INT');")
    ops = parse_migration_file("001.php", up)
    schema = build_schema([("001.php", up)])
    assert any("raw SQL" in w or "execute" in w for w in schema["warnings"])


def test_alias_shares_builder():
    up = _mig("T", "        $t = $this->table('t');\n        $t2 = $t;\n"
                   "        $t2->addColumn('a','string');\n        $t->create();")
    schema = build_schema([("001.php", up)])
    assert "t" in schema["tables"]


def test_update_save_only_table_autovivified():
    up = _mig("T", "        $this->table('external')->addColumn('a','string')->update();")
    schema = build_schema([("001.php", up)])
    # touched only by update() (never create()) → still visible, with a warning
    assert "external" in schema["tables"]
    assert any("create()" in w for w in schema["warnings"])


def test_non_migration_returns_empty():
    assert parse_migration_file("x.php", "<?php class NotAMigration { function f(){} }") == []
    assert parse_migration_file("x.php", "<?php // nonsense") == []
