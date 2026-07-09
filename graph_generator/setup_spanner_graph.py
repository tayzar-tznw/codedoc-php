"""
Setup script for Spanner Graph infrastructure.

Creates (or idempotently migrates):
- Spanner Enterprise instance
- Database with node + edge tables + one property graph
- Property graph: config.GRAPH_NAME (default code_graph_a)

The schema is declared once in SCHEMA_SPEC and everything (CREATE TABLE DDL,
the property-graph definition, the pipeline's insert column lists, and the
idempotent ALTER migration for existing databases) is derived from it — so the
DDL and the Phase 8/9 writes can never drift apart.

Usage:
    python -m graph_generator setup spanner              # 通常はこちら
    python -m graph_generator.setup_spanner_graph [--project PROJECT] [--region REGION]
    python -m graph_generator.setup_spanner_graph --skip-instance   # skip instance creation
    python -m graph_generator.setup_spanner_graph --migrate          # add missing tables/columns only
    python -m graph_generator.setup_spanner_graph --verify|--destroy
"""

import argparse

from google.cloud import spanner
from google.api_core import exceptions as gax_exceptions

from . import config

PROJECT_ID = config.GCP_PROJECT
INSTANCE_ID = config.SPANNER_INSTANCE
DATABASE_ID = config.SPANNER_DATABASE

REGION = "asia-northeast1"


# ===================================================================
# Schema specification (single source of truth)
# ===================================================================
#
# Column type shorthands: "str" → STRING(MAX), "int" → INT64,
# "vec" → ARRAY<FLOAT64> (embeddings; written in Phase 10, not Phase 8).
# JSON payloads (DbTables.columns etc.) are stored as STRING(MAX).

_TYPE_SQL = {"str": "STRING(MAX)", "int": "INT64", "vec": "ARRAY<FLOAT64>"}

# Node tables: name → (pk, [(column, type), ...]). Every node table also gets an
# `embedding` vector column (only Files/Classes/Modules are actually embedded).
NODE_SPEC: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "Files": ("file_id", [
        ("file_id", "str"), ("file_name", "str"), ("extension", "str"),
        ("directory", "str"), ("path", "str"), ("origin", "str"),
        ("summary", "str"), ("embedding", "vec"),
    ]),
    "Classes": ("class_id", [
        ("class_id", "str"), ("name", "str"), ("namespace", "str"),
        ("fqcn", "str"), ("file_id", "str"), ("kind", "str"),
        ("modifiers", "str"), ("start_line", "int"), ("end_line", "int"),
        ("origin", "str"), ("summary", "str"), ("embedding", "vec"),
    ]),
    "Methods": ("method_id", [
        ("method_id", "str"), ("name", "str"), ("class_id", "str"),
        ("file_id", "str"), ("fqmn", "str"), ("signature", "str"),
        ("modifiers", "str"), ("return_type", "str"), ("start_line", "int"),
        ("end_line", "int"), ("origin", "str"), ("summary", "str"),
        ("embedding", "vec"),
    ]),
    "Modules": ("module_id", [
        ("module_id", "str"), ("name", "str"), ("summary", "str"),
        ("embedding", "vec"),
    ]),
    "Directories": ("dir_id", [
        ("dir_id", "str"), ("name", "str"), ("summary", "str"),
        ("embedding", "vec"),
    ]),
    "DbTables": ("table_id", [
        ("table_id", "str"), ("name", "str"), ("columns", "str"),
        ("indexes", "str"), ("foreign_keys", "str"), ("source_file", "str"),
        ("plugin", "str"), ("summary", "str"), ("embedding", "vec"),
    ]),
}

