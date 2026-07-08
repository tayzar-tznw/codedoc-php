"""Entry point: python -m graph_generator <command> [args]

Commands:
    init                        Interactive .env setup
    setup spanner               Create Spanner instance + database + tables + graph

    analyze <target_dir>        Run full pipeline: docs + graph (Phases 1-10)
    generate wiki <target_dir>  Generate documentation only (Phases 1-6)
    generate graph <target_dir> Generate Spanner graph only (Phases 8-10)

    upload graph <target_dir>   Upload graph data to Spanner

    validate                    Validate Spanner graph data
"""

import argparse
import asyncio
import json
import os
import sys
import time

from .pipeline import run_pipeline, run_docs_pipeline, run_graph_pipeline, PipelineData, _get_spanner_db, _print
from . import config
import pickle


def _print_timing_report(data, total_elapsed: float):
    """Print timing report and statistics."""
    print("\n" + "=" * 70)
    print("  TIMING REPORT")
    print("=" * 70)

    phase_order = [
        ("phase1_scan", "Phase 1: File Scanning"),
        ("phase1b_treesitter", "Phase 1.5: Tree-sitter Entities"),
        ("phase2_file_summaries", "Phase 2: File Summaries"),
        ("phase3_dir_summaries", "Phase 3: Dir Summaries"),
        ("phase4_topics", "Phase 4: Topic Extraction"),
        ("phase5_topic_summaries", "Phase 5: Topic Summaries"),
        ("phase6_index", "Phase 6: Index Assembly"),
        ("phase8_write_nodes", "Phase 8: Write Graph Nodes"),
        ("phase9_write_edges", "Phase 9: Write Graph Edges"),
        ("phase10_embeddings", "Phase 10: Generate Embeddings"),
    ]

    for key, label in phase_order:
        if key in data.timings:
            t = data.timings[key]
            mins = int(t // 60)
            secs = t % 60
            if mins > 0:
                print(f"  {label:<35} {mins}m {secs:05.2f}s")
            else:
                print(f"  {label:<35} {secs:.2f}s")

    print("  " + "-" * 45)
    total_mins = int(total_elapsed // 60)
    total_secs = total_elapsed % 60
    if total_mins > 0:
        print(f"  {'TOTAL':<35} {total_mins}m {total_secs:05.2f}s")
    else:
        print(f"  {'TOTAL':<35} {total_secs:.2f}s")
    print("=" * 70)

    print(f"\n  Files summarized:    {len(data.file_summaries)}")
    print(f"  Dirs summarized:     {len(data.dir_summaries)}")
    print(f"  Topics extracted:    {len(data.topics)}")
    print(f"  Entities extracted:  {len(data.extracted_entities)}")
    print(f"  Graph nodes:         files={len(data.file_id_map)}, classes={len(data.class_id_map)}, methods={len(data.method_id_map)}")
    print(f"  Graph modules:       {len(data.module_id_map)}, dirs={len(data.dir_id_map)}")

    # Save timing report
    out_root = os.path.join(os.getcwd(), config.OUTPUT_DIR)
    os.makedirs(out_root, exist_ok=True)
    report = {
        "target_dir": data.target_dir,
        "model": config.MODEL,
        "concurrency": config.GEMINI_CONCURRENCY,
        "total_seconds": total_elapsed,
        "timings": data.timings,
        "statistics": {
            "files_summarized": len(data.file_summaries),
            "dirs_summarized": len(data.dir_summaries),
            "topics_extracted": len(data.topics),
            "entities_extracted": len(data.extracted_entities),
            "graph_files": len(data.file_id_map),
            "graph_classes": len(data.class_id_map),
            "graph_methods": len(data.method_id_map),
            "graph_modules": len(data.module_id_map),
            "graph_dirs": len(data.dir_id_map),
        },
    }
    report_path = os.path.join(out_root, "timing_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Timing report saved: {report_path}")


def _pipeline_data_path() -> str:
    return os.path.join(os.getcwd(), config.OUTPUT_DIR, "pipeline_data.pkl")


def _save_pipeline_data(data: PipelineData):
    path = _pipeline_data_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"  Pipeline data saved: {path}")


def _load_pipeline_data(target_dir: str) -> PipelineData | None:
    path = _pipeline_data_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _check_config():
    """Validate required config before running."""
    if not config.GCP_PROJECT:
        print("Error: GOOGLE_CLOUD_PROJECT is not set.")
        print("  Run: python -m graph_generator init")
        print("  Or set it in .env")
        sys.exit(1)


def cmd_init(args):
    """Interactive setup — generate .env from template."""
    env_path = os.path.join(os.getcwd(), ".env")
    example_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")

    if os.path.exists(env_path) and not getattr(args, "force", False):
        print(f".env already exists at {env_path}")
        print("  Use --force to overwrite")
        return

    print("=" * 60)
    print("  CodeDoc Setup")
    print("=" * 60)

    project = input(f"  GCP Project ID [{config.GCP_PROJECT or 'your-project-id'}]: ").strip()
    project = project or config.GCP_PROJECT or "your-project-id"

    instance = input(f"  Spanner Instance [{config.SPANNER_INSTANCE}]: ").strip()
    instance = instance or config.SPANNER_INSTANCE

    database = input(f"  Spanner Database [{config.SPANNER_DATABASE}]: ").strip()
    database = database or config.SPANNER_DATABASE

    lines = [
        "# CodeDoc Configuration (auto-generated by 'init')",
        f"GOOGLE_CLOUD_PROJECT={project}",
        "GOOGLE_GENAI_USE_VERTEXAI=true",
        "",
        f"SPANNER_INSTANCE={instance}",
        f"SPANNER_DATABASE={database}",
        "",
        "# Uncomment to fix SSL issues on GCE:",
        "# GCE_METADATA_MTLS_MODE=none",
    ]

    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  .env written to {env_path}")
    print(f"  You can now run: python -m graph_generator analyze <dir>")


def _print_banner(label: str, target_dir: str):
    print("=" * 70)
    print(f"  CodeDoc — {label}")
    print("=" * 70)
    print(f"  Target:      {os.path.abspath(target_dir)}")
    print(f"  Model:       {config.MODEL}")
    print(f"  Concurrency: {config.GEMINI_CONCURRENCY}")
    print(f"  Output:      {config.OUTPUT_DIR}")
    print("=" * 70)


def cmd_docs(args):
    """Run docs generation pipeline only (Phases 1-6)."""
    _check_config()
    target_dir = args.target_dir
    if not os.path.isdir(target_dir):
        print(f"Error: Directory not found: {target_dir}")
        sys.exit(1)

    _print_banner("Docs Generation (Phases 1-6)", target_dir)
    total_start = time.time()
    data = asyncio.run(run_docs_pipeline(target_dir))
    total_elapsed = time.time() - total_start
    _print_timing_report(data, total_elapsed)

    # Save data for graph pipeline to pick up
    _save_pipeline_data(data)


def cmd_graph(args):
    """Run Spanner graph pipeline only (Phases 1, 1b, 8-10).

    Tries to load saved pipeline data first. If not available,
    runs Phase 1 (scan) and Phase 1b (tree-sitter) to build the
    structural data, and loads any existing summaries from disk.
    """
    _check_config()
    from .pipeline import phase1_scan, phase1b_treesitter_entities, _load_summaries_from_disk

    target_dir = args.target_dir
    if not os.path.isdir(target_dir):
        print(f"Error: Directory not found: {target_dir}")
        sys.exit(1)

    print("=" * 70)
    print("  CodeDoc — Graph Generation")
    print("=" * 70)
    print(f"  Target:   {os.path.abspath(target_dir)}")
    print(f"  Spanner:  {config.SPANNER_INSTANCE}/{config.SPANNER_DATABASE}")
    print("=" * 70)

    total_start = time.time()

    data = _load_pipeline_data(target_dir)
    if data is not None:
        print(f"  Loaded saved pipeline data ({len(data.file_summaries)} summaries, "
              f"{len(data.extracted_entities)} entities)")
    else:
        print("  No saved pipeline data — running scan + tree-sitter...")
        data = PipelineData(target_dir=os.path.abspath(target_dir))
        phase1_scan(data)
        phase1b_treesitter_entities(data)
        _load_summaries_from_disk(data)

    print()
    run_graph_pipeline(data)
    total_elapsed = time.time() - total_start
    _print_timing_report(data, total_elapsed)


def cmd_run(args):
    """Run the full pipeline (docs + graph)."""
    target_dir = args.target_dir
    if not os.path.isdir(target_dir):
        print(f"Error: Directory not found: {target_dir}")
        sys.exit(1)

    _print_banner("Full Pipeline (Phases 1-10)", target_dir)
    total_start = time.time()
    data = asyncio.run(run_pipeline(target_dir))
    total_elapsed = time.time() - total_start
    _print_timing_report(data, total_elapsed)


def cmd_upload_graph(args):
    """Upload graph data (alias for generate graph)."""
    cmd_graph(args)


def cmd_analyze(args):
    """Run full pipeline: docs + graph."""
    cmd_docs(args)
    data = _load_pipeline_data(args.target_dir)
    if data:
        run_graph_pipeline(data)
        _print_timing_report(data, time.time())


def cmd_setup_spanner(args):
    """Create Spanner instance, database, tables, and property graph."""
    _check_config()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from setup_spanner_graph import create_instance, create_database, verify

    project = config.GCP_PROJECT
    instance = getattr(args, "instance", None) or config.SPANNER_INSTANCE
    database = getattr(args, "database", None) or config.SPANNER_DATABASE
    region = getattr(args, "region", None) or "asia-northeast1"

    print("=" * 60)
    print("  Setup Spanner Graph")
    print("=" * 60)
    print(f"  Project:  {project}")
    print(f"  Instance: {instance}")
    print(f"  Database: {database}")
    print(f"  Region:   {region}")
    print("=" * 60)

    if not getattr(args, "skip_instance", False):
        print("\nStep 1: Creating Spanner instance...")
        create_instance(project, instance, region)
    else:
        print("\nStep 1: Skipping instance creation (--skip-instance)")

    print("\nStep 2: Creating database + tables + property graph...")
    create_database(project, instance, database)

    print("\nStep 3: Verifying...")
    verify(project, instance, database)
    print("\nSpanner setup complete.")


def cmd_validate(args):
    """Validate Spanner graph data against expected counts."""
    db = _get_spanner_db()
    print("=" * 70)
    print("  GRAPH VALIDATION")
    print("=" * 70)

    tables = ["Files", "Classes", "Methods", "Modules", "Directories",
              "FileDependsOn", "ClassInherits", "MethodCalls",
              "FileDefinesClass", "ClassDefinesMethod",
              "FileBelongsToModule", "DirContainsFile"]

    for table in tables:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(f"SELECT COUNT(*) FROM {table}"))
            count = rows[0][0] if rows else 0
            print(f"  {table:<25} {count:>10,}")

    # Check for orphaned edges
    print("\n  Orphan checks:")
    orphan_queries = [
        ("FileDependsOn → Files", """
            SELECT COUNT(*) FROM FileDependsOn e
            WHERE NOT EXISTS (SELECT 1 FROM Files f WHERE f.file_id = e.source_file)
               OR NOT EXISTS (SELECT 1 FROM Files f WHERE f.file_id = e.target_file)
        """),
        ("ClassInherits → Classes", """
            SELECT COUNT(*) FROM ClassInherits e
            WHERE NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.child_class)
               OR NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.parent_class)
        """),
    ]
    for label, query in orphan_queries:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(query))
            count = rows[0][0] if rows else 0
            status = "OK" if count == 0 else f"WARNING: {count} orphans"
            print(f"  {label:<35} {status}")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m graph_generator",
        description="CodeDoc: Generate documentation from source code using Gemini LLM",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    p_init = subparsers.add_parser("init", help="Setup: generate .env configuration")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .env")

    # analyze — full pipeline
    p = subparsers.add_parser("analyze", help="Run full pipeline: docs + graph (Phases 1-10)")
    p.add_argument("target_dir", help="Source code directory")

    # generate — subcommands
    p_gen = subparsers.add_parser("generate", help="Generate docs or graph")
    gen_sub = p_gen.add_subparsers(dest="gen_command")
    p_gw = gen_sub.add_parser("wiki", help="Generate documentation (Phases 1-6)")
    p_gw.add_argument("target_dir", help="Source code directory")
    p_gg = gen_sub.add_parser("graph", help="Generate Spanner graph (Phases 8-10)")
    p_gg.add_argument("target_dir", help="Source code directory")

    # upload — subcommands
    p_up = subparsers.add_parser("upload", help="Upload graph data to Spanner")
    up_sub = p_up.add_subparsers(dest="up_command")
    p_ug = up_sub.add_parser("graph", help="Upload graph data to Spanner")
    p_ug.add_argument("target_dir", help="Source code directory")

    # setup — subcommands
    p_setup = subparsers.add_parser("setup", help="Create Spanner resources")
    setup_sub = p_setup.add_subparsers(dest="setup_command")
    p_ss = setup_sub.add_parser("spanner", help="Create Spanner instance + database + tables + graph")
    p_ss.add_argument("--instance", help="Spanner instance ID")
    p_ss.add_argument("--database", help="Spanner database ID")
    p_ss.add_argument("--region", help="Spanner region (default: asia-northeast1)")
    p_ss.add_argument("--skip-instance", action="store_true", help="Skip instance creation")

    # validate
    subparsers.add_parser("validate", help="Validate Spanner graph data")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "setup":
        if args.setup_command == "spanner":
            cmd_setup_spanner(args)
        else:
            p_setup.print_help()
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "generate":
        if args.gen_command == "wiki":
            cmd_docs(args)
        elif args.gen_command == "graph":
            cmd_graph(args)
        else:
            p_gen.print_help()
    elif args.command == "upload":
        if args.up_command == "graph":
            cmd_upload_graph(args)
        else:
            p_up.print_help()
    elif args.command == "validate":
        cmd_validate(args)


if __name__ == "__main__":
    main()
