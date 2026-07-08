"""
Setup script for Spanner Graph infrastructure.

Creates:
- Spanner Enterprise instance (codedoc-instance, 100 PU)
- Database (codedoc-db) with 13 tables + 1 property graph
- Tables: 5 node tables + 8 edge tables
- Property graph: code_graph_a

Usage:
    python setup_spanner_graph.py [--project PROJECT] [--region REGION]
    python setup_spanner_graph.py --skip-instance   # skip instance creation
    python setup_spanner_graph.py --destroy          # tear down everything
"""

import argparse
import os
import sys
import time

from google.cloud import spanner
from google.api_core import exceptions as gax_exceptions

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "claude-cws-498905")
INSTANCE_ID = os.environ.get("SPANNER_INSTANCE", "codedoc-instance")
DATABASE_ID =  os.environ.get("SPANNER_DATABASE", "codedoc-db")

REGION = "asia-northeast1"


# ===================================================================
# DDL Statements
# ===================================================================

NODE_TABLE_DDL = [
    """CREATE TABLE Files (
        file_id     STRING(MAX) NOT NULL,
        file_name   STRING(MAX),
        extension   STRING(MAX),
        directory   STRING(MAX),
        summary     STRING(MAX),
        embedding   ARRAY<FLOAT64>
    ) PRIMARY KEY (file_id)""",

    """CREATE TABLE Classes (
        class_id    STRING(MAX) NOT NULL,
        name        STRING(MAX),
        file_id     STRING(MAX),
        kind        STRING(MAX),
        modifiers   STRING(MAX),
        summary     STRING(MAX),
        embedding   ARRAY<FLOAT64>
    ) PRIMARY KEY (class_id)""",

    """CREATE TABLE Methods (
        method_id   STRING(MAX) NOT NULL,
        name        STRING(MAX),
        class_id    STRING(MAX),
        file_id     STRING(MAX),
        signature   STRING(MAX),
        modifiers   STRING(MAX),
        return_type STRING(MAX),
        summary     STRING(MAX),
        embedding   ARRAY<FLOAT64>
    ) PRIMARY KEY (method_id)""",

    """CREATE TABLE Modules (
        module_id   STRING(MAX) NOT NULL,
        name        STRING(MAX),
        summary     STRING(MAX),
        embedding   ARRAY<FLOAT64>
    ) PRIMARY KEY (module_id)""",

    """CREATE TABLE Directories (
        dir_id      STRING(MAX) NOT NULL,
        name        STRING(MAX),
        summary     STRING(MAX),
        embedding   ARRAY<FLOAT64>
    ) PRIMARY KEY (dir_id)""",
]

