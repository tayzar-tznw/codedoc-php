"""Unit tests for migration_parser (Phinx/CakePHP migration schema replay)."""

import pathlib

from graph_generator.migration_parser import build_schema, parse_migration_file

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

FIXTURE_PATHS = [
    "test_codes/php_cakephp/config/Migrations/20260708000001_CreateUsers.php",
    "test_codes/php_cakephp/config/Migrations/20260708000002_CreateArticles.php",
    "test_codes/php_cakephp/plugins/Billing/config/Migrations/20260708000003_CreateInvoices.php",
]


def _fixture_files():
    return [(p, (REPO_ROOT / p).read_text(encoding="utf-8"))
            for p in FIXTURE_PATHS]


def _mig(class_name, body, method="change"):
    """Minimal synthetic migration file around a method body."""
    return (
        "<?php\n"
        "use Migrations\\AbstractMigration;\n"
        f"class {class_name} extends AbstractMigration\n"
        "{\n"
        f"    public function {method}(): void\n"
        "    {\n"
        f"{body}\n"
        "    }\n"
        "}\n"
    )


def _col_names(table):
    return [c["name"] for c in table["columns"]]


def _col(table, name):
    for c in table["columns"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"column {name} not in {_col_names(table)}")


# -------------------------------------------------------------------
# Real fixtures
# -------------------------------------------------------------------


def test_fixtures_yield_all_tables():
    schema = build_schema(_fixture_files())
    assert set(schema["tables"]) == {"users", "articles", "invoices"}
    assert schema["warnings"] == []


def test_fixture_users_columns_and_unique_email_index():
    users = build_schema(_fixture_files())["tables"]["users"]
    assert _col_names(users) == ["id", "name", "email", "created", "modified"]
    assert users["columns"][0] == {
        "name": "id", "type": "integer", "options": {"primary_key": True}}
    assert _col(users, "email")["options"] == {"limit": 255}
    assert users["indexes"] == [
        {"columns": ["email"], "unique": True, "name": ""}]
    assert users["foreign_keys"] == []


def test_fixture_articles_columns_fk_and_unique_slug_index():
    articles = build_schema(_fixture_files())["tables"]["articles"]
    assert _col_names(articles) == [
        "id", "user_id", "title", "slug", "published",
        "author_first", "author_last", "created", "modified"]
    assert _col(articles, "user_id")["type"] == "integer"
    assert _col(articles, "user_id")["options"] == {"null": True}
    assert _col(articles, "published")["options"] == {"default": False}
    assert _col(articles, "author_first")["options"] == {
        "limit": 100, "null": True}
    assert articles["indexes"] == [
        {"columns": ["slug"], "unique": True, "name": ""}]
    assert articles["foreign_keys"] == [{
        "column": "user_id", "referenced_table": "users",
        "referenced_column": "id"}]


def test_fixture_invoices_decimal_options():
    invoices = build_schema(_fixture_files())["tables"]["invoices"]
    assert _col_names(invoices) == ["id", "total", "synced", "created"]
    total = _col(invoices, "total")
    assert total["type"] == "decimal"
    assert total["options"] == {"precision": 10, "scale": 2}
    assert _col(invoices, "synced")["options"] == {"default": False}


def test_fixture_source_file_is_creating_migration_path():
    tables = build_schema(_fixture_files())["tables"]
    assert tables["users"]["source_file"] == FIXTURE_PATHS[0]
    assert tables["articles"]["source_file"] == FIXTURE_PATHS[1]
    assert tables["invoices"]["source_file"] == FIXTURE_PATHS[2]


def test_fixture_input_order_does_not_matter():
    assert (build_schema(list(reversed(_fixture_files())))
            == build_schema(_fixture_files()))


def test_parse_migration_file_ops_in_source_order():
    path = FIXTURE_PATHS[1]
    ops = parse_migration_file(path, (REPO_ROOT / path).read_text())
    assert [o["op"] for o in ops] == (
        ["add_column"] * 8 + ["add_index", "add_foreign_key", "create_table"])
    assert {o["table"] for o in ops} == {"articles"}
    assert ops[0] == {
        "op": "add_column", "table": "articles", "line": 11,
        "column": "user_id", "type": "integer", "options": {"null": True}}
    assert ops[9] == {
        "op": "add_foreign_key", "table": "articles", "line": 20,
        "column": "user_id", "referenced_table": "users",
        "referenced_column": "id"}