# Edge tables: name → (pk, [(column, type)...], (src_col, src_table), (dst_col, dst_table)).
EDGE_SPEC: dict[str, tuple[str, list[tuple[str, str]], tuple[str, str], tuple[str, str]]] = {
    "FileImports": ("edge_id", [
        ("edge_id", "str"), ("source_file", "str"), ("target", "str"),
        ("import", "str"), ("resolution", "str"),
    ], ("source_file", "Files"), ("target", "Files")),
    "FileDependsOn": ("edge_id", [
        ("edge_id", "str"), ("source_file", "str"), ("target_file", "str"),
        ("resolution", "str"),
    ], ("source_file", "Files"), ("target_file", "Files")),
    "ClassInherits": ("edge_id", [
        ("edge_id", "str"), ("child_class", "str"), ("parent_class", "str"),
        ("kind", "str"), ("resolution", "str"),
    ], ("child_class", "Classes"), ("parent_class", "Classes")),
    "MethodCalls": ("edge_id", [
        ("edge_id", "str"), ("caller_method", "str"), ("callee_method", "str"),
        ("callee_name", "str"), ("resolution", "str"), ("call_line", "int"),
    ], ("caller_method", "Methods"), ("callee_method", "Methods")),
    "PossiblyCalls": ("edge_id", [
        ("edge_id", "str"), ("caller_method", "str"), ("callee_method", "str"),
        ("callee_name", "str"), ("reason", "str"), ("candidate_count", "int"),
    ], ("caller_method", "Methods"), ("callee_method", "Methods")),
    "FileDefinesClass": ("edge_id", [
        ("edge_id", "str"), ("file_id", "str"), ("class_id", "str"),
    ], ("file_id", "Files"), ("class_id", "Classes")),
    "ClassDefinesMethod": ("edge_id", [
        ("edge_id", "str"), ("class_id", "str"), ("method_id", "str"),
    ], ("class_id", "Classes"), ("method_id", "Methods")),
    "FileBelongsToModule": ("edge_id", [
        ("edge_id", "str"), ("file_id", "str"), ("module_id", "str"),
    ], ("file_id", "Files"), ("module_id", "Modules")),
    "DirContainsFile": ("edge_id", [
        ("edge_id", "str"), ("dir_id", "str"), ("file_id", "str"),
    ], ("dir_id", "Directories"), ("file_id", "Files")),
    "TableReferences": ("edge_id", [
        ("edge_id", "str"), ("source_table", "str"), ("target_table", "str"),
        ("fk_column", "str"), ("referenced_column", "str"),
    ], ("source_table", "DbTables"), ("target_table", "DbTables")),
    "ClassMapsToTable": ("edge_id", [
        ("edge_id", "str"), ("class_id", "str"), ("table_id", "str"),
        ("via", "str"),
    ], ("class_id", "Classes"), ("table_id", "DbTables")),
}

NODE_TABLES = list(NODE_SPEC)
EDGE_TABLES = list(EDGE_SPEC)


def write_columns(table: str) -> list[str]:
    """Column names Phase 8/9 write for a table (embedding excluded — it is
    populated separately in Phase 10). This is what the pipeline imports so
    its inserts always match the DDL."""
    if table in NODE_SPEC:
        cols = NODE_SPEC[table][1]
    else:
        cols = EDGE_SPEC[table][1]
    return [c for c, _t in cols if c != "embedding"]


def _columns_sql(cols: list[tuple[str, str]]) -> str:
    return ",\n        ".join(f"{name:<12} {_TYPE_SQL[t]}" for name, t in cols)


def node_table_ddl() -> list[str]:
    out = []
    for name, (pk, cols) in NODE_SPEC.items():
        out.append(f"CREATE TABLE {name} (\n        {_columns_sql(cols)}\n    ) "
                   f"PRIMARY KEY ({pk})")
    return out


def edge_table_ddl() -> list[str]:
    out = []
    for name, (pk, cols, _src, _dst) in EDGE_SPEC.items():
        out.append(f"CREATE TABLE {name} (\n        {_columns_sql(cols)}\n    ) "
                   f"PRIMARY KEY ({pk})")
    return out


def graph_ddl() -> str:
    node_list = ",\n    ".join(NODE_TABLES)
    edge_defs = []
    for name, (_pk, _cols, (scol, stab), (dcol, dtab)) in EDGE_SPEC.items():
        spk = NODE_SPEC[stab][0]
        dpk = NODE_SPEC[dtab][0]
        edge_defs.append(
            f"{name}\n"
            f"      SOURCE KEY ({scol}) REFERENCES {stab} ({spk})\n"
            f"      DESTINATION KEY ({dcol}) REFERENCES {dtab} ({dpk})")
    edge_list = ",\n    ".join(edge_defs)
    return (f"CREATE OR REPLACE PROPERTY GRAPH {config.GRAPH_NAME}\n"
            f"  NODE TABLES (\n    {node_list}\n  )\n"
            f"  EDGE TABLES (\n    {edge_list}\n  )")


