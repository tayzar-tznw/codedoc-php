# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

CodeDoc ingests a **PHP / CakePHP** codebase, generates Japanese AI documentation with Gemini, and loads a code knowledge graph into Cloud Spanner for Graph-RAG querying. PHP is the only supported target language (`.php` + legacy CakePHP `.ctp` templates); the tool itself is Python 3.12+ on GCP (Vertex AI Gemini + Spanner).

All user-facing docs (README.md, graph_generator/manual.md, GRAPH_SCHEMA_GUIDE.md, presentation.html, sample_prompt.txt) are in **Japanese — keep edits and examples Japanese**. The canonical doc example is `UsersController` / `UsersTable` / `CreateUsers` migration. Exception: presentation.html lines ~1170–1379 record a real past spring-petclinic demo and are intentionally left untouched.

## Environment & commands

The venv is **uv-managed (Python 3.14, no pip binary)** — use `uv pip … --python .venv/bin/python`:

```bash
uv pip install --python .venv/bin/python -r requirements.txt   # requirements.txt is a full pip-freeze; pin new deps
cp .env.example .env    # or: python -m graph_generator init   (interactive)
```

Pipeline CLI (`graph_generator/__main__.py`):

```bash
python -m graph_generator analyze <target_dir>        # full pipeline: docs + graph (Phases 1–10)
python -m graph_generator generate wiki <target_dir>  # docs only (Phases 1–6)
python -m graph_generator generate graph <target_dir> # Spanner graph only (Phases 1, 1.5, 8–10)
python -m graph_generator validate                    # row counts + orphan-edge checks against Spanner
python -m graph_generator setup spanner               # create instance + DB + tables + property graph
python -m graph_generator.setup_spanner_graph --verify|--destroy   # standalone infra script (module form)
```

Query surfaces:

```bash
adk run graph_query_agent                    # ADK agent REPL
python -m mcp_server                         # MCP server, streamable-http on :8080 (.mcp.json points here)
uvicorn webapp.main:app --port 8000          # docs viewer + chat UI backend
cd webapp/frontend && npm run dev|build|lint # Vite/React frontend (eslint)
```

**There is no test suite.** The proven no-GCP verification: `python -m compileall graph_generator`, then run Phases 1 + 1.5 against a throwaway CakePHP tree (they are pure-local, zero API):

```python
from graph_generator.pipeline import PipelineData, phase1_scan, phase1b_treesitter_entities
data = PipelineData(target_dir="/tmp/some_cakephp_tree")
phase1_scan(data); phase1b_treesitter_entities(data)   # inspect data.extracted_entities
```

Fixture policy (changed 2026-07-08): authored fixture code in `test_codes/` **is committed**; only `test_codes/php_cakephp/{vendor,tmp,logs}` stay local via `.gitignore` negations. See "Test fixtures" below.

## Architecture

**Pipeline (`graph_generator/pipeline.py`, 10 phases; Phase 7 is 欠番/removed):**
- Phase 1 scan → Phase 1.5 tree-sitter entity extraction (local, no API) → Phases 2–6 Gemini file/dir/topic summaries + index (Japanese output, prompts in `prompts.py`) → `phase_schema_docs` (ER-diagram docs from DB migrations) → Phases 8–10 Spanner node/edge writes + embeddings.
- Resume model: summaries/topics persist under `OUTPUT_DIR` (default `output_docs_pipeline/`), entities checkpoint to `entities.json`, Phases 8–10 checkpoint to `graph_checkpoint.json`; `analyze` hands docs→graph state via `pipeline_data.pkl`. Deleting `OUTPUT_DIR` forces a clean run.
- Everything is configured through `.env` → `graph_generator/config.py` (models, concurrency, Spanner names, `GRAPH_NAME`/`ID_PREFIX` allow side-by-side graphs in one DB).

