# CodeDoc MCP server

An [MCP](https://modelcontextprotocol.io) server built on [FastMCP 2.x](https://gofastmcp.com) that exposes the CodeDoc query agent (`graph_query_agent`) to any MCP-compatible host — **Antigravity CLI (`agy`)**, Claude Code, [MCP Inspector](https://github.com/modelcontextprotocol/inspector), etc.

Runs over **streamable-http** transport. Two ways to use it:

1. **Local**: `python -m mcp_server` → server on `http://127.0.0.1:8080/mcp`.
2. **Cloud Run**: deploy with `./mcp_server/deploy.sh`, then `gcloud run services proxy …` exposes the remote service on the same `http://127.0.0.1:8080/mcp` URL — your MCP client config works for both modes unchanged.

The server is a thin wrapper around the same ADK Runner pattern used by `webapp/main.py`. It does **not** spawn the webapp; it imports the agent directly and runs it in-process.

---

## Tools

| Tool | Purpose |
|---|---|
| `ask_codebase(question, session_id?)` | Main Q&A, backed by `graph_query_agent`. Returns `{response, session_id, turn_count}`. Pass `session_id` from a prior call to ask a follow-up in the same thread. |
| `list_sessions()` | Enumerate active sessions, most recently updated first. |
| `get_session_history(session_id)` | Full transcript as a list of `{role, text, timestamp}` turns. |
| `delete_session(session_id)` | Idempotent prune. |
| `rename_session(session_id, label)` | Attach a human-readable label. |

`graph_query_agent`'s orchestrator delegates to its graph specialist (`graph_agent`) for structural questions — class/method listings, dependencies, inheritance, impact analysis, counts. Documentation lookups are served straight from the Spanner code graph: the node tables (Files/Classes/Methods/Modules/Directories) carry `summary` columns alongside the structural fields, so a single GQL query covers both.

---

## Pre-setup (one-time per GCP project)

The MCP server is a thin transport — it does NOT provision its own backing services. **Cloud Spanner Graph is the only required backing service**: the graph specialist runs GQL against `SPANNER_INSTANCE` / `SPANNER_DATABASE` / `GRAPH_NAME`. A missing Spanner instance shows up at first tool call as `NOT_FOUND: Instance not found`.

The repo ships a setup script that creates it:

```bash
# Spanner Graph
#   NOTE: creates a 100 PU Enterprise-tier instance (billed while it exists).
#   Edit `processing_units` in graph_generator/setup_spanner_graph.py to change.
python -m graph_generator.setup_spanner_graph --project code-doc-graph

# Tear down Spanner when done:
python -m graph_generator.setup_spanner_graph --destroy --project code-doc-graph
```

`graph_generator/setup_spanner_graph.py` creates instance `codedoc-instance` / db `codedoc-db` by default, but the agent and `deploy.sh` default to `java-codegraph` / `java-codegraph-db` (the names that match the current `.env` and live infra). If you want the agent to talk to a freshly provisioned instance, pass `--instance java-codegraph --database java-codegraph-db` to the setup script, OR override `SPANNER_INSTANCE` / `SPANNER_DATABASE` env vars on the agent side.

---

## Local development

### Prerequisites

1. Project venv: `uv sync` from repo root. `fastmcp` is already in `pyproject.toml`.
2. GCP credentials: `gcloud auth application-default login` (the local server uses your user creds). Set a quota project: `gcloud auth application-default set-quota-project code-doc-graph`.
3. Env vars (in repo-root `.env`, auto-loaded). What the agent actually reads:
   - `GOOGLE_CLOUD_PROJECT` — defaults to `code-doc-graph` (used for both Spanner and Vertex SDK init).
   - `GOOGLE_GENAI_USE_VERTEXAI` — set to `true` so the Gemini client routes through Vertex AI.
   - `SPANNER_INSTANCE` / `SPANNER_DATABASE` / `GRAPH_NAME` — Spanner Graph (defaults `java-codegraph` / `java-codegraph-db` / `code_graph_a`).

### Launch

```bash
python -m mcp_server
```

Server starts on `http://0.0.0.0:8080/mcp`. Set `HOST`/`PORT` to override.

### Hook into Antigravity CLI (`agy`)

`agy` discovers MCP servers via **extensions** at `~/.gemini/extensions/<name>/gemini-extension.json` (NOT via a project-local `.mcp.json` — agy doesn't honor that path). Create the file:

```bash
mkdir -p ~/.gemini/extensions/codedoc
cat > ~/.gemini/extensions/codedoc/gemini-extension.json <<'EOF'
{
  "name": "codedoc",
  "version": "1.0.0",
  "description": "Query the CodeDoc-indexed codebase via the MCP server.",
  "mcpServers": {
    "codedoc": {
      "httpUrl": "http://127.0.0.1:8080/mcp",
      "timeout": 120000
    }
  }
}
EOF
```

Restart `agy`; the codedoc tools become available to any prompt. The same config works for Cloud Run mode — `gcloud run services proxy` makes the remote service look local on the same URL.

### Hook into Claude Code

`.mcp.json` (project root) or `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "codedoc": {
      "type": "http",
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

Restart Claude Code; `/mcp` should list `codedoc` as connected.

### Verify

```bash
npx @modelcontextprotocol/inspector http://127.0.0.1:8080/mcp
```

Or with curl:

```bash
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Expect a JSON-RPC `result` with `serverInfo.name = "codedoc"`.

End-to-end test with agy (after the extension is installed):

```bash
agy --print 'Use the codedoc.ask_codebase tool to ask: 何のサンプルコードですか？' \
  --dangerously-skip-permissions --print-timeout 120s
```

---

## Deploy to Cloud Run

The server ships with everything needed to deploy as a private Cloud Run service authenticated by Cloud Run IAM. Local clients connect through `gcloud run services proxy` so no bearer tokens or OAuth setup are required.

### Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login` for yourself).
- A GCP project with billing enabled and the deployer holding `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/serviceusage.serviceUsageAdmin`, `roles/cloudbuild.builds.editor` (or `roles/owner` for testing).
- The Spanner instance referenced by `SPANNER_INSTANCE` must already exist (see Pre-setup).

### One-shot deploy

From the **project root** (not from inside `mcp_server/`):

```bash
# (Optional) override defaults — the values below ARE the defaults
export PROJECT_ID="code-doc-graph"
export REGION="asia-northeast1"
export SERVICE_NAME="codedoc-mcp"
export SPANNER_INSTANCE="java-codegraph"
export SPANNER_DATABASE="java-codegraph-db"
export GRAPH_NAME="code_graph_a"

./mcp_server/deploy.sh
```

The script is idempotent. On each run it:

1. Enables required APIs (Run, Cloud Build, Artifact Registry, AI Platform, Spanner).
2. Creates (or reuses) the service account `codedoc-mcp-sa@PROJECT_ID.iam.gserviceaccount.com`.
3. Binds the runtime roles:
   - `roles/aiplatform.user` — Gemini calls + text-embedding-005
   - `roles/spanner.databaseUser` — Spanner Graph GQL queries
4. Builds via Cloud Build using `mcp_server/Dockerfile` with the project root as context.
5. Deploys to Cloud Run with `--no-allow-unauthenticated`, `--session-affinity`, and reasonable resource limits (2 Gi / 2 CPU / scale 0–5 / 10 concurrent / 300 s timeout).
6. Injects the agent env vars (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_VERTEXAI`, `SPANNER_*`, `GRAPH_NAME`) into the service.
7. Prints the service URL plus the next two commands you need to run.

### Grant yourself invoker access

Cloud Run uses IAM auth. Each principal that should reach the MCP server needs `roles/run.invoker` bound on the service. The deploy script prints the exact command — run it once per teammate:

```bash
gcloud run services add-iam-policy-binding codedoc-mcp \
  --project code-doc-graph \
  --region asia-northeast1 \
  --member user:you@example.com \
  --role roles/run.invoker
```

### Connect your MCP client to the remote server

`gcloud` proxies the authenticated remote service onto a local port using your local user credentials. No tokens to copy.

```bash
gcloud run services proxy codedoc-mcp \
  --project code-doc-graph \
  --region asia-northeast1 \
  --port 8080
```

Leave that terminal running. Your existing agy extension / `.mcp.json` works unchanged — `http://127.0.0.1:8080/mcp` now routes through the proxy to the Cloud Run service.

### Tearing down

```bash
gcloud run services delete codedoc-mcp --project code-doc-graph --region asia-northeast1
# Optional: clean up the service account
gcloud iam service-accounts delete codedoc-mcp-sa@code-doc-graph.iam.gserviceaccount.com --project code-doc-graph
```

---

## Known caveats

- **Sessions are in-memory per instance.** When Cloud Run scales an instance down or rolls one, the transcripts and labels for sessions on that instance vanish. `--session-affinity` keeps subsequent requests for the same `session_id` on the same instance while it lives, but it can't survive instance death. To make sessions durable across restarts, swap `InMemorySessionService` for `DatabaseSessionService(db_url="…")` in `mcp_server/runner.py` — one-line change — and back it with **Cloud SQL** (Postgres or MySQL) or ADK's **`VertexAiSessionService`**. Avoid SQLite-on-GCS-FUSE: the WAL/locking semantics break on object-storage-backed mounts.
- **Cold start.** ADK + Spanner client import takes several seconds. The first request after scale-to-zero will be slow. Set `MIN_INSTANCES=1` when invoking `deploy.sh` if you want a warm pool (~$0.024/hour idle).
- **No concurrency lock on the per-process metadata dict.** CPython's GIL keeps simple ops safe, but two simultaneous first-calls for the same `session_id` could race. Coding-CLI usage almost never triggers this.
- **CMEK / VPC connector not configured.** If your Spanner instance is behind a VPC, add `--vpc-connector` and `--vpc-egress` flags in `deploy.sh`. Out of scope for the default deploy.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ImportError: graph_query_agent` when running locally | You launched from a directory other than the repo root. `python -m mcp_server` expects the repo root on `sys.path`. |
| `ask_codebase` returns `NOT_FOUND: Instance not found` | Spanner instance referenced by `SPANNER_INSTANCE` doesn't exist. Run `python -m graph_generator.setup_spanner_graph --project ${GOOGLE_CLOUD_PROJECT} --instance ${SPANNER_INSTANCE} --database ${SPANNER_DATABASE}`. |
| `403 Forbidden` from the proxy | Your principal lacks `roles/run.invoker` on the service. Re-run the `add-iam-policy-binding` command above. |
| `PermissionDenied` from Spanner in the Cloud Run logs | Service-account role binding hasn't propagated yet (can take 60–90 s) or the wrong project was used. Check `gcloud projects get-iam-policy ${PROJECT_ID} --flatten='bindings[].members' --filter='bindings.members:codedoc-mcp-sa@'`. |
| agy doesn't see the codedoc tools | Extension is missing or in the wrong place. Confirm `ls ~/.gemini/extensions/codedoc/gemini-extension.json` exists and the JSON parses. Restart `agy`. |
| Cold-start timeout | Bump `--timeout` in `deploy.sh` or set `MIN_INSTANCES=1`. |
| Local server logs are noisy with `[EXPERIMENTAL] PLUGGABLE_AUTH` warning | Harmless ADK feature-flag warning. Sent to stderr only. |