# -------------------------------------------------------------------
# Synthetic migrations: replay ops not present in fixtures
# -------------------------------------------------------------------


def test_remove_column():
    files = [
        ("001_Create.php", _mig("Create", """\
        $table = $this->table('t');
        $table->addColumn('a', 'string')
            ->addColumn('b', 'string')
            ->create();""")),
        ("002_Alter.php", _mig("Alter", """\
        $this->table('t')->removeColumn('a')->update();""")),
    ]
    schema = build_schema(files)
    assert _col_names(schema["tables"]["t"]) == ["id", "b"]
    assert schema["warnings"] == []


def test_remove_column_also_drops_its_indexes_and_fks():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('a', 'string')
            ->addIndex(['a'])
            ->create();""")),
        ("002_Alter.php", _mig("Alter", """\
        $this->table('t')->removeColumn('a')->update();""")),
    ]
    t = build_schema(files)["tables"]["t"]
    assert _col_names(t) == ["id"]
    assert t["indexes"] == []


def test_rename_column_updates_indexes_and_fks():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('old', 'integer')
            ->addIndex(['old'], ['unique' => true])
            ->addForeignKey('old', 'other', 'id')
            ->create();""")),
        ("002_Alter.php", _mig("Alter", """\
        $this->table('t')->renameColumn('old', 'new')->update();""")),
    ]
    t = build_schema(files)["tables"]["t"]
    assert _col_names(t) == ["id", "new"]
    assert _col(t, "new")["type"] == "integer"
    assert t["indexes"] == [{"columns": ["new"], "unique": True, "name": ""}]
    assert t["foreign_keys"] == [{
        "column": "new", "referenced_table": "other",
        "referenced_column": "id"}]


def test_change_column_replaces_type_and_options():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('a', 'string', ['limit' => 50])->create();""")),
        ("002_Alter.php", _mig("Alter", """\
        $this->table('t')->changeColumn('a', 'text', ['null' => true])->update();""")),
    ]
    t = build_schema(files)["tables"]["t"]
    assert _col(t, "a") == {"name": "a", "type": "text",
                            "options": {"null": True}}


def test_drop_table():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('a', 'string')->create();""")),
        ("002_Drop.php", _mig("Drop", """\
        $this->table('t')->drop()->save();""")),
    ]
    schema = build_schema(files)
    assert schema["tables"] == {}
    assert schema["warnings"] == []


def test_rename_table_moves_entry_and_retargets_fks():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('old_name')->addColumn('a', 'string')->create();
        $this->table('refs')->addColumn('old_id', 'integer')
            ->addForeignKey('old_id', 'old_name', 'id')
            ->create();""")),
        ("002_Rename.php", _mig("Rename", """\
        $this->table('old_name')->rename('new_name')->update();""")),
    ]
    schema = build_schema(files)
    assert set(schema["tables"]) == {"new_name", "refs"}
    assert schema["tables"]["new_name"]["name"] == "new_name"
    assert schema["tables"]["refs"]["foreign_keys"][0]["referenced_table"] \
        == "new_name"
    assert schema["warnings"] == []


def test_duplicate_create_warns_and_merges_keep_first():
    files = [
        ("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('a', 'string')->create();""")),
        ("002_CreateAgain.php", _mig("CreateAgain", """\
        $this->table('t')->addColumn('a', 'integer')
            ->addColumn('b', 'integer')
            ->create();""")),
    ]
    schema = build_schema(files)
    t = schema["tables"]["t"]
    assert _col_names(t) == ["id", "a", "b"]      # union of both creates
    assert _col(t, "a")["type"] == "string"       # first definition wins
    assert t["source_file"] == "001_Create.php"
    assert any("duplicate create" in w and "'t'" in w
               for w in schema["warnings"])


