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
import contextlib
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
        ("phase1c_lsp_resolution", "Phase 1.6: LSP Resolution"),
        ("phase1d_db_schema", "Phase 1.7: DB Schema (migrations)"),
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
            data = pickle.load(f)
        # Pickle restores __dict__ only — a pkl written by an older version
        # lacks fields added since (resolutions, file_origins, ...). Backfill
        # dataclass defaults so attribute access doesn't AttributeError.
        import dataclasses
        for f in dataclasses.fields(PipelineData):
            if not hasattr(data, f.name):
                if f.default is not dataclasses.MISSING:
                    setattr(data, f.name, f.default)
                elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                    setattr(data, f.name, f.default_factory())
        return data
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


def _resolve_include_vendor(target_dir, args) -> bool:
    """Decide whether vendor/ files join the graph.

    Precedence: --include-vendor / --exclude-vendor flags → INCLUDE_VENDOR env
    → interactive prompt (only when vendor files exist and stdin is a TTY) →
    default exclude. The prompt reports vendor file count + total size first.
    """
    from .pipeline import vendor_stats

    if getattr(args, "include_vendor", False):
        return True
    if getattr(args, "exclude_vendor", False):
        return False
    env = config.INCLUDE_VENDOR.strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False

    stats = vendor_stats(target_dir)
    if stats["files"] == 0:
        return False
    mb = stats["bytes"] / (1024 * 1024)
    print(f"\n  Found {stats['files']} vendor PHP files ({mb:.1f} MB) under this target.")
    print("  Including them adds graph nodes/edges marked origin='vendor'")
    print("  (calls into vendor still resolve); they are excluded from the")
    print("  generated docs and embeddings to control cost.")
    if not sys.stdin.isatty():
        print("  Non-interactive session -> excluding vendor "
              "(pass --include-vendor to include).")
        return False
    answer = input("  Include vendor files in the graph? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


# ── Multi-repo helpers ────────────────────────────────────────────

def _resolve_repo_name(target_dir, args) -> str:
    """Repo name for a target: --repo-name if given, else the dir basename."""
    name = getattr(args, "repo_name", None)
    if name:
        return name
    return os.path.basename(os.path.abspath(target_dir).rstrip(os.sep)) or "repo"


@contextlib.contextmanager
def _repo_output_dir(repo):
    """Isolate a repo's checkpoints/outputs under OUTPUT_DIR/repos/<repo> so
    repos ingested into the same graph never clobber each other's state."""
    base = config.OUTPUT_DIR
    config.OUTPUT_DIR = os.path.join(base, "repos", repo)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    try:
        yield
    finally:
        config.OUTPUT_DIR = base


def _load_repos_manifest(path):
    """Parse a --repos manifest: a JSON list of {name?, path, include_vendor?}
    (bare path strings also accepted). Returns [(name, abs_path, include_vendor)]."""
    with open(path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    out = []
    for e in entries:
        if isinstance(e, str):
            p = e
            name, inc = None, None
        else:
            p = e["path"]
            name = e.get("name")
            inc = e.get("include_vendor")
        p_abs = os.path.abspath(p)
        name = name or os.path.basename(p_abs.rstrip(os.sep)) or "repo"
        out.append((name, p_abs, inc))
    return out


def _iter_repos(args):
    """Yield (repo_name, abs_path, include_vendor) for a command — either the
    single positional target or each entry of a --repos manifest."""
    if getattr(args, "repos", None):
        for name, path, inc in _load_repos_manifest(args.repos):
            if inc is None:
                inc = _resolve_include_vendor(path, args)
            yield name, path, inc
        return
    target = getattr(args, "target_dir", None)
    if not target:
        print("Error: provide a target directory, or --repos <manifest.json>")
        sys.exit(1)
    yield (_resolve_repo_name(target, args), os.path.abspath(target),
           _resolve_include_vendor(target, args))


def cmd_docs(args):
    """Run docs generation pipeline only (Phases 1-6), per repo."""
    _check_config()
    for repo, target_dir, include_vendor in _iter_repos(args):
        if not os.path.isdir(target_dir):
            print(f"Error: Directory not found: {target_dir}")
            sys.exit(1)
        with _repo_output_dir(repo):
            _print_banner(f"Docs Generation (repo: {repo})", target_dir)
            total_start = time.time()
            data = asyncio.run(run_docs_pipeline(
                target_dir, include_vendor=include_vendor, repo=repo))
            _print_timing_report(data, time.time() - total_start)
            _save_pipeline_data(data)  # for a later `generate graph`


def cmd_graph(args):
    """Run Spanner graph pipeline only (Phases 1, 1b, 8-10).

    Tries to load saved pipeline data first. If not available,
    runs Phase 1 (scan) and Phase 1b (tree-sitter) to build the
    structural data, and loads any existing summaries from disk.
    """
    _check_config()
    from .pipeline import phase1_scan, phase1b_treesitter_entities, _load_summaries_from_disk

    for repo, target_dir, include_vendor in _iter_repos(args):
        if not os.path.isdir(target_dir):
            print(f"Error: Directory not found: {target_dir}")
            sys.exit(1)
        with _repo_output_dir(repo):
            print("=" * 70)
            print("  CodeDoc — Graph Generation")
            print("=" * 70)
            print(f"  Repo:     {repo}")
            print(f"  Target:   {target_dir}")
            print(f"  Spanner:  {config.SPANNER_INSTANCE}/{config.SPANNER_DATABASE}")
            print("=" * 70)

            total_start = time.time()
            data = _load_pipeline_data(target_dir)
            if data is not None:
                if not data.repo:
                    data.repo = repo
                print(f"  Loaded saved pipeline data ({len(data.file_summaries)} summaries, "
                      f"{len(data.extracted_entities)} entities)")
            else:
                print("  No saved pipeline data — running scan + tree-sitter...")
                data = PipelineData(target_dir=target_dir)
                data.repo = repo
                data.include_vendor = include_vendor
                phase1_scan(data)
                phase1b_treesitter_entities(data)
                _load_summaries_from_disk(data)

            print()
            run_graph_pipeline(data)
            _print_timing_report(data, time.time() - total_start)


def cmd_run(args):
    """Run the full pipeline (docs + graph)."""
    target_dir = args.target_dir
    if not os.path.isdir(target_dir):
        print(f"Error: Directory not found: {target_dir}")
        sys.exit(1)

    include_vendor = _resolve_include_vendor(target_dir, args)
    _print_banner("Full Pipeline (Phases 1-10)", target_dir)
    total_start = time.time()
    data = asyncio.run(run_pipeline(target_dir, include_vendor=include_vendor))
    total_elapsed = time.time() - total_start
    _print_timing_report(data, total_elapsed)


def cmd_upload_graph(args):
    """Upload graph data (alias for generate graph)."""
    cmd_graph(args)


def cmd_analyze(args):
    """Run full pipeline (docs + graph) for each repo, into the shared graph."""
    _check_config()
    for repo, target_dir, include_vendor in _iter_repos(args):
        if not os.path.isdir(target_dir):
            print(f"Error: Directory not found: {target_dir}")
            sys.exit(1)
        with _repo_output_dir(repo):
            _print_banner(f"Full Pipeline (repo: {repo})", target_dir)
            total_start = time.time()
            data = asyncio.run(run_docs_pipeline(
                target_dir, include_vendor=include_vendor, repo=repo))
            _save_pipeline_data(data)
            run_graph_pipeline(data)
            _print_timing_report(data, time.time() - total_start)


def cmd_setup_spanner(args):
    """Create Spanner instance, database, tables, and property graph."""
    _check_config()
    from .setup_spanner_graph import create_instance, create_database, verify

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


def cmd_crossref(args):
    """Derive + write cross-repo dependency edges and print the coupling matrix."""
    _check_config()
    from .crossref import run_crossref
    print("=" * 70)
    print("  CodeDoc — Cross-Repository Analysis")
    print("=" * 70)
    run_crossref(printer=print, write=not getattr(args, "dry_run", False))
    print("=" * 70)


def cmd_evaluate(args):
    """Evaluate the extractor + resolver against the committed fixtures."""
    from .evaluate import run_evaluation

    fixtures = (["php_plain", "php_cakephp"] if args.fixture == "all"
                else [args.fixture])
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = run_evaluation(fixtures, repo_root,
                          dump_edges=getattr(args, "dump_edges", False))
    sys.exit(code)


def cmd_validate(args):
    """Validate Spanner graph data against expected counts."""
    db = _get_spanner_db()
    print("=" * 70)
    print("  GRAPH VALIDATION")
    print("=" * 70)

    from .setup_spanner_graph import NODE_TABLES, EDGE_TABLES
    tables = NODE_TABLES + EDGE_TABLES

    for table in tables:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(f"SELECT COUNT(*) FROM {table}"))
            count = rows[0][0] if rows else 0
            print(f"  {table:<25} {count:>10,}")

    # Per-repo node counts — surfaces an accidental cross-repo merge (a repo
    # missing, or two repos sharing a name) at a glance.
    print("\n  Nodes by repo:")
    for table in NODE_TABLES:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(
                f"SELECT repo, COUNT(*) AS n FROM {table} GROUP BY repo ORDER BY repo"))
            if rows:
                summary = ", ".join(f"{r[0] or '(none)'}={r[1]:,}" for r in rows)
                print(f"  {table:<25} {summary}")

    # Check for orphaned edges (both endpoints must resolve to a node). The
    # resolution-driven Phase 9 should make MethodCalls/PossiblyCalls/
    # TableReferences orphan-free too — surface any that aren't.
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
        ("MethodCalls → Methods", """
            SELECT COUNT(*) FROM MethodCalls e
            WHERE NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.caller_method)
               OR NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.callee_method)
        """),
        ("PossiblyCalls → Methods", """
            SELECT COUNT(*) FROM PossiblyCalls e
            WHERE NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.caller_method)
               OR NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.callee_method)
        """),
        ("FileImports → Files", """
            SELECT COUNT(*) FROM FileImports e
            WHERE NOT EXISTS (SELECT 1 FROM Files f WHERE f.file_id = e.source_file)
               OR NOT EXISTS (SELECT 1 FROM Files f WHERE f.file_id = e.target)
        """),
        ("TableReferences → DbTables", """
            SELECT COUNT(*) FROM TableReferences e
            WHERE NOT EXISTS (SELECT 1 FROM DbTables t WHERE t.table_id = e.source_table)
               OR NOT EXISTS (SELECT 1 FROM DbTables t WHERE t.table_id = e.target_table)
        """),
        ("ClassMapsToTable → nodes", """
            SELECT COUNT(*) FROM ClassMapsToTable e
            WHERE NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.class_id)
               OR NOT EXISTS (SELECT 1 FROM DbTables t WHERE t.table_id = e.table_id)
        """),
        ("CrossRepoRef → Classes", """
            SELECT COUNT(*) FROM CrossRepoRef e
            WHERE NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.source_class)
               OR NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.target_class)
        """),
        ("CrossRepoFileRef → nodes", """
            SELECT COUNT(*) FROM CrossRepoFileRef e
            WHERE NOT EXISTS (SELECT 1 FROM Files f WHERE f.file_id = e.source_file)
               OR NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.target_class)
        """),
        ("CrossRepoCalls → Methods", """
            SELECT COUNT(*) FROM CrossRepoCalls e
            WHERE NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.caller_method)
               OR NOT EXISTS (SELECT 1 FROM Methods m WHERE m.method_id = e.callee_method)
        """),
        ("DiBinds → Classes", """
            SELECT COUNT(*) FROM DiBinds e
            WHERE NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.source_class)
               OR NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.target_class)
        """),
        ("DiInjects → Classes", """
            SELECT COUNT(*) FROM DiInjects e
            WHERE NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.source_class)
               OR NOT EXISTS (SELECT 1 FROM Classes c WHERE c.class_id = e.target_class)
        """),
    ]
    for label, query in orphan_queries:
        with db.snapshot() as snap:
            rows = list(snap.execute_sql(query))
            count = rows[0][0] if rows else 0
            status = "OK" if count == 0 else f"WARNING: {count} orphans"
            print(f"  {label:<35} {status}")

    # Cross-repo coupling matrix (source_repo → target_repo : ref/call/di counts)
    print("\n  Cross-repo coupling:")
    coupling: dict[tuple, dict] = {}
    for tbl, key in (("CrossRepoRef", "refs"), ("CrossRepoFileRef", "refs"),
                     ("CrossRepoCalls", "calls"),
                     ("DiBinds", "di"), ("DiInjects", "di")):
        with db.snapshot() as snap:
            for row in snap.execute_sql(
                    f"SELECT source_repo, target_repo, COUNT(*) FROM {tbl} "
                    "GROUP BY source_repo, target_repo"):
                if len(row) >= 3:
                    c = coupling.setdefault((row[0], row[1]),
                                            {"refs": 0, "calls": 0, "di": 0})
                    c[key] = c.get(key, 0) + row[2]  # DiBinds+DiInjects both → di
    if coupling:
        for (src, tgt) in sorted(coupling):
            c = coupling[(src, tgt)]
            print(f"    {src} → {tgt} :  {c['refs']} refs / {c['calls']} calls / {c['di']} di")
    else:
        print("    (none — run `crossref` after ingesting 2+ repos)")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m graph_generator",
        description="CodeDoc: Generate documentation from source code using Gemini LLM",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    def _add_vendor_flags(p):
        p.add_argument("--include-vendor", action="store_true",
                       help="Include vendor/ files in the graph (skip the prompt)")
        p.add_argument("--exclude-vendor", action="store_true",
                       help="Exclude vendor/ files (skip the prompt)")

    def _add_repo_flags(p):
        # target_dir optional so a run can be driven entirely by --repos manifest
        p.add_argument("target_dir", nargs="?", help="Source code directory (one repo)")
        p.add_argument("--repo-name", help="Repository name in the graph (default: dir basename)")
        p.add_argument("--repos", metavar="MANIFEST",
                       help="JSON manifest of repos [{name?, path, include_vendor?}] "
                            "to ingest into the same graph")

    # init
    p_init = subparsers.add_parser("init", help="Setup: generate .env configuration")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing .env")

    # analyze — full pipeline
    p = subparsers.add_parser("analyze", help="Run full pipeline: docs + graph (Phases 1-10)")
    _add_repo_flags(p)
    _add_vendor_flags(p)

    # generate — subcommands
    p_gen = subparsers.add_parser("generate", help="Generate docs or graph")
    gen_sub = p_gen.add_subparsers(dest="gen_command")
    p_gw = gen_sub.add_parser("wiki", help="Generate documentation (Phases 1-6)")
    _add_repo_flags(p_gw)
    _add_vendor_flags(p_gw)
    p_gg = gen_sub.add_parser("graph", help="Generate Spanner graph (Phases 8-10)")
    _add_repo_flags(p_gg)
    _add_vendor_flags(p_gg)

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

    # crossref — cross-repository dependency + coupling analysis
    p_xr = subparsers.add_parser(
        "crossref", help="Analyze cross-repo dependencies + coupling (run after ingesting repos)")
    p_xr.add_argument("--dry-run", action="store_true",
                      help="Derive + print the coupling matrix without writing edges")

    # --- evaluate (local, no GCP) ---
    p_eval = subparsers.add_parser(
        "evaluate", help="Evaluate extractor+resolver against test_codes/ fixtures (local, no GCP)")
    p_eval.add_argument("--fixture", choices=["php_plain", "php_cakephp", "all"],
                        default="all", help="Fixture to evaluate (default: all)")
    p_eval.add_argument("--dump-edges", action="store_true",
                        help="Include derived MethodCalls/PossiblyCalls/ClassInherits in the report dict")
    p_eval.add_argument("--quiet-misses", action="store_true",
                        help="Hide per-case miss details")

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
    elif args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "crossref":
        cmd_crossref(args)
    elif args.command == "validate":
        cmd_validate(args)


if __name__ == "__main__":
    main()