**Parser contract (`graph_generator/treesitter_parser.py`):**
- `parse_entities(path, content)` returns `{file_path, namespace, classes: [{name, kind, modifiers, base_classes, interfaces, methods: [{name, modifiers, return_type, parameters, calls}]}], imports}` or `None` (file then has no Class/Method nodes; `chunk_by_structure` separately falls back to char-splitting). Uses `tree_sitter_php.language_php()` — the full grammar, so HTML-mixed templates parse.
- Properties and enum cases are folded into `methods` as zero-parameter members, deduped against real method names — Phase 8 keys `method_id` by `(file, class, name)`, so name collisions would merge graph rows.
- Top-level functions land in a `(global)` pseudo-class. Trait `use` inside a class body is appended to `interfaces` (mixin).

**Edge derivation invariant (Phase 9, `pipeline.py`, resolution-based):** `MethodCalls`/`ClassInherits`/`FileImports`/`FileDependsOn` edges are emitted **only** from Phase 1.6 (`resolution.py`) records whose status is `resolved` and whose target is an internal graph node — each carries a `resolution` provenance property (`lsp` | `convention:<rule>`). `external` targets (confirmed but outside the graph, e.g. vendor or another repo) are **counted, never edged**. Ambiguous/dynamic/unresolved calls go to the separate `PossiblyCalls` table (name-heuristic/candidate fan-out capped by `POSSIBLY_CALLS_MAX_CANDIDATES`). The old simple-name first-definition-wins matching is gone; **zero wrong MethodCalls/ClassInherits edges** is a hard guarantee, enforced structurally and measured by `python -m graph_generator evaluate` (the regression gate: ≥85% ground-truth + entity coverage + QA, 0 wrong edges). Pure builders `build_node_rows`/`derive_edge_rows` do all derivation with no GCP; the writers just batch-insert. If you change what the parser emits, check `resolution.py` (SpanIndex/EntityIndex) and `derive_edge_rows` together.

**Node identity (`_make_id`, `ID_SCHEME`):** node IDs hash `(repo, target-relative-path, fqcn, member)` — portable across machines, and **repo-scoped** so identical relpath+FQCN in different repositories are always distinct nodes (never merged). `ID_PREFIX` still namespaces side-by-side *graphs* in one DB; `repo` is the within-graph dimension (every node table has a `repo` column). Multiple repos are **isolated islands** in one shared graph — a reference from repo A into repo B is `external` (no edge). Ingest incrementally (`analyze <dir> --repo-name X`) or batch (`analyze --repos manifest.json`); each repo's checkpoints live under `OUTPUT_DIR/repos/<repo>/`. Bump `ID_SCHEME` whenever `_make_id` inputs change (the graph_checkpoint guard then discards stale-scheme resumes).

**Scanning config (`config.py`):** `SOURCE_EXTENSIONS = {.php, .ctp}`; `SKIP_DIRS` excludes `vendor`, `tmp`, `logs`, `webroot`, `bin` — but **`config/Migrations` must stay scannable**: `DB_SCHEMA_DETECTORS` (additive list, currently CakePHP/Phinx via the `/config/Migrations/` path token) drives the データベーススキーマ topic and ER docs.

