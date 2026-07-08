"""ADK Runner setup, shared by all MCP tools.

A single Runner wraps the graph_query_agent root agent (Spanner code graph).
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_pkg_dir = Path(__file__).resolve().parent
_project_root = _pkg_dir.parent

# Pick up .env from project root first, then the package dir, then the agent's
# own graph_query_agent/.env as a fallback. The MCP server imports the agent
# directly rather than via `adk run`, so ADK's per-agent .env autoload never
# fires; without this, GOOGLE_GENAI_USE_VERTEXAI=true (plus project / Spanner
# settings) the agent needs are missing and google-genai demands an API key.
# dotenv does not override already-set vars, so root/package .env still win.
load_dotenv(_project_root / ".env")
load_dotenv(_pkg_dir / ".env")
load_dotenv(_project_root / "graph_query_agent" / ".env")

# Make graph_query_agent importable when launched as
# `python -m mcp_server` from any cwd.
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Log to stderr so Cloud Run captures logs cleanly and stdout stays clean for
# any future stdio-mode users.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("codedoc-mcp")

import contextlib

# Defensive: route any accidental stdout writes during ADK / agent imports to
# stderr. Not strictly required for HTTP transport (stdout isn't the wire), but
# keeps logs tidy and is essential if anyone re-enables stdio mode later.
with contextlib.redirect_stdout(sys.stderr):
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.errors.already_exists_error import AlreadyExistsError
    from graph_query_agent.agent import root_agent

APP_NAME = "codedoc-mcp"
USER_ID = "mcp_user"

session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
