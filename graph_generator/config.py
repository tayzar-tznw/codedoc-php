"""Central configuration — all settings loaded from .env with sensible defaults."""

import os

from dotenv import load_dotenv
load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


# ── Google Cloud ──────────────────────────────────────────────
GCP_PROJECT = _env("GOOGLE_CLOUD_PROJECT", "claude-cws-498905")
GCP_REGION = _env("GOOGLE_CLOUD_LOCATION", "global")

# ── Gemini ────────────────────────────────────────────────────
MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_CONCURRENCY = _env_int("GEMINI_CONCURRENCY", 100)
# Output token cap. Topic-summary docs (Mermaid + tables) are large, and Gemini-3
# thinking also draws from this budget — too low yields empty responses. It's a
# cap (you only pay for tokens actually produced), so keep it generous.
MAX_OUTPUT_TOKENS = _env_int("MAX_OUTPUT_TOKENS", 8192)

# ── Spanner Graph ─────────────────────────────────────────────
SPANNER_INSTANCE = _env("SPANNER_INSTANCE", "codedoc-instance")
SPANNER_DATABASE = _env("SPANNER_DATABASE", "codedoc-db")
GRAPH_NAME = _env("GRAPH_NAME", "code_graph_a")
ID_PREFIX = _env("ID_PREFIX", "a")
SPANNER_BATCH_SIZE = 5000

# ── Embeddings ────────────────────────────────────────────────
EMBED_MODEL = "text-embedding-005"
EMBED_BATCH_SIZE = 20
EMBED_CONCURRENCY = _env_int("EMBED_CONCURRENCY", 20)

# ── Output ────────────────────────────────────────────────────
OUTPUT_DIR = _env("OUTPUT_DIR", "output_docs_pipeline")

# ── LSP resolution (Phase 1.6) ────────────────────────────────
# Intelephense binary; install with `npm i -g intelephense`. When missing,
# Phase 1.6 degrades to convention/parser resolution with a loud warning
# (calls it cannot confirm go to PossiblyCalls, never to MethodCalls).
INTELEPHENSE_PATH = _env("INTELEPHENSE_PATH", "intelephense")
LSP_INDEX_TIMEOUT = _env_int("LSP_INDEX_TIMEOUT", 300)      # seconds
LSP_REQUEST_TIMEOUT = _env_int("LSP_REQUEST_TIMEOUT", 15)   # seconds

# Unresolved/ambiguous calls become PossiblyCalls edges toward same-named
# internal methods — but only when the candidate count stays at or below
# this cap (common names like `get` would otherwise fan out absurdly).
POSSIBLY_CALLS_MAX_CANDIDATES = _env_int("POSSIBLY_CALLS_MAX_CANDIDATES", 5)

# Vendor scanning: "" → interactive prompt when vendor dirs are found
# (non-TTY defaults to exclude); "true"/"false" → no prompt.
INCLUDE_VENDOR = _env("INCLUDE_VENDOR", "")

# ── File scanning ─────────────────────────────────────────────
# PHP only. `.ctp` covers legacy CakePHP (≤3) view templates — plain PHP syntax.
SOURCE_EXTENSIONS = {
    ".php", ".ctp",
}

# `vendor` (Composer), `tmp`/`logs`, and `webroot` (assets + front controller)
# are CakePHP noise; `bin` holds only the `cake` console bootstrap.
# NOTE: `config/Migrations` must stay scannable — DB_SCHEMA_DETECTORS below
# relies on it.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".idea", ".vscode", "build", "dist",
    "bin", "venv", ".venv",
    "vendor", "tmp", "logs", "webroot",
}

# ── Database schema detection ─────────────────────────────────
# When a codebase contains DB schema/migration files, the pipeline emits a
# dedicated "Database Schema" category (topic) with an ER-diagram doc.
#
# This is a list of detectors so support for a new framework is purely additive
# — add one dict, no pipeline code change. A file matches a detector if:
#   - its basename is in `file_names`, OR
#   - its path contains any substring in `dir_tokens`, OR
#   - (optional) its head contains any substring in `content_patterns`
#     (this reads the file, so scope it — pair with a narrow dir_token).
# Note: a file is only considered if it was scanned (its extension is in
# SOURCE_EXTENSIONS). CakePHP/Phinx migrations are plain `.php` files under
# `config/Migrations/`, so they are scanned as long as that directory is not
# in SKIP_DIRS.
DB_SCHEMA_DETECTORS = [
    {
        "framework": "CakePHP (Phinx Migrations)",
        "dir_tokens": {"/config/Migrations/"},
    },
]
