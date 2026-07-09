---
name: codedoc-query
description: >-
  Proactively query a CodeDoc-indexed codebase through the `codedoc` MCP server's
  `ask_codebase` tool (a graph_query_agent over a Spanner code graph) to understand
  code BEFORE grepping or editing. Use it on your own initiative — without waiting
  to be asked by name — whenever fixing a bug, debugging, refactoring, extending,
  or reviewing a large or unfamiliar codebase: to locate where something lives,
  list a class's methods, trace who-calls-what (call chains), map inheritance,
  find a file/module's dependents, assess change impact / blast radius, or detect
  cyclic dependencies, plus "what does X do / explain this component" lookups.
  Prefer ask_codebase over manual code spelunking for whole-codebase questions.
  Follow-ups reuse the returned session_id. Triggers: fix/debug/refactor/understand
  a big codebase, impact analysis, call graph, dependency check.
---

# CodeDoc codebase query (MCP) — use proactively

The `codedoc` MCP server exposes a query agent (`graph_query_agent`) that answers
questions about an indexed codebase by running GQL against a **Spanner code
knowledge graph**. The graph's nodes carry both structure *and* generated
`summary` text, so one tool covers structural queries and documentation lookups,
for a codebase of any language. **The agent answers in Japanese** (its configured
default) — relay answers as-is unless the user wants a translation.

## Use this proactively

When you're about to **fix, debug, refactor, extend, or review** a large or
unfamiliar codebase, reach for `ask_codebase` on your own initiative — *before*
grepping or reading files, and without waiting for the user to name the tool.
Whole-codebase questions are exactly what the graph answers fastest.

Default workflow when starting a task in such a codebase:

1. **Orient** — ask for a high-level overview, or where a feature/area lives.
2. **Locate** — ask which class / file / method implements the thing you must change.
3. **Check impact BEFORE editing** — ask what depends on / calls the target
   ("what calls `X`?", "what depends on `<file>`?", "blast radius of changing `Y`?").
4. **Drill down** — refine with follow-ups in the *same* session (see below).

Then make your edit with the dependency/impact picture in hand.

**When NOT to use it:** files already open in front of you (just read them), or
no `codedoc` server is connected (see Prerequisites — fall back to normal code
navigation and tell the user the index isn't available).

## Prerequisites: the MCP server must be connected

The tools appear under an MCP server named **`codedoc`**: `ask_codebase`,
`list_sessions`, `get_session_history`, `delete_session`, `rename_session`.

- **Claude Code**: run `/mcp` to confirm `codedoc` is connected. If not, add it to
  `.mcp.json` (project) or `~/.claude.json`:
  ```json
  { "mcpServers": { "codedoc": { "type": "http", "url": "http://127.0.0.1:8080/mcp" } } }
  ```
- **Antigravity CLI (`agy`) / Gemini CLI**: register an extension at
  `~/.gemini/extensions/codedoc/gemini-extension.json` pointing at the same URL
  via `httpUrl` (not the Claude shape).
- The server itself is started from the **CodeDoc MCP server repo** (local
  `python -m mcp_server` → `http://127.0.0.1:8080/mcp`, or a Cloud Run deploy
  reached via `gcloud run services proxy`). It needs GCP credentials and a
  **populated graph** for *this* codebase. See that repo's README for setup.

If the `codedoc` tools aren't present, don't guess from memory — tell the user the
index for this codebase isn't connected.

## Tools

| Tool | Use it for | Returns |
|---|---|---|
| `ask_codebase(question, session_id?)` | The main entry point. Ask one clear question; omit `session_id` to start fresh. | `{response, session_id, turn_count}` |
| `list_sessions()` | See active Q&A threads (most recent first). | list of `{id, label, first question, turn_count, timestamps}` |
| `get_session_history(session_id)` | Full transcript of a thread. | list of `{role, text, timestamp}` |
| `rename_session(session_id, label)` | Label a thread for later lookup. | `{session_id, label}` |
| `delete_session(session_id)` | Prune a thread (idempotent). | `{deleted, session_id}` |

Sessions are **in-memory for the life of the server process** — they don't
survive a server restart.

## What the graph contains

The agent translates questions into GQL over these entities (the same schema for
every CodeDoc-indexed codebase):

- **Nodes** (6): `Files`, `Classes`, `Methods`, `Modules`, `Directories`,
  `DbTables` (each with a `summary`; `DbTables` is the DB schema replayed from
  migration files — columns, indexes, foreign keys).
- **Edges** (11): `FileImports`, `FileDependsOn`, `ClassInherits`, `MethodCalls`,
  `PossiblyCalls`, `FileDefinesClass`, `ClassDefinesMethod`,
  `FileBelongsToModule`, `DirContainsFile`, `TableReferences`, `ClassMapsToTable`.

Call/inheritance/import edges (`MethodCalls`, `ClassInherits`, `FileImports`,
`FileDependsOn`) contain only **resolved** targets and carry a `resolution`
provenance property; unconfirmed candidate calls live in `PossiblyCalls` instead
— treat those as "maybe". `TableReferences` is a table→table foreign key;
`ClassMapsToTable` links a CakePHP Table class to its `DbTables` row.
`Files`/`Classes`/`Methods` also carry an `origin` property (`app` | `vendor`).

## Asking good questions

- **One concept per call.** Ask a single, specific question; drill down with
  follow-ups rather than stacking sub-questions.
- **Name concrete entities** (a class, file, method, module) when you know them —
  it anchors the GQL. Use placeholders below with your real names:
  - "list the methods of class `<ClassName>`"
  - "what calls `<methodName>`?" / "what does `<ClassName>` call?"
  - "which files depend on `<path/to/File>`?"
  - "show the inheritance hierarchy under `<BaseClass>`"
  - "are there any cyclic dependencies?"
  - "which DB table does `<TableClass>` map to?" / "which tables reference `<table_name>`?"
  - "give a high-level overview of this codebase" / "what does the `<module>` module do?"
- **Japanese phrasing works best**, English is accepted (e.g. `<ClassName> のメソッドを一覧して`).

## Multi-turn follow-ups

`ask_codebase` returns a `session_id`. Pass it back on the next call to continue
the same thread with full prior context:

```
1) ask_codebase("<ClassName> のメソッドを一覧して")
     → { response: "...", session_id: "abc123", turn_count: 1 }
2) ask_codebase("その中で外部 I/O を行うのは？", session_id: "abc123")
     → { ..., session_id: "abc123", turn_count: 2 }
```

Use `list_sessions()` to find a thread, `get_session_history(id)` to review it,
and `rename_session(id, "...")` to label a long investigation.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `NOT_FOUND: Instance not found` | The Spanner instance the server points at doesn't exist / the graph for this codebase isn't built. The codebase needs to be indexed by CodeDoc first. |
| `403 Forbidden` (Cloud Run) | Caller lacks `roles/run.invoker`, or `gcloud run services proxy` isn't running. |
| Empty / "見つかりません" answer | The entity isn't in the graph (wrong name, or not indexed). Re-check the name, or start with a broad overview question. |
| First call is slow | Cold start (agent + Spanner import). Subsequent calls are fast. |

This skill *queries* an index; it does not build one. Indexing a codebase into the
graph is done by the CodeDoc pipeline, not from here.
