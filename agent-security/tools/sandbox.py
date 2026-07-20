"""Docker-based sandbox for tool execution.

Instead of confining tools with in-process path checks and a command
denylist (which is only as strong as the checks we remember to write),
the action tools run inside a long-lived Docker container.  The user's
project is bind-mounted into the container; everything outside that
mount is the container's own minimal filesystem and is invisible or
read-only to the tool.  Network egress can be disabled entirely with
``--network none``.

The container is started once per session and reused for every action
tool call (via ``docker exec``) to avoid per-call startup latency.
In-memory planning tools (todo, scratchpad, ask_question) are **not**
run in the container — their state would not survive between separate
``docker exec`` processes — so they stay in-process on the host.

This module requires a Docker-compatible container CLI on the host:
Docker, or Podman (which is auto-detected when ``docker info`` does not
work — note that a shell ``alias docker=podman`` is **not** enough,
because the agent invokes the binary directly via ``subprocess`` without
a shell).  The runtime can also be forced with the ``$AGENT_DOCKER``
environment variable.  On first use the ``agent-security-runner`` image
is built automatically from the ``Dockerfile`` next to this package.
"""

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path


class DockerSandboxError(Exception):
    """Raised when the sandbox container cannot be used to run a tool."""


# Tools that touch the outside world and therefore run in the container.
ACTION_TOOLS = {
    "read_file",
    "glob_files",
    "grep",
    "write_file",
    "edit_file",
    "run_bash",
    "webfetch",
}

DEFAULT_IMAGE = "agent-security-runner"
# §4.3 Default per-tool-call timeout.  30 minutes was far too generous
# and let a hanging command block the whole session.  Lowered to 120 s
# with a --tool-timeout CLI override.
EXEC_TIMEOUT_S = 120


def _runtime_works(cmd: list[str]) -> bool:
    """Return True if the given runtime CLI can talk to its daemon."""
    try:
        return subprocess.run(
            cmd + ["info"], capture_output=True
        ).returncode == 0
    except FileNotFoundError:
        return False


def _resolve_runtime() -> list[str]:
    """Pick the container-runtime command to use.

    Preference order:
      1. ``$AGENT_DOCKER`` (explicit override, e.g. ``podman`` or
         ``/usr/bin/podman``; may include arguments such as ``sudo podman``);
      2. ``docker`` if ``docker info`` succeeds;
      3. ``podman`` if ``podman info`` succeeds;
      4. ``docker`` as a last resort, so the caller raises the standard,
         informative error instead of failing obscurely.
    """
    override = os.environ.get("AGENT_DOCKER", "").strip()
    candidates: list[list[str]] = []
    if override:
        candidates.append(shlex.split(override))
    candidates.append(["docker"])
    candidates.append(["podman"])

    seen: set[tuple[str, ...]] = set()
    for cmd in candidates:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        if _runtime_works(cmd):
            return cmd
    return ["docker"]


# Resolved once at import; re-checked per instance in case the daemon
# was stopped between import and sandbox creation.
DOCKER_CMD = _resolve_runtime()


