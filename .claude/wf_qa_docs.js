export const meta = {
  name: 'codedoc-qa-and-docs',
  description: 'Author QA question sets and update Japanese docs for the LSP/DB-schema/vendor upgrade',
  phases: [
    { title: 'QA', detail: 'author + self-verify qa_questions.json per fixture' },
    { title: 'Docs', detail: 'update each doc file for the new pipeline' },
    { title: 'Verify', detail: 'adversarially check each doc against the code' },
  ],
}

// ── Shared facts brief (the delta this PR introduces) ────────────────────────
const FACTS = `
CodeDoc pipeline upgrade you are documenting (repo: /home/admin/dev/codedoc_php).
The project ingests PHP/CakePHP, generates Japanese docs with Gemini, loads a
code knowledge graph into Cloud Spanner. What changed in this PR:

PHASES (graph track now runs these before Phase 8):
- Phase 1.6 "LSP Resolution" (graph_generator/resolution.py + lsp_client.py +
  php_conventions.py): resolves every PHP call site / inheritance / import to an
  EXACT fully-qualified target using Intelephense (LSP textDocument/definition)
  plus CakePHP string-convention rules (fetchTable('Users') -> App\\Model\\Table\\UsersTable,
  plugin-dot 'Billing.Audit', loadComponent/addBehavior, magic finders via Table::__call,
  entity virtual fields). Each resolution has a status: resolved | external
  (target confirmed but outside the graph, e.g. vendor) | ambiguous | dynamic |
  unresolved, and a provenance 'via' (lsp | convention:<rule> | parser). Checkpointed
  to resolutions.json (per-file mtime resume). If Intelephense is missing it
  degrades loudly to convention/parser-only (never falls back to the old wrong
  name-matching).
- Phase 1.7 "DB Schema" (graph_generator/migration_parser.py): deterministically
  replays CakePHP/Phinx migrations under config/Migrations/ (incl. plugins) into a
  final schema (tables/columns/indexes/foreign keys). No DB connection.

EDGE POLICY (the core correctness guarantee — ZERO wrong edges):
- MethodCalls / ClassInherits / FileImports / FileDependsOn edges are emitted ONLY
  from RESOLVED records whose target is an internal graph node, each carrying a
  'resolution' provenance property. 'external' targets are counted but produce no
  edge. Unresolved / ambiguous / dynamic calls go to a NEW edge table PossiblyCalls
  (name-heuristic / candidate fan-out, capped by POSSIBLY_CALLS_MAX_CANDIDATES=5).
  The old first-definition-wins simple-name matching is deleted.

NODE IDENTITY: node IDs now hash TARGET-RELATIVE paths + FQCN (portable, and
identically-named classes/methods across files/namespaces/projects never merge).

SPANNER SCHEMA (single source of truth: graph_generator/setup_spanner_graph.py SCHEMA_SPEC):
- 6 NODE tables: Files, Classes, Methods, Modules, Directories, DbTables (NEW).
- 11 EDGE tables: FileImports (now populated; was dormant), FileDependsOn,
  ClassInherits, MethodCalls, PossiblyCalls (NEW), FileDefinesClass,
  ClassDefinesMethod, FileBelongsToModule, DirContainsFile, TableReferences (NEW),
  ClassMapsToTable (NEW).
- New/added columns: Files(path, origin); Classes(namespace, fqcn, start_line,
  end_line, origin); Methods(fqmn, start_line, end_line, origin);
  MethodCalls(resolution, call_line); ClassInherits(resolution);
  FileDependsOn(resolution); FileImports(import, resolution).
- DbTables(table_id, name, columns JSON, indexes JSON, foreign_keys JSON,
  source_file, plugin, summary, embedding).
- TableReferences(edge_id, source_table, target_table, fk_column, referenced_column) — FK table->table.
- ClassMapsToTable(edge_id, class_id, table_id, via) — CakePHP Table class -> DbTable
  (via='settable' when setTable('x') is literal, else 'convention' via Inflector).
- setup_spanner_graph.py now migrates EXISTING databases idempotently
  (INFORMATION_SCHEMA diff -> ALTER ADD COLUMN / CREATE TABLE); new flag
  "python -m graph_generator.setup_spanner_graph --migrate". Property graph is CREATE OR REPLACE.

VENDOR HANDLING: vendor/ is excluded by default. New flags --include-vendor /
--exclude-vendor on analyze/generate, plus .env INCLUDE_VENDOR, plus an interactive
prompt at analysis time that reports vendor PHP file count + total size. When
included, vendor files get graph nodes/edges marked origin='vendor' but are
EXCLUDED from the Gemini doc phases (2-6) and embeddings (cost). Even when vendor
nodes are excluded, Intelephense still indexes vendor so calls into vendor resolve
as 'external' (counted, no edge).

NEW 'evaluate' COMMAND (local, NO GCP):
  python -m graph_generator evaluate [--fixture php_plain|php_cakephp|all] [--dump-edges]
Runs Phases 1, 1.5, 1.6, 1.7 + node/edge derivation against the committed
test_codes/ fixtures and reports: ground-truth completeness %, entity coverage %,
QA completeness %, and wrong-edge count. Exit 0 if all >= 85% and wrong edges == 0,
else 1 (2 if php_cakephp vendor is not composer-installed).

NEW CONFIG KEYS (graph_generator/config.py, all via .env):
  INTELEPHENSE_PATH (default 'intelephense'), LSP_INDEX_TIMEOUT=300,
  LSP_REQUEST_TIMEOUT=15, POSSIBLY_CALLS_MAX_CANDIDATES=5, INCLUDE_VENDOR (''=prompt).
The .env targets GCP project 'codedoc-php', Spanner instance 'codedoc', database 'codedoc-php'.

NEW MODULES: graph_generator/{lsp_client,resolution,php_conventions,migration_parser,evaluate}.py.

STYLE RULES (must follow):
- User-facing docs are JAPANESE — keep them Japanese. CLAUDE.md and .env.example are ENGLISH.
- Canonical doc example is UsersController / UsersTable / CreateUsers migration.
- Do NOT invent behavior — everything above is what the code does; read the code to confirm specifics.
`