**Graph & query stack:** `graph_generator/setup_spanner_graph.py` (declarative `SCHEMA_SPEC`, single source of truth for DDL + the pipeline's insert column lists via `write_columns`) creates 6 node tables (Files/Classes/Methods/Modules/Directories/DbTables) + 16 edge tables (adds PossiblyCalls, TableReferences, ClassMapsToTable; FileImports now populated; cross-repo CrossRepoRef/CrossRepoFileRef/CrossRepoCalls + DI DiBinds/DiInjects written by the `crossref` command) + one property graph (`config.GRAPH_NAME`, default `code_graph_a`). Existing DBs migrate idempotently via `python -m graph_generator.setup_spanner_graph --migrate` (INFORMATION_SCHEMA diff → ALTER/CREATE). `graph_query_agent` is a two-ADK-agent setup — `root_agent` (orchestrator, no tools) delegates to `graph_agent`, which holds the read-only `run_gql_query` tool. It is consumed by `mcp_server/` (FastMCP over HTTP) and `webapp/main.py` (FastAPI: serves generated docs from `OUTPUT_DIR` + `/api/chat` endpoints).

**Cross-repo layer (`graph_generator/crossref.py`, run post-ingest via `crossref`):** builds an app-origin-only FQCN/function registry from every ingested repo's committed `entities.json` and emits CrossRepoRef (kinds: import/class_ref/extends/implements/uses/new/type_hint/instanceof), CrossRepoFileRef (classless bootstrap/config files), CrossRepoCalls (typed/injected/chained receivers + namespaced function calls), DiBinds/DiInjects. Zero-wrong-edge rules mirror PHP semantics: classes resolve via `FileCtx.resolve_class_strict` (never global fallback; callable strings FQ verbatim; only functions fall back ns→global); vendor-origin entities are mirrors (own nothing, never ref sources); a referencing repo's own committed definition wins (no edge); multi-repo-owned FQCNs are ambiguous → dropped + reported. Every drop is surfaced in the console "dropped / unresolved" section and `OUTPUT_DIR/crossref_report.json`. Eval fixture: `test_codes/multi_repo/` (edge-set GT in `cross_ground_truth.json` — empirically re-derived, safe to extend; exact-set matched by `tests/test_multi_repo_fixture.py`, review-repro regressions in `tests/test_crossref_accuracy.py`).

**Legacy naming caution:** `mcp_server/deploy.sh` defaults to Spanner instance `java-codegraph` / `java-codegraph-db` — pre-PHP-era names matching existing live infra; don't "fix" them blindly.

## Test fixtures (`test_codes/`)

Two committed PHP fixtures with machine-readable ground truth for semantic resolution that syntax parsing alone cannot do (same method/class names across packages, framework magic). They are the eval target for the PHP extractor and the future LSP-like "which class does this call resolve to" feature.

- **`php_plain/`** — framework-free; hand-authored `vendor/` with twin packages `Acme\Reporting\Report::generate()` vs `Globex\Reporting\Report::generate()`. 49 cases in 13 scenario dirs (`S01_Aliases`…`S16_StaticVsInstance`): use/alias/FQCN/group-use, traits + `insteadof`, late static binding, `__call`/`__get` magic, callables, namespaced-vs-global function fallback, plus `AMBIGUOUS`/`DYNAMIC` negative controls.
- **`php_cakephp/`** — real CakePHP **5.3.6** app, lock committed; regenerate vendor (~4.5k files) with `cd test_codes/php_cakephp && composer install` (php8.2 + intl/mbstring/xml/sqlite3 — installed on this box). 41 cases: Billing/Shipping plugin twins (`Service\Gateway::charge`), app classes shadowing real vendor classes (`App\Utility\Text::slug` vs `Cake\Utility\Text::slug`, `Hash::get`, `Http\Client::get`), string conventions (`fetchTable`/`loadComponent`/`addBehavior` incl. plugin-dot `'Billing.Audit'`), behavior mixins + magic finders via `Table::__call`, entity virtual fields, fluent query chains, framework callbacks (`beforeSave` — no call sites), DI wiring, needle names (`get`/`set`/`save`/`find`/`first` — 127 vendor definitions). Phinx migrations under `config/Migrations/` (+ Billing plugin) match the fields the code touches.

`ground_truth.json` per fixture: `file`/`line`/`expr` (+`occurrence`) → `expected` (`FQCN::method` | FQCN for string-convention `class_ref`s | `AMBIGUOUS`/`DYNAMIC` with `candidates`), `defined_in`, `syntactic_target` (the magic hop), `receiver`, `answer_location` (app|plugin|vendor). Vendor `defined_in` is path-only, so lock bumps don't invalidate it. A resolver is correct when it reports `expected` (or the candidates set for AMBIGUOUS/DYNAMIC) — never score against Phase 9's first-definition-wins winner.

Rules when touching fixtures:
- Authored line numbers are **frozen into ground truth** — after ANY fixture edit, run `python3 test_codes/validate_ground_truth.py` (standalone stdlib: schema, file existence, expr@line/occurrence, PSR-4 truthfulness) and update `ground_truth.json` in the same change.
- The name collisions and byte-identical call-site pairs ARE the test — never dedupe/rename them, and never add comments to fixture code (intent lives in the READMEs and `why_hard` fields).
- Keep fixtures and the validator standalone — no imports from or assumptions about `graph_generator` internals.
