import os
import sys
import json
import uuid
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Load .env before any Google imports. Mirror mcp_server/runner.py: root .env
# first, then graph_query_agent/.env, so the agent's Vertex AI settings
# (GOOGLE_GENAI_USE_VERTEXAI=true, project, location) and Spanner graph config
# load even when there is no root .env. dotenv does not override already-set vars.
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")
load_dotenv(_project_root / "graph_query_agent" / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import markdown

# Add project root to path so we can import graph_query_agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from graph_query_agent.agent import root_agent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Docs are read from the same OUTPUT_DIR the pipeline writes to (see
# graph_generator/config.py). Kept inline rather than importing that
# package so the webapp container stays self-contained. .env is loaded above.
DOCS_DIR = Path(__file__).resolve().parent.parent / os.environ.get("OUTPUT_DIR", "output_docs_pipeline")
APP_NAME = "codedoc"

# ---------------------------------------------------------------------------
# ADK Runner setup
# ---------------------------------------------------------------------------
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="CodeDoc Viewer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Markdown rendering helper
# ---------------------------------------------------------------------------
md = markdown.Markdown(extensions=["tables", "fenced_code", "codehilite", "toc"])


def render_md(text: str) -> str:
    md.reset()
    return md.convert(text)


# ---------------------------------------------------------------------------
# Documentation API
# ---------------------------------------------------------------------------
@app.get("/api/docs/index")
async def get_index():
    index_path = DOCS_DIR / "index.md"
    if not index_path.exists():
        raise HTTPException(404, "index.md not found")
    raw = index_path.read_text(encoding="utf-8")
    return {"markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/metadata")
async def get_metadata():
    meta_path = DOCS_DIR / "metadata.json"
    if not meta_path.exists():
        raise HTTPException(404, "metadata.json not found")
    return json.loads(meta_path.read_text(encoding="utf-8"))


@app.get("/api/docs/topics")
async def get_topics():
    tree_path = DOCS_DIR / "topics" / "topic_tree.json"
    if not tree_path.exists():
        raise HTTPException(404, "topic_tree.json not found")
    return json.loads(tree_path.read_text(encoding="utf-8"))


@app.get("/api/docs/topics/{topic_name}")
async def get_topic(topic_name: str):
    topic_path = DOCS_DIR / "topics" / f"{topic_name}.md"
    if not topic_path.exists():
        raise HTTPException(404, f"Topic '{topic_name}' not found")
    raw = topic_path.read_text(encoding="utf-8")
    return {"name": topic_name, "markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/summaries/{kind}/{name}")
async def get_summary(kind: str, name: str):
    if kind not in ("files", "dirs"):
        raise HTTPException(400, "kind must be 'files' or 'dirs'")
    summary_path = DOCS_DIR / "summaries" / kind / f"{name}.md"
    if not summary_path.exists():
        raise HTTPException(404, f"Summary '{name}' not found")
    raw = summary_path.read_text(encoding="utf-8")
    return {"name": name, "kind": kind, "markdown": raw, "html": render_md(raw)}


@app.get("/api/docs/tree")
async def get_doc_tree():
    """Return a structured tree of all available docs."""
    tree = {"topics": [], "summaries": {"files": [], "dirs": []}}

    topics_dir = DOCS_DIR / "topics"
    if topics_dir.exists():
        for f in sorted(topics_dir.iterdir()):
            if f.suffix == ".md":
                tree["topics"].append(f.stem)

    for kind in ("files", "dirs"):
        kind_dir = DOCS_DIR / "summaries" / kind
        if kind_dir.exists():
            for f in sorted(kind_dir.iterdir()):
                if f.suffix == ".md":
                    tree["summaries"][kind].append(f.stem)

    return tree


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_id = "web_user"

    # Ensure session exists
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    user_content = genai_types.Content(
        role="user", parts=[genai_types.Part(text=req.message)]
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=user_content
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    return {"response": final_text, "session_id": session_id}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    user_id = "web_user"

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )

    user_content = genai_types.Content(
        role="user", parts=[genai_types.Part(text=req.message)]
    )

    async def event_generator():
        # Send session_id first
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        payload = {
                            "type": "delta" if event.partial else "text",
                            "text": part.text,
                            "author": event.author,
                            "is_final": event.is_final_response(),
                        }
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Serve React build (production) with SPA history-fallback
# ---------------------------------------------------------------------------
# The frontend uses react-router BrowserRouter (clean URLs like /topic/:name,
# /chat). Hitting such a path directly or refreshing sends the request to
# FastAPI, which has no matching route. A plain StaticFiles mount would 404
# ({"detail":"Not Found"}); instead we serve real build files when they exist
# and fall back to index.html so the client-side router can take over.
frontend_build = (Path(__file__).resolve().parent / "frontend" / "dist").resolve()
if frontend_build.exists():
    # Hashed JS/CSS bundles (StaticFiles has built-in path-traversal protection).
    app.mount("/assets", StaticFiles(directory=str(frontend_build / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # The real /api/* routes are declared above and take precedence; this
        # guard only catches undefined /api/* paths, which must stay JSON 404s
        # rather than silently returning the SPA shell.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        # Serve a real top-level build file (favicon.svg, icons.svg, …) when it
        # exists and stays inside the build dir (path-traversal guard).
        candidate = (frontend_build / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(frontend_build):
            return FileResponse(candidate)
        # Otherwise hand the SPA shell back so react-router resolves the route.
        return FileResponse(frontend_build / "index.html")