EDGE_TABLE_DDL = [
    """CREATE TABLE FileImports (
        edge_id     STRING(MAX) NOT NULL,
        source_file STRING(MAX) NOT NULL,
        target      STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE FileDependsOn (
        edge_id     STRING(MAX) NOT NULL,
        source_file STRING(MAX) NOT NULL,
        target_file STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE ClassInherits (
        edge_id       STRING(MAX) NOT NULL,
        child_class   STRING(MAX) NOT NULL,
        parent_class  STRING(MAX) NOT NULL,
        kind          STRING(MAX)
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE MethodCalls (
        edge_id         STRING(MAX) NOT NULL,
        caller_method   STRING(MAX) NOT NULL,
        callee_method   STRING(MAX) NOT NULL,
        callee_name     STRING(MAX)
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE FileDefinesClass (
        edge_id     STRING(MAX) NOT NULL,
        file_id     STRING(MAX) NOT NULL,
        class_id    STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE ClassDefinesMethod (
        edge_id     STRING(MAX) NOT NULL,
        class_id    STRING(MAX) NOT NULL,
        method_id   STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE FileBelongsToModule (
        edge_id     STRING(MAX) NOT NULL,
        file_id     STRING(MAX) NOT NULL,
        module_id   STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",

    """CREATE TABLE DirContainsFile (
        edge_id     STRING(MAX) NOT NULL,
        dir_id      STRING(MAX) NOT NULL,
        file_id     STRING(MAX) NOT NULL
    ) PRIMARY KEY (edge_id)""",
]

GRAPH_DDL_TEMPLATE = """CREATE OR REPLACE PROPERTY GRAPH {graph_name}
  NODE TABLES (
    Files,
    Classes,
    Methods,
    Modules,
    Directories
  )
  EDGE TABLES (
    FileImports
      SOURCE KEY (source_file) REFERENCES Files (file_id)
      DESTINATION KEY (target) REFERENCES Files (file_id),
    FileDependsOn
      SOURCE KEY (source_file) REFERENCES Files (file_id)
      DESTINATION KEY (target_file) REFERENCES Files (file_id),
    ClassInherits
      SOURCE KEY (child_class) REFERENCES Classes (class_id)
      DESTINATION KEY (parent_class) REFERENCES Classes (class_id),
    MethodCalls
      SOURCE KEY (caller_method) REFERENCES Methods (method_id)
      DESTINATION KEY (callee_method) REFERENCES Methods (method_id),
    FileDefinesClass
      SOURCE KEY (file_id) REFERENCES Files (file_id)
      DESTINATION KEY (class_id) REFERENCES Classes (class_id),
    ClassDefinesMethod
      SOURCE KEY (class_id) REFERENCES Classes (class_id)
      DESTINATION KEY (method_id) REFERENCES Methods (method_id),
    FileBelongsToModule
      SOURCE KEY (file_id) REFERENCES Files (file_id)
      DESTINATION KEY (module_id) REFERENCES Modules (module_id),
    DirContainsFile
      SOURCE KEY (dir_id) REFERENCES Directories (dir_id)
      DESTINATION KEY (file_id) REFERENCES Files (file_id)
  )"""


def create_instance(project_id: str, instance_id: str, region: str):
    """Create Spanner Enterprise instance with 100 PU."""
    from google.cloud.spanner_admin_instance_v1.types import spanner_instance_admin

    client = spanner.Client(project=project_id, disable_builtin_metrics=True)

    # Check if already exists
    instance = client.instance(instance_id)
    if instance.exists():
        print(f"  Instance '{instance_id}' already exists, skipping.")
        return

    config_name = f"projects/{project_id}/instanceConfigs/regional-{region}"

    # Use the admin API directly to set Enterprise edition
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
    """Create database with all tables and property graphs."""
    client = spanner.Client(project=project_id, disable_builtin_metrics=True)
    instance = client.instance(instance_id)

    # Check if database exists
    database = instance.database(database_id)
    if database.exists():
        print(f"  Database '{database_id}' already exists.")
        print("  Adding property graphs...")
        _add_graphs(database)
        return

    # Step 1: Create database with table DDL only (no graphs yet)
    table_ddl = NODE_TABLE_DDL + EDGE_TABLE_DDL
    print(f"  Creating database '{database_id}' with {len(table_ddl)} table DDL statements...")
    database = instance.database(database_id, ddl_statements=table_ddl)
    operation = database.create()
    print("  Waiting for database creation...")
    operation.result(timeout=600)
    print(f"  Database '{database_id}' created with {len(table_ddl)} tables.")

    # Step 2: Add property graphs via update_ddl
    _add_graphs(database)


def _add_graphs(database):
    """Add property graph definitions to existing database."""
    graph_ddl = [
        GRAPH_DDL_TEMPLATE.format(graph_name="code_graph_a"),
    ]

    try:
        print("  Adding property graph definitions...")
        operation = database.update_ddl(graph_ddl)
        print("  Waiting for graph creation...")
        operation.result(timeout=300)
        print("  Property graph created: code_graph_a")
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

    # Check tables
    with database.snapshot() as snapshot:
        results = snapshot.execute_sql(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = '' ORDER BY TABLE_NAME"
        )
        tables = [row[0] for row in results]
        print(f"  Tables ({len(tables)}): {', '.join(tables)}")

    # Check property graphs
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
    parser = argparse.ArgumentParser(description="Setup Spanner Graph for CodeDoc")
    parser.add_argument("--project", default=PROJECT_ID, help="GCP project ID")
    parser.add_argument("--region", default=REGION, help="Spanner region")
    parser.add_argument("--instance", default=INSTANCE_ID, help="Spanner instance ID")
    parser.add_argument("--database", default=DATABASE_ID, help="Spanner database ID")
    parser.add_argument("--skip-instance", action="store_true", help="Skip instance creation")
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
    print(f"  Graph: code_graph_a")


if __name__ == "__main__":
    main()