def test_dynamic_argument_skips_op_with_warning():
    files = [("001_Create.php", _mig("Create", """\
        $name = getenv('COL');
        $this->table('t')->addColumn($name, 'string')
            ->addColumn('ok', 'string')
            ->create();"""))]
    schema = build_schema(files)
    assert _col_names(schema["tables"]["t"]) == ["id", "ok"]
    assert any("addColumn" in w and "non-literal" in w
               for w in schema["warnings"])


def test_dynamic_table_name_skips_chain_with_warning():
    files = [("001_Create.php", _mig("Create", """\
        $this->table($tbl)->addColumn('a', 'string')->create();"""))]
    schema = build_schema(files)
    assert schema["tables"] == {}
    assert any("non-literal table name" in w for w in schema["warnings"])


def test_direct_chaining_without_variable():
    files = [("001_Create.php", _mig("Create", """\
        $this->table('x')->addColumn('a', 'string', ['limit' => 10])->create();"""))]
    t = build_schema(files)["tables"]["x"]
    assert _col_names(t) == ["id", "a"]
    assert _col(t, "a")["options"] == {"limit": 10}


def test_multi_statement_builder_variable():
    files = [("001_Create.php", _mig("Create", """\
        $table = $this->table('t');
        $table->addColumn('a', 'string');
        $table->addIndex(['a']);
        $table->create();"""))]
    t = build_schema(files)["tables"]["t"]
    assert _col_names(t) == ["id", "a"]
    assert t["indexes"] == [{"columns": ["a"], "unique": False, "name": ""}]


def test_up_method_is_parsed_when_no_change():
    files = [("001_Create.php",
              _mig("Create", """\
        $this->table('t')->addColumn('a', 'string')->create();""",
                   method="up"))]
    assert _col_names(build_schema(files)["tables"]["t"]) == ["id", "a"]


def test_table_options_id_false_disables_implicit_pk():
    files = [("001_Create.php", _mig("Create", """\
        $this->table('t', ['id' => false])->addColumn('a', 'string')->create();"""))]
    assert _col_names(build_schema(files)["tables"]["t"]) == ["a"]


def test_foreign_key_array_form_records_first_pair():
    files = [("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('a', 'integer')
            ->addColumn('b', 'integer')
            ->addForeignKey(['a', 'b'], 'other', ['x', 'y'])
            ->create();"""))]
    t = build_schema(files)["tables"]["t"]
    assert t["foreign_keys"] == [{
        "column": "a", "referenced_table": "other", "referenced_column": "x"}]


def test_foreign_key_two_arg_form_defaults_to_id():
    files = [("001_Create.php", _mig("Create", """\
        $this->table('t')->addColumn('u_id', 'integer')
            ->addForeignKey('u_id', 'users')
            ->create();"""))]
    t = build_schema(files)["tables"]["t"]
    assert t["foreign_keys"] == [{
        "column": "u_id", "referenced_table": "users",
        "referenced_column": "id"}]


def test_update_on_table_never_created_warns():
    files = [("002_Alter.php", _mig("Alter", """\
        $this->table('ghost')->addColumn('a', 'string')->update();"""))]
    schema = build_schema(files)
    assert _col_names(schema["tables"]["ghost"]) == ["a"]  # no implicit id
    assert any("ghost" in w and "no create()" in w for w in schema["warnings"])


# -------------------------------------------------------------------
# parse_migration_file on non-migrations
# -------------------------------------------------------------------


def test_non_migration_class_returns_empty():
    src = """<?php
class UsersController extends AppController
{
    public function change(): void
    {
        $this->table('users')->addColumn('a', 'string')->create();
    }
}
"""
    assert parse_migration_file("c.php", src) == []


def test_migration_without_change_or_up_returns_empty():
    src = """<?php
use Migrations\\AbstractMigration;
class Weird extends AbstractMigration
{
    public function down(): void
    {
        $this->table('t')->drop()->save();
    }
}
"""
    assert parse_migration_file("w.php", src) == []


def test_garbage_input_returns_empty():
    assert parse_migration_file("g.php", "this is not php at all {{{") == []
    assert parse_migration_file("e.php", "") == []
