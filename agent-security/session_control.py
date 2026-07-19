"""Session-level abort & kill switches (checklist §6.3).

Three pieces:

  1. ``AbortController`` — a thread-safe flag set by a signal handler
     (SIGINT / SIGTERM) or by any code path that detects a fatal
     condition (cost cap, iteration cap, user request).

  2. ``FileRollback`` — snapshots original file bytes before each
     write/edit so that an abort can offer to revert reversible state.

  3. ``kill_in_flight`` — a helper that sends SIGINT to any python
     process running inside the sandbox container, stopping a hanging
     ``docker exec`` without tearing down the container itself.

The controller is checked:
  - at the top of the inner agent loop (between LLM turns),
  - between tool calls inside ``handle_tool_calls``,
  - before each LLM request.
"""

from __future__ import annotations

import hashlib
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 6.3 AbortController
# ---------------------------------------------------------------------------

class AbortController:
    """Thread-safe abort flag for the agent session.

    A single instance is created per session.  Signal handlers (SIGINT,
    SIGTERM) call ``trigger()``; the agent loop polls ``triggered``
    between turns and between tool calls.

    Once triggered:
      - the inner loop stops dispatching new tools,
      - any in-flight tool is killed (``kill_in_flight``),
      - a ``session_abort`` audit event is emitted,
      - reversible file changes are offered for rollback.
    """

    def __init__(self) -> None:
        self._triggered = False
        self._reason: str | None = None
        self._lock = threading.Lock()
        self._registered_signals: list[int] = []

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def trigger(self, reason: str = "abort requested") -> None:
        """Set the abort flag.  Safe to call from a signal handler."""
        with self._lock:
            if not self._triggered:
                self._triggered = True
                self._reason = reason

    def reset(self) -> None:
        """Clear the flag (used by tests)."""
        with self._lock:
            self._triggered = False
            self._reason = None

    def install_signal_handlers(self) -> None:
        """Register SIGINT / SIGTERM handlers that call ``trigger``.

        Only call this from the main thread (signal.signal requirement).
        The previous handlers are not saved — we deliberately replace
        them because the abort controller is the final arbiter.
        """
        def _handler(signum, frame):
            name = signal.Signals(signum).name
            self.trigger(f"received {name}")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
                self._registered_signals.append(sig)
            except (ValueError, OSError):
                # Not in main thread, or signal not supported on this
                # platform — skip silently.
                pass

    def remove_signal_handlers(self) -> None:
        """Restore default signal handling."""
        for sig in self._registered_signals:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        self._registered_signals.clear()


# ---------------------------------------------------------------------------
# 6.3 kill_in_flight
# ---------------------------------------------------------------------------

def kill_in_flight(container: str) -> None:
    """Send SIGINT to any python process inside the sandbox container.

    This stops a hanging ``docker exec`` (e.g. a long ``run_bash``)
    without tearing down the container itself, so the ``finally`` block
    can still clean up.
    """
    try:
        subprocess.run(
            ["docker", "exec", container, "pkill", "-INT", "python"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        # Best-effort: if the container is already gone or pkill isn't
        # available, the exec subprocess's own timeout (§4.3) will
        # eventually clean up.
        pass


# ---------------------------------------------------------------------------
# 6.3 FileRollback
# ---------------------------------------------------------------------------

class FileRollback:
    """Snapshot original file bytes before each write/edit (§6.3).

    On abort, ``offer_rollback`` walks the snapshot list and restores
    each file to its pre-edit state, prompting the user for
    confirmation.

    Only ``write_file`` and ``edit_file`` are reversible; ``run_bash``
    side effects (e.g. ``git commit``) are not — the audit log is the
    only record for those.
    """

    def __init__(self) -> None:
        # path -> backup path (in a temp dir)
        self._snapshots: list[tuple[str, Path]] = []
        self._backup_dir: Path | None = None

    def _ensure_backup_dir(self) -> Path:
        if self._backup_dir is None:
            self._backup_dir = Path(__file__).resolve().parent / ".rollback_backups"
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        return self._backup_dir

    def snapshot(self, path: str) -> None:
        """Save a copy of *path* if it exists, for later rollback."""
        p = Path(path)
        if not p.exists() or not p.is_file():
            return
        try:
            backup = self._ensure_backup_dir() / (
                hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:16]
                + "_" + p.name
            )
            shutil.copy2(p, backup)
            self._snapshots.append((str(p.resolve()), backup))
        except OSError:
            # If we can't snapshot, we just can't roll back — don't
            # block the tool call.
            pass

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)

    def offer_rollback(self) -> int:
        """Prompt the user to revert all snapshotted files.

        Returns the number of files actually restored.
        """
        if not self._snapshots:
            print("  [rollback] No reversible file changes to roll back.")
            return 0

        print(f"\n  [rollback] {len(self._snapshots)} file(s) were modified "
              "during this session.")
        try:
            answer = input("  Revert all changes? [y/n]: ").strip().lower()
        except EOFError:
            answer = "n"

        if answer not in ("y", "yes"):
            print("  [rollback] Keeping changes.")
            return 0

        restored = 0
        for original_path, backup_path in self._snapshots:
            try:
                shutil.copy2(backup_path, original_path)
                restored += 1
            except OSError as e:
                print(f"  [rollback] Could not restore {original_path}: {e}")
        print(f"  [rollback] Restored {restored} file(s).")
        return restored

    def cleanup(self) -> None:
        """Remove the backup directory."""
        if self._backup_dir and self._backup_dir.exists():
            shutil.rmtree(self._backup_dir, ignore_errors=True)


__all__ = [
    "AbortController",
    "kill_in_flight",
    "FileRollback",
]
