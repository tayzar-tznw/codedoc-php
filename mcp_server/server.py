"""FastMCP server exposing the CodeDoc query agents as MCP tools.

Tools:
  - ask_codebase(question, session_id?) — main Q&A entry point
  - list_sessions() — enumerate active sessions with labels and previews
  - get_session_history(session_id) — full turn-by-turn transcript
  - delete_session(session_id) — prune a session
  - rename_session(session_id, label) — attach a human-readable label
"""

import uuid

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.genai import types as genai_types

from mcp_server import sessions
from mcp_server.runner import (
    APP_NAME,
    USER_ID,
    AlreadyExistsError,
    log,
    runner,
    session_service,
)


mcp = FastMCP(
    name="codedoc",
    instructions=(
        "CodeDoc — query an analyzed codebase with `ask_codebase`. It answers "
        "questions about class relationships, call chains, inheritance, impact "
        "analysis, and general documentation by querying a Spanner code graph. "
        "To ask a follow-up in the same conversation, pass the "
        "`session_id` returned by the previous call. Answers are in Japanese "
        "(project convention)."
    ),
)


async def _ensure_session(session_id: str) -> None:
    """Create the session in ADK if it doesn't exist yet.

    Tolerates the get/create race: if a concurrent caller created the session
    between our get_session and create_session, ADK raises AlreadyExistsError
    which we swallow — the session exists either way.
    """
    existing = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if existing is None:
        try:
            await session_service.create_session(
                app_name=APP_NAME, user_id=USER_ID, session_id=session_id
            )
        except AlreadyExistsError:
            pass


@mcp.tool()
async def ask_codebase(
    question: str,
    session_id: str | None = None,
) -> dict:
    """Ask the CodeDoc query agent a question about the analyzed codebase.

    Uses the graph_query_agent (Spanner code graph) to answer questions about
    class relationships, call chains, inheritance, impact analysis, and general
    documentation.

    Args:
        question: Natural-language question. Japanese works best; English is fine too.
        session_id: Optional. Pass the session_id returned by a previous call
            to ask a follow-up in the same conversation. Omit to start fresh.

    Returns:
        {response: str, session_id: str, turn_count: int}
    """
    sid = session_id or str(uuid.uuid4())
    await _ensure_session(sid)
    meta = sessions.record_turn(sid, question)

    log.info("ask_codebase session=%s msg=%r", sid[:8], question[:200])

    user_content = genai_types.Content(
        role="user", parts=[genai_types.Part(text=question)]
    )
    final_text = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=sid, new_message=user_content
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text += part.text

    return {
        "response": final_text,
        "session_id": sid,
        "turn_count": meta.turn_count,
    }


@mcp.tool()
async def list_sessions() -> list[dict]:
    """List active query sessions, most recently updated first.

    Returns one entry per session with id, label (if set), the first question
    asked, the turn count, and timestamps. Sessions live only for the lifetime
    of this MCP server process.
    """
    items = sorted(
        sessions.metadata.values(), key=lambda m: m.updated_at, reverse=True
    )
    return [m.to_dict() for m in items]


@mcp.tool()
async def get_session_history(session_id: str) -> list[dict]:
    """Return the full transcript of a session as a list of turns.

    Each turn has role ('user' or 'model'), text, and a unix timestamp. Partial
    streaming events are filtered out — only complete turns are returned.

    Raises a tool error if the session_id is unknown.
    """
    session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if session is None:
        raise ToolError(f"session_id {session_id!r} not found")

    turns: list[dict] = []
    for event in session.events:
        if event.partial:
            continue
        if event.content is None or not event.content.parts:
            continue
        text = "".join(p.text or "" for p in event.content.parts)
        if not text:
            continue
        turns.append({
            "role": event.content.role or "model",
            "text": text,
            "timestamp": event.timestamp,
        })
    return turns


@mcp.tool()
async def delete_session(session_id: str) -> dict:
    """Delete a session and its metadata.

    Idempotent: returns deleted=False if the session was not found in either
    the metadata index or the ADK session store.
    """
    existed_in_adk = (
        await session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
        is not None
    )
    meta_dropped = sessions.drop(session_id)
    if existed_in_adk:
        await session_service.delete_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
    return {"deleted": meta_dropped or existed_in_adk, "session_id": session_id}


@mcp.tool()
async def rename_session(session_id: str, label: str) -> dict:
    """Attach a human-readable label to a session for easier later lookup.

    The label is metadata only — it doesn't affect the agent's behavior. Raises
    a tool error if the session_id is unknown.
    """
    try:
        meta = sessions.rename(session_id, label)
    except KeyError:
        raise ToolError(f"session_id {session_id!r} not found")
    return {"session_id": session_id, "label": meta.label}