// ── Phase 1: QA question sets (self-verifying) ───────────────────────────────
const QA_SCHEMA = {
  type: 'object',
  properties: {
    fixture: { type: 'string' },
    file_written: { type: 'string' },
    question_count: { type: 'integer' },
    qa_pct: { type: 'number' },
    verified: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['fixture', 'question_count', 'qa_pct', 'verified'],
}

function qaPrompt(fixture) {
  return `Author a Japanese QA question set for the CodeDoc graph evaluator, at
test_codes/${fixture}/qa_questions.json. This is a NEW file. Do not edit anything else.

Use the venv python: .venv/bin/python. Repo: /home/admin/dev/codedoc_php.

The evaluator is graph_generator/evaluate.py — READ its answer_question() function to
learn the exact check schema. The file format is:
{
  "version": 1, "fixture": "${fixture}", "language": "ja",
  "questions": [
    {"id": "QA-01",
     "question": "<natural-language Japanese question>",
     "check": {"type": "<one of the supported types>", "subject": "<FQMN or FQCN or path or table>"},
     "expected": [<list of expected strings>] or <int for count_at_least>,
     "match": "contains_all" | "exact_set" | "equals" | "count_at_least"}
  ]
}
Supported check.type values (see answer_question): node_exists, callees_of, callers_of,
possibly_callees_of, parents_of, children_of, methods_of, class_file, imports_of,
class_table, table_columns, table_fks, count_at_least. For callees_of/callers_of the
subject is a fully-qualified method name "FQCN::method"; for parents_of/children_of/
methods_of/class_table it is a FQCN; for class_file it is a FQCN and expected is the
relative file path; for table_columns/table_fks the subject is a DB table name
(table_fks expected entries look like "user_id->users"); for imports_of the subject is
a relative file path.

REQUIREMENTS:
1. Write 16-20 questions that meaningfully exercise the graph produced for this
   fixture: call resolution (callees_of / callers_of), inheritance, class->file,
   imports, PossiblyCalls for ambiguous/dynamic sites, and for php_cakephp also
   class_table / table_columns / table_fks (DbTables). Cover the interesting cases:
   plugin twins, app-shadows-vendor, the DB schema (articles.user_id->users).
2. The questions MUST be answerable by the graph as it actually resolves. To find the
   true answers, run the evaluator and inspect the derived graph. The most reliable
   way: write a short throwaway python snippet that runs
   graph_generator.pipeline phase1_scan/phase1b_treesitter_entities/phase1c_lsp_resolution/
   phase1d_db_schema then build_node_rows + derive_edge_rows on
   test_codes/${fixture}, and print the MethodCalls/ClassInherits/DbTables/ClassMapsToTable/
   TableReferences edges (see how graph_generator/evaluate.py evaluate_fixture does it, and
   _build_graph_view / answer_question for how each check is evaluated). Set an isolated
   OUTPUT_DIR env var (e.g. OUTPUT_DIR=/tmp/qa_${fixture}) so you don't clobber anything.
3. After writing the file, VERIFY by running:
     OUTPUT_DIR=/tmp/qa_verify_${fixture} .venv/bin/python -m graph_generator evaluate --fixture ${fixture}
   and confirm the "QA completeness" line is 100% (every question you wrote must be
   answerable — if any fail, fix the question's expected value or drop it; never leave
   a failing question). The other metrics (ground-truth completeness, coverage,
   wrong edges) are already passing — do not change any source code.
${FACTS}

FACTS FOR YOUR QUESTIONS: use only relationships the graph really contains. Report the
final question_count and the QA completeness % you achieved (must be 100).`
}

// ── Phase 2: doc updates (one agent per file) ────────────────────────────────
const DOC_SCHEMA = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    updated: { type: 'boolean' },
    sections_changed: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['file', 'updated', 'summary'],
}

