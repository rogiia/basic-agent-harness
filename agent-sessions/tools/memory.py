from memory import MemoryStore


store: MemoryStore | None = None
session_id: str | None = None


def configure_memory(memory_store: MemoryStore, active_session_id: str) -> None:
    global store, session_id
    store = memory_store
    session_id = active_session_id


def read_memory(scope: str) -> str:
    return _get_store().read_notes(scope)


def append_memory(scope: str, content: str) -> str:
    return _get_store().append_notes(scope, content)


def list_sessions() -> str:
    sessions = _get_store().list_sessions()
    if not sessions:
        return "No saved sessions."
    return "\n".join(
        f"{item['id']} | {item['updated_at']} | {item['summary'][:160] or '(no summary)'}"
        for item in sessions
    )


def search_sessions(query: str) -> str:
    sessions = _get_store().search_sessions(query)
    if not sessions:
        return "No matching sessions."
    return "\n".join(
        f"{item['id']} | {item['updated_at']} | {item['summary'][:500] or '(no summary)'}"
        for item in sessions
    )


def read_session(session_id: str) -> str:
    session = _get_store().load_session(session_id)
    transcript = "\n".join(
        f"{message.get('role', 'unknown')}: {message.get('content') or '[tool call]'}"
        for message in session.get("transcript", session.get("messages", []))
    )
    return f"Summary:\n{session.get('summary') or '(no summary)'}\n\nTranscript:\n{transcript[:12000]}"


def write_session_summary(summary: str) -> str:
    if session_id is None:
        raise RuntimeError("Memory has not been configured.")
    return _get_store().write_summary(session_id, summary)


def _get_store() -> MemoryStore:
    if store is None:
        raise RuntimeError("Memory has not been configured.")
    return store
