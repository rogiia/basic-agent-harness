"""Secret & credential management (checklist §5).

Three layers:

  §5.1  Never put secrets in the system prompt.
         - ``scan_environment_for_secrets`` runs at startup and warns
           about env vars whose names look like credentials.
         - ``audit_system_prompt`` statically checks a prompt string for
           f-string interpolation of env vars or known secret names.

  §5.2  Credential injection at the harness level.
         - ``build_container_env`` returns the minimal environment dict
           passed to the sandbox container — only an allowlist of vars
           the agent genuinely needs, never raw host credentials.
         - ``CREDENTIAL_MOUNT_PATHS`` lists host files/dirs that must
           NOT be bind-mounted into the container (``~/.aws``,
           ``~/.ssh``, ``~/.netrc``, …).

  §5.3  Rotate credentials per session.
         - ``SessionCredentials`` generates a fresh per-session token
           via ``secrets.token_urlsafe``; the harness can pass it to
           the container and use it to authenticate any harness-side
           tool calls.  Rotation = recreating the container, which the
           sandbox already does once per session.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 5.1 Never put secrets in the system prompt
# ---------------------------------------------------------------------------

# Env-var name pattern that *looks* like a secret.  Matching is
# case-insensitive.  False positives (e.g. ``PRINTER_SETTINGS``) are
# fine — we only warn, we don't block.
SECRET_ENV_PATTERN = re.compile(
    r"(.*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|APIKEY|API_KEY|AUTH).*)",
    re.IGNORECASE,
)

# Pattern that detects f-string / str.format interpolation of os.environ
# or os.getenv inside a system prompt template.  We flag these so the
# author can confirm no secret value leaks into the model's context.
PROMPT_INTERPOLATION_PATTERN = re.compile(
    r"\{.*(?:os\.environ|os\.getenv|getenv|environ).*\}",
    re.IGNORECASE,
)


def scan_environment_for_secrets() -> list[tuple[str, str]]:
    """Return a list of ``(name, source)`` for env vars that look like secrets.

    ``source`` is ``"env"`` (a real environment variable).  This is a
    startup warning only — it does not modify the environment.  The
    caller (the harness) decides whether to scrub them before starting
    the sandbox container (see ``build_container_env``).
    """
    found: list[tuple[str, str]] = []
    for name in sorted(os.environ):
        if SECRET_ENV_PATTERN.match(name):
            found.append((name, "env"))
    return found


def audit_system_prompt(prompt: str) -> list[str]:
    """Statically check *prompt* for patterns that could leak secrets.

    Returns a list of warnings (empty if clean).  This catches:

      - ``f"... {os.environ['API_KEY']} ..."``  — interpolating env
        vars directly into the prompt.
      - ``f"... {os.getenv('SECRET')} ..."``    — same, via getenv.
      - Literal occurrences of known secret-looking env-var names.
    """
    warnings: list[str] = []

    if PROMPT_INTERPOLATION_PATTERN.search(prompt):
        warnings.append(
            "System prompt appears to interpolate os.environ / os.getenv. "
            "Never put secrets in the system prompt — the model can leak "
            "them in tool calls or responses."
        )

    # Flag literal occurrences of host secret env-var names.
    for name in os.environ:
        if SECRET_ENV_PATTERN.match(name) and name in prompt:
            warnings.append(
                f"System prompt literally contains the env-var name "
                f"'{name}'. Even if this is the name and not the value, "
                f"its presence may prompt the model to look it up."
            )

    return warnings


# ---------------------------------------------------------------------------
# 5.2 Credential injection at the harness level
# ---------------------------------------------------------------------------

# Host paths that must NEVER be bind-mounted into the sandbox container.
# If any of these exist, the harness warns at startup; the container is
# started without them mounted (Docker doesn't auto-mount them, but we
# double-check our ``docker run`` command never includes them).
CREDENTIAL_MOUNT_PATHS: list[Path] = [
    Path.home() / ".aws",
    Path.home() / ".ssh",
    Path.home() / ".config" / "gcloud",
    Path.home() / ".docker",
    Path.home() / ".netrc",
    Path.home() / ".kube",
    Path.home() / ".gnupg",
]

# Environment variables that the sandbox container is allowed to
# inherit from the host.  Everything else is stripped.  This is the
# "credential injection at the harness level" control: the model never
# sees API keys, but tools that need a host identity (e.g. ``USER`` for
# file ownership) still work.
ALLOWED_CONTAINER_ENV = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "AGENT_SESSION_ID",      # set per-session by the harness (§5.3)
    "AGENT_SESSION_TOKEN",   # short-lived, per-session (§5.3)
})


def check_credential_mounts() -> list[Path]:
    """Return the subset of credential paths that exist on the host.

    These must NOT be mounted into the container.  The harness uses
    this to verify the ``docker run`` command is safe.
    """
    return [p for p in CREDENTIAL_MOUNT_PATHS if p.exists()]


def build_container_env(session_id: str, session_token: str) -> dict[str, str]:
    """Return the minimal environment dict for the sandbox container.

    Only ``ALLOWED_CONTAINER_ENV`` variables are inherited from the
    host; everything else (including any secret-looking env vars) is
    stripped.  The per-session id and token are injected by the
    harness, never by the model.
    """
    env: dict[str, str] = {}
    for name in ALLOWED_CONTAINER_ENV:
        if name in os.environ:
            env[name] = os.environ[name]
    env["AGENT_SESSION_ID"] = session_id
    env["AGENT_SESSION_TOKEN"] = session_token
    return env


# ---------------------------------------------------------------------------
# 5.3 Rotate credentials per session
# ---------------------------------------------------------------------------

class SessionCredentials:
    """Per-session credentials generated and rotated by the harness.

    A fresh ``SessionCredentials`` is created at the start of each
    agent session.  The token is passed to the container via
    ``--env`` (never via a tool schema, never in the system prompt).
    Any harness-authenticated tool (e.g. a future ``github_api`` tool)
    uses this token rather than a long-lived host credential.

    "Rotation" = recreating the container, which ``DockerSandbox`` does
    once per session already.
    """

    def __init__(self) -> None:
        self.session_id = secrets.token_urlsafe(12)
        self.session_token = secrets.token_urlsafe(32)
        self._revoked = False

    def revoke(self) -> None:
        """Mark the session credentials as revoked.

        In the current single-process model this is bookkeeping; in a
        multi-process / server scenario it would also invalidate the
        token in a shared store.
        """
        self._revoked = True
        # Rotate: generate a new token so any stale reference is useless.
        self.session_token = secrets.token_urlsafe(32)

    @property
    def revoked(self) -> bool:
        return self._revoked

    def container_env(self) -> dict[str, str]:
        """Return the env dict to pass to the sandbox container."""
        return build_container_env(self.session_id, self.session_token)

    def __repr__(self) -> str:
        # Never include the token itself in repr/log output.
        return f"SessionCredentials(id={self.session_id!r}, revoked={self._revoked})"


__all__ = [
    "SECRET_ENV_PATTERN",
    "scan_environment_for_secrets",
    "audit_system_prompt",
    "CREDENTIAL_MOUNT_PATHS",
    "ALLOWED_CONTAINER_ENV",
    "check_credential_mounts",
    "build_container_env",
    "SessionCredentials",
]