def _all_table_ddl() -> list[str]:
    return node_table_ddl() + edge_table_ddl()


# ===================================================================
# Instance / database lifecycle
# ===================================================================


def create_instance(project_id: str, instance_id: str, region: str):
    """Create Spanner Enterprise instance with 100 PU."""
    from google.cloud.spanner_admin_instance_v1.types import spanner_instance_admin

    client = spanner.Client(project=project_id, disable_builtin_metrics=True)

    instance = client.instance(instance_id)
    if instance.exists():
        print(f"  Instance '{instance_id}' already exists, skipping.")
        return

    config_name = f"projects/{project_id}/instanceConfigs/regional-{region}"
    admin_api = client.instance_admin_api
    instance_proto = spanner_instance_admin.Instance(
        name=f"projects/{project_id}/instances/{instance_id}",
        config=config_name,
        display_name="CodeDoc Graph RAG",
        processing_units=100,
        edition=spanner_instance_admin.Instance.Edition.ENTERPRISE,
    )

    print(f"  Creating instance '{instance_id}' (Enterprise, 100 PU, {region})...")
    operation = admin_api.create_instance(
        parent=f"projects/{project_id}",
        instance_id=instance_id,
        instance=instance_proto,
    )
    print("  Waiting for instance creation...")
    operation.result(timeout=300)
    print(f"  Instance '{instance_id}' created (Enterprise edition).")


def create_database(project_id: str, instance_id: str, database_id: str):
    """Create the database, or migrate an existing one to the current schema."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    instance = client.instance(instance_id)

    database = instance.database(database_id)
    if database.exists():
        print(f"  Database '{database_id}' already exists — migrating schema...")
        migrate_schema(database)
        _add_graphs(database)
        return

    table_ddl = _all_table_ddl()
    print(f"  Creating database '{database_id}' with {len(table_ddl)} tables...")
    database = instance.database(database_id, ddl_statements=table_ddl)
    operation = database.create()
    print("  Waiting for database creation...")
    operation.result(timeout=600)
    print(f"  Database '{database_id}' created with {len(table_ddl)} tables.")

    _add_graphs(database)


def _existing_tables(database) -> set[str]:
    with database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ''")
        return {r[0] for r in rows}


def _existing_columns(database, table: str) -> set[str]:
    with database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = '' AND TABLE_NAME = @t",
            params={"t": table}, param_types={"t": spanner.param_types.STRING})
        return {r[0] for r in rows}


def migrate_schema(database):
    """Idempotently add missing tables and columns to an existing database.

    Reads INFORMATION_SCHEMA and issues only the deltas (CREATE TABLE for new
    tables, ALTER TABLE ADD COLUMN for new columns) so upgrading a graph built
    by an older version never drops data. The property graph is refreshed
    separately via CREATE OR REPLACE.
    """
    existing = _existing_tables(database)
    ddl: list[str] = []

    # New tables
    all_specs = {**{n: NODE_SPEC[n][1] for n in NODE_SPEC},
                 **{n: EDGE_SPEC[n][1] for n in EDGE_SPEC}}
    create_map = {name: stmt for name, stmt in
                  zip(list(NODE_SPEC) + list(EDGE_SPEC), _all_table_ddl())}
    for name in list(NODE_SPEC) + list(EDGE_SPEC):
        if name not in existing:
            ddl.append(create_map[name])

    # New columns on existing tables
    for name, cols in all_specs.items():
        if name not in existing:
            continue
        have = _existing_columns(database, name)
        for col, typ in cols:
            if col not in have:
                ddl.append(f"ALTER TABLE {name} ADD COLUMN {col} {_TYPE_SQL[typ]}")

    if not ddl:
        print("  Schema already up to date — no table/column changes.")
        return
    print(f"  Applying {len(ddl)} schema change(s):")
    for stmt in ddl:
        print(f"    {stmt.splitlines()[0].strip()[:80]}")
    operation = database.update_ddl(ddl)
    operation.result(timeout=600)
    print("  Schema migration complete.")


def _add_graphs(database):
    """Add / refresh the property graph definition."""
    try:
        print("  Adding property graph definition...")
        operation = database.update_ddl([graph_ddl()])
        print("  Waiting for graph creation...")
        operation.result(timeout=300)
        print(f"  Property graph created: {config.GRAPH_NAME}")
    except gax_exceptions.GoogleAPICallError as e:
        print(f"  Graph creation note: {e}")


def destroy_all(project_id: str, instance_id: str, database_id: str):
    """Drop database and delete instance."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    instance = client.instance(instance_id)

    database = instance.database(database_id)
    if database.exists():
        print(f"  Dropping database '{database_id}'...")
        database.drop()
        print("  Database dropped.")

    if instance.exists():
        print(f"  Deleting instance '{instance_id}'...")
        instance.delete()
        print("  Instance deleted.")


