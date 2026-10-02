"""File-backed long-term memory and resumable conversation sessions."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MemoryStore:
    """Persist agent notes, user preferences, and complete session records."""

    def __init__(self, root: Path):
        self.root = root.expanduser()
        self.sessions_dir = self.root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(exist_ok=True)

    def read_notes(self, scope: str) -> str:
        path = self._notes_path(scope)
        return path.read_text(encoding="utf-8") if path.exists() else "(empty)"

    def append_notes(self, scope: str, content: str) -> str:
        path = self._notes_path(scope)
        with path.open("a", encoding="utf-8") as file:
            if path.stat().st_size:
                file.write("\n")
            file.write(content.strip() + "\n")
        return f"Saved {scope} memory."

    def create_session(self, session_id: str, messages: list[dict]) -> None:
        now = _timestamp()
        self._write_session(session_id, {
            "id": session_id,
            "created_at": now,
            "updated_at": now,
            "summary": "",
            "messages": _serializable_messages(messages),
        })

    def load_session(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.exists():
            raise ValueError(f"No saved session with id '{session_id}'.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        session = self.load_session(session_id)
        session["messages"] = _serializable_messages(messages)
        session["updated_at"] = _timestamp()
        self._write_session(session_id, session)

    def write_summary(self, session_id: str, summary: str) -> str:
        session = self.load_session(session_id)
        session["summary"] = summary.strip()
        session["updated_at"] = _timestamp()
        self._write_session(session_id, session)
        return "Saved session summary."

    def ensure_summary(self, session_id: str) -> None:
        session = self.load_session(session_id)
        if session.get("summary", "").strip():
            return
        excerpts = []
        for message in session.get("messages", []):
            if message.get("role") not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if content:
                excerpts.append(f"{message['role']}: {content}")
        session["summary"] = "\n".join(excerpts[-6:])[:2000] or "Session ended without messages."
        session["updated_at"] = _timestamp()
        self._write_session(session_id, session)

    def list_sessions(self) -> list[dict[str, str]]:
        sessions = []
        for path in self.sessions_dir.glob("*.json"):
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
                sessions.append({
                    "id": session["id"],
                    "updated_at": session["updated_at"],
                    "summary": session.get("summary", ""),
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def search_sessions(self, query: str) -> list[dict[str, str]]:
        terms = set(re.findall(r"\w+", query.lower()))
        matches = []
        for session in self.list_sessions():
            record = self.load_session(session["id"])
            haystack = json.dumps(record, ensure_ascii=False).lower()
            if all(term in haystack for term in terms):
                matches.append(session)
        return matches

    def _notes_path(self, scope: str) -> Path:
        if scope not in {"personal", "user"}:
            raise ValueError("scope must be 'personal' or 'user'")
        return self.root / f"{scope}.md"

    def _session_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("session id contains invalid characters")
        return self.sessions_dir / f"{session_id}.json"

    def _write_session(self, session_id: str, data: dict[str, Any]) -> None:
        path = self._session_path(session_id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)


def _serializable_messages(messages: list[Any]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        if isinstance(message, dict):
            result.append(message)
        elif hasattr(message, "model_dump"):
            result.append(message.model_dump(exclude_none=True))
        else:
            raise TypeError(f"Cannot persist message of type {type(message).__name__}")
    return result


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