const DOC_TASKS = [
  {
    file: 'GRAPH_SCHEMA_GUIDE.md', lang: 'Japanese',
    brief: `Update the graph schema guide to the NEW schema: 6 node tables (add DbTables)
and 11 edge tables (add PossiblyCalls, TableReferences, ClassMapsToTable; note FileImports
is now populated). Document every new column (see FACTS). Add a clear explanation of the
resolution provenance ('resolution' property) and the zero-wrong-edge policy (resolved ->
MethodCalls, everything else -> PossiblyCalls). Update the mermaid overview and the GQL
example appendix with at least one query over the new tables (e.g. which DB table a
UsersTable maps to via ClassMapsToTable, or article->user foreign key via TableReferences,
or callers of a method via MethodCalls filtered by resolution). Keep the existing
UsersController/UsersTable/CreateUsers example style. Cross-check table/column names against
graph_generator/setup_spanner_graph.py SCHEMA_SPEC exactly.`,
  },
  {
    file: 'graph_generator/manual.md', lang: 'Japanese',
    brief: `Update the manual: add Phase 1.6 (LSP resolution) and Phase 1.7 (DB schema from
migrations) to the phase walkthrough; document resolutions.json, the degraded-mode behavior
when Intelephense is missing, and the new 'evaluate' command with its 85/85/85/0 gate.
Update the Phase 8/9 column tables and the edge-label list to the new 6-node/11-edge schema
with the new columns. Mention the vendor include/exclude flow and the new config keys.`,
  },
  {
    file: 'README.md', lang: 'Japanese',
    brief: `Update the README node/edge lists (6 nodes / 11 edges), the phase list (add
1.6 LSP resolution + 1.7 DB schema), the command list (add 'evaluate' and the
--include-vendor/--exclude-vendor flags, and setup --migrate), and add a short section on
the Intelephense-based resolution and the zero-wrong-edge guarantee. Keep it Japanese and
concise; do not duplicate the whole schema guide.`,
  },
  {
    file: '.claude/skills/codedoc-query/SKILL.md', lang: 'Japanese',
    brief: `Update the Nodes/Edges bullet lists to the new 6-node/11-edge schema (add
DbTables, PossiblyCalls, TableReferences, ClassMapsToTable; FileImports now populated).
If there are GQL examples, keep them valid against the new schema. Small, focused edit.`,
  },
  {
    file: '.env.example', lang: 'English',
    brief: `Rewrite .env.example to match graph_generator/config.py exactly. Remove dead keys
that config.py never reads (GOOGLE_CLOUD_REGION, GEMINI_REGION). Add the keys config.py DOES
read that were missing (ID_PREFIX, MAX_OUTPUT_TOKENS) and the NEW keys (INTELEPHENSE_PATH,
LSP_INDEX_TIMEOUT, LSP_REQUEST_TIMEOUT, POSSIBLY_CALLS_MAX_CANDIDATES, INCLUDE_VENDOR) with
sensible commented defaults. Use placeholder project values (your-project-id) — do NOT put
real project names. Keep it English with short inline comments. Verify every key you list is
actually read by config.py (grep it).`,
  },
]

