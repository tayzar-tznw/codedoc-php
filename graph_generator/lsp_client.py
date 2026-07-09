"""Minimal LSP client (JSON-RPC over stdio) for the Intelephense PHP server.

Stdlib-only: subprocess + a reader thread + Content-Length framing. Scope is
deliberately narrow — initialize, wait for Intelephense's custom
`indexingStarted`/`indexingEnded` notifications, didOpen/didClose,
textDocument/definition, workspace/symbol, shutdown. Anything the server asks
of us (workspace/configuration, client/registerCapability, ...) gets a
minimal, well-formed answer so the session never stalls.

Positions on the wire are LSP-native: 0-based lines, 0-based UTF-16 columns
(treesitter_parser already emits UTF-16 columns; callers pass 1-based lines
and this module converts).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any
from urllib.parse import quote, unquote, urlparse


class LspUnavailable(Exception):
    """The language server could not be started or initialized."""


class LspRequestTimeout(Exception):
    """A single request exceeded its timeout (server still usable)."""


def path_to_uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path), safe="/")


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    return unquote(parsed.path)


# Settings pushed to Intelephense. `files.associations` must include *.ctp so
# CakePHP legacy templates participate in workspace indexing.
_INTELEPHENSE_SETTINGS = {
    "files": {
        "associations": ["*.php", "*.phtml", "*.ctp"],
        "exclude": [],
    },
    "diagnostics": {"enable": False},
    "telemetry": {"enabled": False},
}


class LspClient:
    """One Intelephense session rooted at a workspace directory."""

    def __init__(self, command: list[str], root_dir: str, storage_dir: str,
                 index_timeout: float = 300.0, request_timeout: float = 15.0):
        self.command = command
        self.root_dir = os.path.abspath(root_dir)
        self.storage_dir = storage_dir
        self.index_timeout = index_timeout
        self.request_timeout = request_timeout

        self.server_version: str = ""
        self.indexing_partial = False  # True if indexingEnded never arrived

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}  # id → {event, result, error}
        self._indexing_started = threading.Event()
        self._indexing_ended = threading.Event()
        self._index_progress_tokens: set[Any] = set()  # reader thread only
        self._dead = threading.Event()
        self._open_uris: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────

    def start(self):
        """Spawn the server, initialize, and wait for workspace indexing."""
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=self.root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            raise LspUnavailable(f"cannot spawn {self.command[0]!r}: {e}") from e

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        os.makedirs(self.storage_dir, exist_ok=True)
        init_params = {
            "processId": os.getpid(),
            "rootUri": path_to_uri(self.root_dir),
            "workspaceFolders": [
                {"uri": path_to_uri(self.root_dir), "name": os.path.basename(self.root_dir)}
            ],
            "capabilities": {
                "workspace": {
                    "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "workspaceFolders": True,
                    "symbol": {},
                },
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "synchronization": {"didSave": False},
                },
                "window": {"workDoneProgress": True},
            },
            "initializationOptions": {
                "storagePath": self.storage_dir,
                "clearCache": False,
            },
        }
        try:
            result = self._request("initialize", init_params,
                                   timeout=max(self.request_timeout, 60.0))
        except (LspRequestTimeout, LspUnavailable) as e:
            self.close()
            raise LspUnavailable(f"initialize failed: {e}") from e

        info = (result or {}).get("serverInfo") or {}
        self.server_version = info.get("version", "")

        self._notify("initialized", {})
        # Push settings both ways: didChangeConfiguration for servers that
        # listen, workspace/configuration answers for servers that pull.
        self._notify("workspace/didChangeConfiguration",
                     {"settings": {"intelephense": _INTELEPHENSE_SETTINGS}})

        self._wait_for_indexing()

    def _wait_for_indexing(self):
        """Block until indexingEnded (Intelephense custom notification).

        On a warm cache the started/ended pair still fires, but guard the
        never-started case: if nothing arrives shortly after initialize,
        assume the server is ready rather than burning the full timeout.
        """
        if self._indexing_ended.wait(timeout=self.index_timeout):
            return
        if not self._indexing_started.is_set():
            # No indexing activity ever reported — treat as ready.
            return
        self.indexing_partial = True

    def close(self):
        proc = self._proc
        if proc is None:
            return
        try:
            self._request("shutdown", None, timeout=5.0)
        except Exception:
            pass
        try:
            self._notify("exit", None)
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except Exception:
            proc.kill()
        self._dead.set()
        self._fail_all_pending("server closed")

    # ── requests used by resolution ──────────────────────────────

    def did_open(self, path: str, text: str):
        uri = path_to_uri(path)
        if uri in self._open_uris:
            return
        self._open_uris.add(uri)
        self._notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "php", "version": 1, "text": text},
        })

    def did_close(self, path: str):
        uri = path_to_uri(path)
        if uri not in self._open_uris:
            return
        self._open_uris.discard(uri)
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def definition(self, path: str, line: int, col: int) -> list[dict]:
        """Definition locations for the symbol at (1-based line, UTF-16 col).

        Returns [{"path": abs_path, "line": 1-based}] — LocationLink and
        Location shapes are normalized; null → [].
        """
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line - 1, "character": col},
        }, timeout=self.request_timeout)
        return self._normalize_locations(result)

    def workspace_symbol(self, query: str) -> list[dict]:
        """Workspace symbols: [{"name", "kind", "path", "line", "container"}]."""
        result = self._request("workspace/symbol", {"query": query},
                               timeout=self.request_timeout)
        out = []
        for item in result or []:
            loc = item.get("location") or {}
            uri = loc.get("uri", "")
            rng = (loc.get("range") or {}).get("start") or {}
            out.append({
                "name": item.get("name", ""),
                "kind": item.get("kind", 0),
                "path": uri_to_path(uri) if uri else "",
                "line": rng.get("line", 0) + 1,
                "container": item.get("containerName", ""),
            })
        return out

    @staticmethod
    def _normalize_locations(result: Any) -> list[dict]:
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        out = []
        for loc in result:
            if "targetUri" in loc:  # LocationLink
                uri = loc.get("targetUri", "")
                rng = (loc.get("targetSelectionRange") or loc.get("targetRange") or {})
            else:  # Location
                uri = loc.get("uri", "")
                rng = loc.get("range") or {}
            start = rng.get("start") or {}
            if uri:
                out.append({"path": uri_to_path(uri), "line": start.get("line", 0) + 1})
        return out

    # ── JSON-RPC plumbing ────────────────────────────────────────

    def _request(self, method: str, params: Any, timeout: float) -> Any:
        if self._dead.is_set():
            raise LspUnavailable("server is not running")
        with self._state_lock:
            req_id = self._next_id
            self._next_id += 1
            slot: dict[str, Any] = {"event": threading.Event()}
            self._pending[req_id] = slot
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        if not slot["event"].wait(timeout=timeout):
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise LspRequestTimeout(f"{method} timed out after {timeout}s")
        if "error" in slot:
            raise LspUnavailable(f"{method} error: {slot['error']}")
        return slot.get("result")

    def _notify(self, method: str, params: Any):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _respond(self, req_id: Any, result: Any):
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _send(self, payload: dict):
        proc = self._proc
        if proc is None or proc.stdin is None or self._dead.is_set():
            raise LspUnavailable("server is not running")
        body = json.dumps(payload).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        try:
            with self._write_lock:
                proc.stdin.write(frame)
                proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._dead.set()
            self._fail_all_pending(f"write failed: {e}")
            raise LspUnavailable(f"server pipe closed: {e}") from e

    def _read_loop(self):
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stream = proc.stdout
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    if b":" in line:
                        k, v = line.split(b":", 1)
                        headers[k.decode("ascii").lower()] = v.decode("ascii").strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0:
                    continue
                body = stream.read(length)
                if body is None or len(body) < length:
                    raise EOFError
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
        except (EOFError, ValueError, OSError):
            pass
        finally:
            self._dead.set()
            self._fail_all_pending("server exited")

    def _dispatch(self, msg: dict):
        # Response to one of our requests
        if "id" in msg and "method" not in msg:
            with self._state_lock:
                slot = self._pending.pop(msg["id"], None)
            if slot is not None:
                if "error" in msg:
                    slot["error"] = msg["error"]
                else:
                    slot["result"] = msg.get("result")
                slot["event"].set()
            return

        method = msg.get("method", "")

        # Server → client requests: answer minimally so the session proceeds.
        if "id" in msg:
            if method == "workspace/configuration":
                items = (msg.get("params") or {}).get("items") or []
                answers = []
                for item in items:
                    section = item.get("section", "")
                    if section == "intelephense":
                        answers.append(_INTELEPHENSE_SETTINGS)
                    elif section.startswith("intelephense."):
                        sub: Any = _INTELEPHENSE_SETTINGS
                        for part in section.split(".")[1:]:
                            sub = sub.get(part) if isinstance(sub, dict) else None
                        answers.append(sub)
                    else:
                        answers.append(None)
                self._respond(msg["id"], answers)
            elif method == "workspace/applyEdit":
                self._respond(msg["id"], {"applied": False})
            else:
                # client/registerCapability, window/workDoneProgress/create, ...
                self._respond(msg["id"], None)
            return

        # Notifications we care about. Indexing completion is signalled
        # differently across Intelephense versions: legacy custom
        # indexingStarted/indexingEnded, the standard $/progress protocol
        # (when the client advertises window.workDoneProgress), and always
        # a "Indexing finished" logMessage — watch all three.
        if method == "indexingStarted":
            self._indexing_started.set()
        elif method == "indexingEnded":
            self._indexing_ended.set()
        elif method == "$/progress":
            params = msg.get("params") or {}
            value = params.get("value") or {}
            kind = value.get("kind")
            if kind == "begin":
                title = (value.get("title") or "").lower()
                if "index" in title or not title:
                    self._index_progress_tokens.add(params.get("token"))
                    self._indexing_started.set()
            elif kind == "end" and params.get("token") in self._index_progress_tokens:
                self._indexing_ended.set()
        elif method == "window/logMessage":
            text = ((msg.get("params") or {}).get("message") or "").lower()
            if "indexing finished" in text or "indexing cancelled" in text:
                self._indexing_ended.set()
            elif "workspace indexing" in text:
                self._indexing_started.set()

    def _fail_all_pending(self, reason: str):
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = reason
            slot["event"].set()