def verify(project_id: str, instance_id: str, database_id: str):
    """Verify tables and graphs exist."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    instance = client.instance(instance_id)
    database = instance.database(database_id)

    with database.snapshot() as snapshot:
        results = snapshot.execute_sql(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = '' ORDER BY TABLE_NAME"
        )
        tables = [row[0] for row in results]
        print(f"  Tables ({len(tables)}): {', '.join(tables)}")
        expected = set(NODE_TABLES) | set(EDGE_TABLES)
        missing = expected - set(tables)
        if missing:
            print(f"  MISSING tables: {', '.join(sorted(missing))}")

    with database.snapshot() as snapshot:
        try:
            results = snapshot.execute_sql(
                "SELECT PROPERTY_GRAPH_NAME FROM INFORMATION_SCHEMA.PROPERTY_GRAPHS"
            )
            graphs = [row[0] for row in results]
            print(f"  Property graphs ({len(graphs)}): {', '.join(graphs)}")
        except Exception as e:
            print(f"  Property graphs: none yet ({e})")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m graph_generator.setup_spanner_graph",
        description="Setup Spanner Graph for CodeDoc",
    )
    parser.add_argument("--project", default=PROJECT_ID, help="GCP project ID")
    parser.add_argument("--region", default=REGION, help="Spanner region")
    parser.add_argument("--instance", default=INSTANCE_ID, help="Spanner instance ID")
    parser.add_argument("--database", default=DATABASE_ID, help="Spanner database ID")
    parser.add_argument("--skip-instance", action="store_true", help="Skip instance creation")
    parser.add_argument("--migrate", action="store_true",
                        help="Only add missing tables/columns to an existing database")
    parser.add_argument("--destroy", action="store_true", help="Tear down all resources")
    parser.add_argument("--verify", action="store_true", help="Verify setup")
    args = parser.parse_args()

    print(f"Project: {args.project}")
    print(f"Instance: {args.instance}")
    print(f"Database: {args.database}")
    print(f"Region: {args.region}")
    print()

    if args.destroy:
        print("DESTROYING all resources...")
        confirm = input("Type 'yes' to confirm: ")
        if confirm != "yes":
            print("Aborted.")
            return
        destroy_all(args.project, args.instance, args.database)
        return

    if args.verify:
        print("Verifying setup...")
        verify(args.project, args.instance, args.database)
        return

    if args.migrate:
        print("Migrating existing database schema...")
        client = spanner.Client(project=args.project, disable_builtin_metrics=True)
        database = client.instance(args.instance).database(args.database)
        migrate_schema(database)
        _add_graphs(database)
        return

    if not args.skip_instance:
        print("Step 1: Creating Spanner instance...")
        create_instance(args.project, args.instance, args.region)
    else:
        print("Step 1: Skipping instance creation (--skip-instance)")

    print("\nStep 2: Creating database with tables and graphs...")
    create_database(args.project, args.instance, args.database)

    print("\nStep 3: Verifying setup...")
    verify(args.project, args.instance, args.database)

    print("\nDone! Spanner Graph infrastructure is ready.")
    print(f"  Instance: {args.instance}")
    print(f"  Database: {args.database}")
    print(f"  Graph: {config.GRAPH_NAME}")


if __name__ == "__main__":
    main()