class DockerSandbox:
    """Manage a long-lived container that executes action tool calls."""

    def __init__(
        self,
        project_root: Path,
        tools_dir: Path,
        network: str = "bridge",
        image: str = DEFAULT_IMAGE,
        build_context: Path | None = None,
        exec_timeout: float = EXEC_TIMEOUT_S,
        container_env: dict | None = None,
    ):
        self.runtime = DOCKER_CMD
        if not _runtime_works(self.runtime):
            raise DockerSandboxError(
                "No container runtime is available on the host. Install "
                "Docker or Podman and ensure its daemon/service is running, "
                "or set $AGENT_DOCKER to the binary to use. (Note: a shell "
                "alias such as `alias docker=podman` is not enough, because "
                "the agent invokes the CLI directly without a shell.)"
            )

        self.project_root = Path(project_root).resolve()
        if not self.project_root.is_dir():
            raise DockerSandboxError(
                f"Project root is not a directory: {self.project_root}"
            )

        self.tools_dir = Path(tools_dir).resolve()
        self.build_context = Path(build_context or self.tools_dir.parent).resolve()
        self.image = image
        self.network = network
        self.exec_timeout = float(exec_timeout)
        # §5.2: the harness — not the model — controls the container env.
        # Only an allowlist of vars is inherited from the host; secret-
        # looking env vars are stripped before the container ever starts.
        self.container_env = container_env or {}
        self.container = f"agent-sandbox-{uuid.uuid4().hex[:8]}"

        self._ensure_image()
        self._start_container()

    # -- image ----------------------------------------------------------

    def _ensure_image(self) -> None:
        inspect = subprocess.run(
            self.runtime + ["image", "inspect", self.image],
            capture_output=True,
        )
        if inspect.returncode == 0:
            return
        dockerfile = self.build_context / "Dockerfile"
        if not dockerfile.exists():
            raise DockerSandboxError(
                f"Cannot build sandbox image: Dockerfile not found at {dockerfile}."
            )
        print(f"  [sandbox] building image '{self.image}' (one-time)...")
        build = subprocess.run(
            self.runtime + ["build", "-t", self.image, str(self.build_context)],
        )
        if build.returncode != 0:
            raise DockerSandboxError(
                f"Failed to build sandbox image '{self.image}' "
                f"(docker build exited {build.returncode})."
            )

    # -- container lifecycle -------------------------------------------

    def _start_container(self) -> None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        gid = os.getgid() if hasattr(os, "getgid") else 0

        cmd = [
            *self.runtime, "run", "-d",
            "--name", self.container,
            "--network", self.network,
            "--user", f"{uid}:{gid}",
            # Mount the project at the same absolute path so paths the
            # agent reports match between host and container.
            "-v", f"{self.project_root}:{self.project_root}",
            # Mount the tool implementations read-only.
            "-v", f"{self.tools_dir}:/agent_tools:ro",
            "-w", str(self.project_root),
        ]

        # §5.2 Credential injection at the harness level: only the
        # allowlisted env vars (set by the harness, never by the model)
        # are passed to the container.  Host credentials are stripped.
        for name, value in self.container_env.items():
            cmd.extend(["-e", f"{name}={value}"])

        cmd.extend([
            "--rm",
            self.image,
            "sleep", "infinity",
        ])
        run = subprocess.run(cmd, capture_output=True, text=True)
        if run.returncode != 0:
            raise DockerSandboxError(
                f"Could not start sandbox container: {run.stderr.strip() or run.stdout.strip()}"
            )

        # Sanity check: confirm the container is actually running.
        ps = subprocess.run(
            self.runtime + ["inspect", "-f", "{{.State.Running}}", self.container],
            capture_output=True, text=True,
        )
        if ps.returncode != 0 or ps.stdout.strip() != "true":
            raise DockerSandboxError(
                f"Sandbox container '{self.container}' is not running after start."
            )

    # -- tool execution ------------------------------------------------

    def run_tool(self, name: str, args: dict) -> str:
        """Execute *name* with *args* inside the container, return its output.

        §4.3: enforces a per-call timeout (``self.exec_timeout``).  A
        timeout is reported back as a ``DockerSandboxError`` with a
        clear message so the LLM knows not to retry blindly.
        """
        try:
            proc = subprocess.run(
                [
                    *self.runtime, "exec", "-i",
                    self.container,
                    "python", "/agent_tools/_dispatch.py", name,
                ],
                input=json.dumps(args),
                capture_output=True,
                text=True,
                timeout=self.exec_timeout,
            )
        except subprocess.TimeoutExpired:
            raise DockerSandboxError(
                f"Tool '{name}' timed out after {self.exec_timeout:.0f}s. "
                "The command did not finish in the allowed time. Do not "
                "retry the same call — adjust the approach or ask the user."
            )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise DockerSandboxError(
                f"Container exec for '{name}' failed (exit {proc.returncode}): {err}"
            )
        return proc.stdout

    def close(self) -> None:
        subprocess.run(
            self.runtime + ["rm", "-f", self.container], capture_output=True
        )