function docPrompt(task) {
  return `Update the file ${task.file} in /home/admin/dev/codedoc_php. Language: ${task.lang}.
Edit ONLY this one file. Read the current file first, and read the relevant source
(graph_generator/setup_spanner_graph.py, config.py, __main__.py, pipeline.py, evaluate.py)
to get names exactly right. Preserve the existing document's structure, tone, and formatting
conventions; make surgical updates, do not rewrite wholesale unless a section is now wrong.

TASK: ${task.brief}
${FACTS}
Report which sections you changed and confirm names match the code.`
}

// ── Run ──────────────────────────────────────────────────────────────────────
phase('QA')
const qa = await parallel([
  () => agent(qaPrompt('php_plain'), { label: 'qa:php_plain', phase: 'QA', schema: QA_SCHEMA, agentType: 'general-purpose' }),
  () => agent(qaPrompt('php_cakephp'), { label: 'qa:php_cakephp', phase: 'QA', schema: QA_SCHEMA, agentType: 'general-purpose' }),
])

phase('Docs')
const docs = await parallel(
  DOC_TASKS.map(t => () =>
    agent(docPrompt(t), { label: `doc:${t.file}`, phase: 'Docs', schema: DOC_SCHEMA, agentType: 'general-purpose' })
  )
)

// Adversarial verification of the schema-heavy docs against the code.
phase('Verify')
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    accurate: { type: 'boolean' },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['file', 'accurate', 'problems'],
}
const verifyTargets = ['GRAPH_SCHEMA_GUIDE.md', 'README.md', '.env.example']
const verdicts = await parallel(
  verifyTargets.map(f => () =>
    agent(`Adversarially verify that ${f} in /home/admin/dev/codedoc_php is FACTUALLY
consistent with the code. Read ${f} and the authoritative sources
(graph_generator/setup_spanner_graph.py SCHEMA_SPEC for table/column names,
graph_generator/config.py for env keys, graph_generator/__main__.py for CLI commands).
Report every mismatch: wrong/missing table or column names, wrong edge counts, env keys
that don't exist in config.py, commands that don't exist. Be skeptical; list concrete
problems (empty list if genuinely accurate).`,
      { label: `verify:${f}`, phase: 'Verify', schema: VERIFY_SCHEMA, agentType: 'general-purpose' })
  )
)

return {
  qa: qa.filter(Boolean),
  docs: docs.filter(Boolean).map(d => ({ file: d.file, updated: d.updated })),
  verify: verdicts.filter(Boolean),
}
