"""Tool permission & parameter-scoping policy.

Implements checklist §2.2 (confirmation for destructive actions) and
§2.3 (scope tool parameters) in one place so the rules are easy to
audit and extend.

Three layers, evaluated in order by ``check_tool_policy`` before the
mode-based permission decision in ``agent.check_permission``:

1. Path scoping  - reject path-bearing tool args that escape the
   working directory (generalized from the write-only check that
   existed before).
2. Shell policy  - parse ``run_bash`` commands with ``shlex`` and apply
   a denylist of binaries and a regex denylist of dangerous patterns.
3. Web policy    - reject SSRF targets (cloud metadata, loopback,
   link-local, RFC1918 private ranges).

Each layer returns ``(allowed: bool, reason: str | None)``.  When a
layer rejects, the call is blocked regardless of the permission mode.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
import socket
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 2.2 Destructive-action classification
# ---------------------------------------------------------------------------

# Tools whose side effects mutate state outside the agent's own memory.
DESTRUCTIVE_TOOLS = frozenset({
    "run_bash",
    "write_file",
    "edit_file",
})

# Tools that ALWAYS require explicit human confirmation, even under
# ``dangerouslySkipPermissions``.  These are the irreversible / exfil
# class — we refuse them outright (a denylist), not just prompt for them.
#
# ``run_bash`` is not in this set as a whole; instead its *command* is
# screened by the shell policy below.  ``webfetch`` is also policy-
# screened (SSRF).  This set is reserved for tools where *any* call is
# too dangerous to auto-run.
ALWAYS_CONFIRM_TOOLS = frozenset({
    # Currently empty — kept for future destructive tools (e.g. delete_file,
    # send_email).  The shell policy handles the dangerous run_bash cases.
})

# Argument patterns that make an otherwise-allowed tool require
# confirmation regardless of mode.  Each entry is (tool_name, regex).
# The regex is matched (case-insensitive) against the JSON-serialized
# args dict.
ALWAYS_CONFIRM_ARG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # rm -rf with a broad or root target
    ("run_bash", re.compile(r"\brm\s+-rf?\s+(/|~|\*|\$HOME|\.\.)", re.I)),
    # force-push to git
    ("run_bash", re.compile(r"\bgit\s+push\s+(-f|--force)", re.I)),
    # privilege escalation
    ("run_bash", re.compile(r"\bsudo\b|\bsu\b\s+", re.I)),
    # any use of docker from inside the sandbox (escape risk)
    ("run_bash", re.compile(r"\bdocker\b", re.I)),
    # chmod world-writable
    ("run_bash", re.compile(r"\bchmod\s+[-\d]*7\d\d?", re.I)),
    # network exfiltration tools
    ("run_bash", re.compile(r"\b(curl|wget|nc|netcat|ncat)\b", re.I)),
    # overwriting a file with empty content = delete
    ("write_file", re.compile(r'"content"\s*:\s*"\s*"')),
]


def is_destructive(tool_name: str, args: dict[str, Any]) -> bool:
    """Return True if the call is in the destructive class."""
    if tool_name in DESTRUCTIVE_TOOLS:
        return True
    for name, pat in ALWAYS_CONFIRM_ARG_PATTERNS:
        if name == tool_name and pat.search(_args_blob(args)):
            return True
    # write_file emptying an existing file is treated as a delete in
    # the caller; here we flag the empty-content case.
    return False


def always_confirm_required(tool_name: str, args: dict[str, Any]) -> bool:
    """Return True if the call must prompt the user regardless of mode."""
    if tool_name in ALWAYS_CONFIRM_TOOLS:
        return True
    for name, pat in ALWAYS_CONFIRM_ARG_PATTERNS:
        if name == tool_name and pat.search(_args_blob(args)):
            return True
    return False


def _args_blob(args: dict[str, Any]) -> str:
    import json
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


# ---------------------------------------------------------------------------
# 2.3 Path scoping
# ---------------------------------------------------------------------------

# Tools that take a filesystem path argument.  The value is the key in
# the args dict that holds the path.
PATH_TOOLS: dict[str, str] = {
    "read_file": "path",
    "glob_files": "path",
    "grep": "path",
    "write_file": "path",
    "edit_file": "path",
}


def check_path_scope(
    tool_name: str,
    args: dict[str, Any],
    working_dir: Path,
) -> tuple[bool, str | None]:
    """Reject path-bearing tool calls whose target escapes working_dir.

    Note: the Docker mount already constrains the *container's* view of
    the filesystem; this check is a defense-in-depth layer on the host
    side so a malicious path is rejected before it ever reaches docker.
    """
    if tool_name not in PATH_TOOLS:
        return True, None
    raw = args.get(PATH_TOOLS[tool_name])
    if not raw:
        return True, None  # missing arg is a schema problem, not a scope problem
    try:
        target = Path(raw)
        if not target.is_absolute():
            target = working_dir / target
        target.resolve().relative_to(working_dir.resolve())
        return True, None
    except (ValueError, OSError, RuntimeError) as e:
        return False, (
            f"Path '{raw}' is outside the working directory "
            f"({working_dir}). File tools may only touch paths inside "
            f"the project root. ({e})"
        )


# ---------------------------------------------------------------------------
# 2.3 Shell policy (run_bash denylist)
# ---------------------------------------------------------------------------

# Binaries that must never be executed by the agent's shell tool, even
# under dangerouslySkipPermissions.  Used as a hard denylist.
SHELL_DENYLIST_BINARIES = frozenset({
    "docker",      # sandbox escape / host control
    "sudo", "su",  # privilege escalation
    "nc", "netcat", "ncat",  # reverse shells / exfil
    "curl", "wget",  # exfil / SSRF — handled here AND in web policy
    "chmod", "chown",  # permission tampering
    "mkfs", "dd",  # destructive disk ops
    "shutdown", "reboot", "halt", "poweroff",
    "systemctl", "service",
    "crontab", "at",
})

# Dangerous patterns matched against the raw command string.
SHELL_DENYLIST_PATTERNS = [
    (re.compile(r"\brm\s+-rf?\s+(/|~|\*|\$HOME|\.\.)", re.I),
     "recursive delete of a broad or root target"),
    (re.compile(r">\s*/etc/", re.I),
     "redirect into /etc/ (system files)"),
    (re.compile(r"\bmkfs\b", re.I), "filesystem format command"),
    (re.compile(r"\bdd\b\s+if=", re.I), "raw disk write via dd"),
    (re.compile(r":\(\)\s*\{", re.I), "fork-bomb pattern"),
    (re.compile(r"\b(eval|exec)\b", re.I),
     "eval/exec in a shell command (injection risk)"),
    (re.compile(r">\s*/dev/sd", re.I), "write to a block device"),
    (re.compile(r"\bhistory\s+-c\b", re.I), "history wipe"),
    (re.compile(r"\bexport\s+PATH=", re.I),
     "PATH override (could shadow binaries)"),
]


def check_shell_policy(command: str) -> tuple[bool, str | None]:
    """Screen a ``run_bash`` command against the denylist."""
    if not command or not command.strip():
        return True, None

    # Pattern check first (catches "rm -rf /" regardless of binary).
    for pat, reason in SHELL_DENYLIST_PATTERNS:
        if pat.search(command):
            return False, f"Blocked by shell policy: {reason}."

    # Tokenize and inspect the leading binary of each pipeline segment.
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unparseable (e.g. unbalanced quotes) — let the shell itself
        # reject it, but flag for confirmation.
        return False, "Shell command could not be parsed (unbalanced quotes)."

    for tok in tokens:
        if tok in ("|", "||", "&&", ";"):
            continue
        if tok.startswith("-"):
            continue  # flag
        binary = Path(tok).name
        if binary in SHELL_DENYLIST_BINARIES:
            return False, (
                f"Blocked by shell policy: binary '{binary}' is on the "
                f"denylist for run_bash."
            )
        # First non-flag token is the command; after that, subsequent
        # bare tokens are arguments.  We only need to check each token
        # against the denylist once.
    return True, None


# ---------------------------------------------------------------------------
# 2.3 Web policy (SSRF guard)
# ---------------------------------------------------------------------------

# Hosts that must never be fetched, regardless of mode.
WEB_DENYLIST_HOSTS = frozenset({
    "169.254.169.254",       # AWS / GCP / Azure cloud metadata
    "metadata.google.internal",  # GCP metadata
    "metadata.azure.com",    # Azure metadata
    "0.0.0.0",
    "::1",
    "localhost",
})


def check_web_policy(url: str) -> tuple[bool, str | None]:
    """Reject URLs that target loopback / link-local / private ranges."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return False, f"Unparseable URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported scheme '{parsed.scheme}'."
    host = parsed.hostname
    if not host:
        return False, "URL has no host component."
    if host.lower() in WEB_DENYLIST_HOSTS:
        return False, f"Blocked host '{host}' (loopback / metadata)."

    # Resolve and check the IP family.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Let the actual fetcher surface the DNS error.
        return True, None
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return False, f"Blocked IP '{ip}' (loopback / link-local / multicast)."
        if addr.is_private:
            return False, f"Blocked IP '{ip}' (RFC1918 private range — SSRF guard)."
    return True, None


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def check_tool_policy(
    tool_name: str,
    args: dict[str, Any],
    working_dir: Path,
) -> tuple[bool, str | None]:
    """Run all policy layers.  Returns (allowed, reason).

    Called by ``agent.check_permission`` BEFORE the mode-based decision.
    A False here is a hard block that no mode can override.
    """
    # Layer 1: path scope.
    ok, reason = check_path_scope(tool_name, args, working_dir)
    if not ok:
        return False, reason

    # Layer 2: shell policy.
    if tool_name == "run_bash":
        ok, reason = check_shell_policy(args.get("command", ""))
        if not ok:
            return False, reason

    # Layer 3: web / SSRF policy.
    if tool_name == "webfetch":
        ok, reason = check_web_policy(args.get("url", ""))
        if not ok:
            return False, reason

    return True, None


__all__ = [
    "DESTRUCTIVE_TOOLS",
    "ALWAYS_CONFIRM_TOOLS",
    "ALWAYS_CONFIRM_ARG_PATTERNS",
    "is_destructive",
    "always_confirm_required",
    "check_path_scope",
    "check_shell_policy",
    "check_web_policy",
    "check_tool_policy",
]
