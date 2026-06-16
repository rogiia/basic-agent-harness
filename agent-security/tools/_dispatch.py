"""Runs inside the sandbox container and dispatches a single tool call.

Invoked by the host as:

    docker exec -i <container> python /agent_tools/_dispatch.py <tool_name>

The tool arguments are read from stdin as a JSON object; the result is
printed to stdout.  Only the *action* tools (the ones that touch the
filesystem, shell, or network) are dispatched here — the in-memory
planning tools stay on the host.
"""

import json
import sys
import traceback

sys.path.insert(0, "/agent_tools")

import filesystem  # noqa: E402
import shell        # noqa: E402
import web          # noqa: E402

REGISTRY = {
    "read_file":  filesystem.read_file,
    "glob_files": filesystem.glob_files,
    "grep":       filesystem.grep,
    "write_file": filesystem.write_file,
    "edit_file":  filesystem.edit_file,
    "run_bash":   shell.run_bash,
    "webfetch":   web.webfetch,
}


def main() -> None:
    if len(sys.argv) < 2:
        print("Error: dispatch requires a tool name argument.")
        return

    name = sys.argv[1]
    if name not in REGISTRY:
        print(f"Error: tool '{name}' is not available inside the container.")
        return

    raw = sys.stdin.read()
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(f"Error: could not parse tool arguments as JSON: {e}")
        return

    try:
        result = REGISTRY[name](**args)
    except Exception as e:
        result = f"Error executing tool '{
            name}' in container: {e}\n" + traceback.format_exc()

    # The tool result is the only thing on stdout; the host captures it.
    sys.stdout.write(result if result is not None else "(no output)")


if __name__ == "__main__":
    main()
