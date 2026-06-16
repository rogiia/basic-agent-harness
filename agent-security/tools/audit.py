"""Append-only JSONL audit log for agent sessions.

Every event is written as one JSON object per line, with an auto-added
ISO timestamp and session id.  The log is flushed after every record so
that a crash still leaves a complete trail.  The file alone is enough
to reconstruct what the agent did, in order.

§2.4: tool results are truncated to ``MAX_RESULT_BYTES`` in the log to
prevent unbounded growth; a SHA-256 and full length are stored alongside
so the truncated entry is self-describing and tamper-evident.

§6.1: dedicated events for every decision step —
``log_permission_decision``, ``log_llm_request``, ``log_llm_response``,
``log_session_abort`` — give full forensic replay without guessing.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Tool results larger than this are stored truncated in the audit log.
MAX_RESULT_BYTES = 8 * 1024


def _truncate_for_log(result: str) -> dict:
    """Return a dict describing *result*, truncating if large.

    The dict always has ``size`` (bytes) and ``sha256``.  If the result
    is short enough it is included verbatim under ``result``; otherwise
    only the first ``MAX_RESULT_BYTES`` are kept under ``result_truncated``.
    """
    raw = result if isinstance(result, str) else str(result)
    encoded = raw.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    size = len(encoded)
    if size <= MAX_RESULT_BYTES:
        return {"result": raw, "size": size, "sha256": digest}
    truncated = encoded[:MAX_RESULT_BYTES].decode("utf-8", errors="replace")
    return {
        "result_truncated": truncated,
        "truncated_from_size": size,
        "sha256": digest,
    }


class AuditLog:
    """Append-only JSONL audit log of agent activity."""

    def __init__(self, log_dir: Path, session_id: str | None = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.log_dir / f"audit-{self.session_id}-{ts}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")
        self.log("session_start", log_file=str(self.path))

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session_id,
            "event": event,
        }
        record.update(fields)
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    # -- convenience wrappers ------------------------------------------

    def log_config(self, **fields: Any) -> None:
        """Record the session configuration (mode, sandbox root, etc.)."""
        self.log("config", **fields)

    def log_user_message(self, content: str) -> None:
        self.log("user_message", content=content)

    def log_assistant_message(self, content: str | None, tool_calls) -> None:
        calls = []
        if tool_calls:
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = tc.function.arguments
                calls.append({"id": tc.id, "name": tc.function.name, "args": args})
        self.log("assistant_message", content=content, tool_calls=calls)

    def log_tool_result(
        self,
        tool_call_id: str,
        name: str,
        args: dict,
        permission_allowed: bool,
        container_error: bool,
        container_reason: str | None,
        result: str,
        intent_drift: bool = False,
        intent_reason: str | None = None,
        permission_reason: str | None = None,
    ) -> None:
        self.log(
            "tool_result",
            tool_call_id=tool_call_id,
            tool=name,
            args=args,
            permission_allowed=permission_allowed,
            permission_reason=permission_reason,
            container_error=container_error,
            container_reason=container_reason,
            intent_drift=intent_drift,
            intent_reason=intent_reason,
            **_truncate_for_log(result),
        )

    # -- §6.1 decision-step logging ----------------------------------

    def log_permission_decision(
        self,
        tool: str,
        args: dict,
        mode: str,
        allowed: bool,
        reason: str | None = None,
    ) -> None:
        """Record a standalone permission decision (§6.1).

        This is emitted *before* the tool runs (or is refused), so the
        audit trail shows the decision and its reason even if the
        subsequent tool execution crashes.
        """
        self.log(
            "permission_decision",
            tool=tool,
            args=args,
            mode=mode,
            allowed=allowed,
            reason=reason,
        )

    def log_llm_request(
        self,
        model: str,
        message_count: int,
        token_estimate: int,
        has_tools: bool,
    ) -> None:
        """Record that an LLM request is about to be sent (§6.1)."""
        self.log(
            "llm_request",
            model=model,
            message_count=message_count,
            token_estimate=token_estimate,
            has_tools=has_tools,
        )

    def log_llm_response(
        self,
        model: str,
        finish_reason: str | None,
        usage: dict | None = None,
        message_hash: str | None = None,
        tool_call_count: int = 0,
    ) -> None:
        """Record the LLM response metadata (§6.1).

        ``message_hash`` is a SHA-256 of the assistant message content
        so the full conversation can be verified for forensic replay
        without storing every token in the log.
        """
        self.log(
            "llm_response",
            model=model,
            finish_reason=finish_reason,
            usage=usage,
            message_hash=message_hash,
            tool_call_count=tool_call_count,
        )

    def log_session_abort(self, reason: str) -> None:
        """Record that the session was aborted (§6.3)."""
        self.log("session_abort", reason=reason)

    def close(self) -> None:
        self.log("session_end")
        self._fh.close()
