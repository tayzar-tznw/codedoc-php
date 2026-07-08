"""In-memory session metadata (labels, previews, turn counts).

ADK's InMemorySessionService persists the conversation events but has no notion
of a human-readable label or per-session summary. This module keeps a tiny
parallel dict so list_sessions / rename_session work the way a user expects.

All state is process-local — lost when the MCP server exits, by design.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

_PREVIEW_LIMIT = 120


@dataclass
class SessionMeta:
    session_id: str
    first_message: str
    created_at: datetime
    updated_at: datetime
    turn_count: int = 0
    label: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "label": self.label,
            "first_message": self.first_message,
            "turn_count": self.turn_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


metadata: dict[str, SessionMeta] = {}


def record_turn(session_id: str, message: str) -> SessionMeta:
    """Insert or update metadata for a session. Returns the current meta."""
    now = datetime.now(timezone.utc)
    meta = metadata.get(session_id)
    if meta is None:
        meta = SessionMeta(
            session_id=session_id,
            first_message=message[:_PREVIEW_LIMIT],
            created_at=now,
            updated_at=now,
            turn_count=1,
        )
        metadata[session_id] = meta
    else:
        meta.updated_at = now
        meta.turn_count += 1
    return meta


def drop(session_id: str) -> bool:
    return metadata.pop(session_id, None) is not None


def rename(session_id: str, label: str) -> SessionMeta:
    meta = metadata.get(session_id)
    if meta is None:
        raise KeyError(session_id)
    meta.label = label
    meta.updated_at = datetime.now(timezone.utc)
    return meta
