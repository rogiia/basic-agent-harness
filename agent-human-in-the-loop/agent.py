import argparse
import os
import json
from enum import Enum
from pathlib import Path
from openai import OpenAI
from tools import get_tool_registry, get_tool_schemas
from dotenv import load_dotenv

load_dotenv()

TOOL_REGISTRY = get_tool_registry()
TOOL_SCHEMAS = get_tool_schemas()

# ---------------------------------------------------------------------------
# Permission modes
# ---------------------------------------------------------------------------

class PermissionMode(Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    DANGEROUSLY_SKIP_PERMISSIONS = "dangerouslySkipPermissions"


# Always allowed: read-only filesystem tools
READ_TOOLS = {"read_file", "glob_files", "grep"}

# Always allowed: internal planning/bookkeeping and user-interaction tools (no external side effects)
PLANNING_TOOLS = {"todo_append", "todo_list", "todo_update", "read_scratchpad", "write_scratchpad", "ask_question"}

# Conditionally allowed in acceptEdits mode when target is within working dir
WRITE_TOOLS = {"write_file", "edit_file"}


def _resolve_tool_path(tool_name: str, args: dict) -> str | None:
    """Return the file-path argument for write tools, or None if not applicable."""
    if tool_name in WRITE_TOOLS:
        return args.get("path")
    return None


def _is_within_working_dir(path: str, working_dir: Path) -> bool:
    """Return True if *path* resolves to somewhere inside *working_dir*."""
    try:
        target = Path(path)
        if not target.is_absolute():
            target = working_dir / target
        target.resolve().relative_to(working_dir.resolve())
        return True
    except ValueError:
        return False


def _ask_permission(tool_name: str, args: dict) -> bool:
    """Interactively ask the user whether to allow a tool call.

    Returns True if the user grants permission, False otherwise.
    """
    print(f"\n  [permission required] {tool_name}")
    print(f"  Arguments: {json.dumps(args, ensure_ascii=False)}")
    while True:
        try:
            answer = input("  Allow this action? [y/n]: ").strip().lower()
        except EOFError:
            print("  (EOF — denying permission)")
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.")


def check_permission(
    tool_name: str,
    args: dict,
    mode: PermissionMode,
    working_dir: Path,
) -> bool:
    """Decide whether a tool call is permitted under the current mode.

    May interactively prompt the user when a decision cannot be made
    automatically.  Returns True if the tool call should proceed.

    Permission rules
    ----------------
    default
        Read tools and planning tools run freely.  Every other tool
        requires explicit user approval.

    acceptEdits
        Read tools and planning tools run freely.  Write tools
        (write_file, edit_file) run freely only when the target path is
        inside the working directory; otherwise the user is prompted.
        All other tools require explicit user approval.

    dangerouslySkipPermissions
        All tools run without any prompt.
    """
    # Planning and read tools are always free regardless of mode
    if tool_name in READ_TOOLS or tool_name in PLANNING_TOOLS:
        return True

    if mode == PermissionMode.DANGEROUSLY_SKIP_PERMISSIONS:
        return True

    if mode == PermissionMode.ACCEPT_EDITS and tool_name in WRITE_TOOLS:
        path = _resolve_tool_path(tool_name, args)
        if path and _is_within_working_dir(path, working_dir):
            return True  # auto-approved — within the working directory
        # Path is outside the working directory → fall through to ask

    # Default mode, or acceptEdits for non-write / out-of-tree tools
    return _ask_permission(tool_name, args)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def get_llm_client():
    return OpenAI(
        base_url="http://localhost:11434/v1",
        api_key="."
    )


def handle_tool_calls(
    tool_calls,
    messages,
    mode: PermissionMode,
    working_dir: Path,
):
    """Execute each tool the LLM requested and append the results to messages."""
    for tool_call in tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        print(f"  [tool] {name}({args})")

        if name not in TOOL_REGISTRY:
            result = (
                f"Error: unknown tool '{name}'. "
                f"Available tools: {list(TOOL_REGISTRY.keys())}"
            )
        elif not check_permission(name, args, mode, working_dir):
            result = (
                f"Permission denied: the user did not allow '{name}' to run. "
                "Do not retry this tool call without asking the user first."
            )
        else:
            try:
                result = TOOL_REGISTRY[name](**args)
            except TypeError as e:
                result = (
                    f"Error: invalid arguments for tool '{name}': {e}. "
                    "Check the tool schema and retry with the correct arguments."
                )

        print(f"  [tool result] {result[:200]}{'...' if len(result) > 200 else ''}")

        # The LLM needs the result tied back to the specific tool call id
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })


def agent_loop(client, mode: PermissionMode, working_dir: Path):
    messages = [
        {
            "role": "system",
            "content": (
                "You are a capable coding and research assistant.\n\n"

                "## Available tools\n\n"
                "Action tools: read_file, write_file, edit_file, glob_files, grep, run_bash, webfetch\n\n"
                "Planning tools:\n"
                "- Scratchpad (read_scratchpad / write_scratchpad): your private working memory. "
                "Use it to think through an approach, store intermediate findings, or draft content "
                "before committing. Each write fully replaces the previous content.\n"
                "- To-do list (todo_append / todo_list / todo_update): a persistent task tracker. "
                "Items carry a status: pending, in_progress, done, cancelled, or failed.\n"
                "- Clarification (ask_question): ask the user a single focused question when you "
                "are genuinely blocked and cannot reasonably infer the missing information from "
                "context. Do not use it for progress updates or to confirm actions you can already "
                "take — only ask when it is strictly necessary to proceed.\n\n"

                "## Working directory\n\n"
                "The current working directory is always the user's project root. "
                "When asked to work on a project or codebase without a specified path, "
                "start by exploring '.' with glob_files or run_bash. "
                "Never ask the user to supply a path.\n\n"

                "## How to plan\n\n"
                "For complex or multi-step tasks (roughly 3 or more distinct steps, or when the "
                "path forward is unclear):\n"
                "1. Write your initial thinking and approach to the scratchpad before acting.\n"
                "2. Break the work into concrete steps and add each one to the to-do list with "
                "todo_append (status: pending).\n"
                "3. Before starting a step, mark it in_progress with todo_update. "
                "Keep only one item in_progress at a time.\n"
                "4. Mark items done immediately after completing them — do not batch completions.\n"
                "5. Call todo_list to review remaining work before moving to the next step.\n"
                "6. Mark tasks cancelled if they become unnecessary.\n\n"
                "For simple, single-step tasks: act directly without creating todos.\n\n"
                "Planning tool calls (write_scratchpad, todo_append, todo_update, todo_list) "
                "are internal bookkeeping, not responses to the user. After any planning tool "
                "call, always continue working immediately — make your next tool call or, once "
                "the task is fully complete, give a substantive final answer. "
                "Never emit an empty or whitespace-only message.\n\n"
                "## Replanning\n\n"
                "After every tool result, check whether the outcome matched your expectation. "
                "If a tool returns an error, unexpected output, or reveals information that "
                "changes your understanding of the task, do not move to the next planned step — "
                "replan first.\n\n"
                "When a step fails:\n"
                "1. Diagnose in the scratchpad — is this a recoverable input error (wrong path, "
                "typo, wrong argument) or a deeper problem (wrong approach, wrong assumption)?\n"
                "2. Mark the task failed: todo_update(id, status='failed').\n"
                "3. Choose a recovery action:\n"
                "   - Retry: the failure is correctable. Fix the input and set the task back to "
                "in_progress. The tool will report which retry attempt this is.\n"
                "   - Replace: the approach is wrong. Cancel the task and add a revised one.\n"
                "   - Reorder: new information makes a different task more urgent. Update the "
                "pending items before continuing.\n"
                "4. If todo_update reports that the retry limit has been reached, stop retrying. "
                "Write a clear diagnosis in the scratchpad — what you tried, what failed each "
                "time, and what you need — then give the user a concise escalation message "
                "and wait for their input.\n\n"
                "When a tool succeeds but returns information that changes the picture, pause "
                "before acting. Call todo_list, reassess all pending items in the scratchpad, "
                "and cancel or replace any tasks that no longer make sense.\n\n"
                "## How to use the scratchpad\n\n"
                "Before each tool call during a complex task, update the scratchpad with your "
                "current thinking. Structure each entry around these five steps:\n\n"
                "1. Restate the goal — write what you understand the task to be, in your own words. "
                "This catches misreads before they compound into wasted work.\n"
                "2. Survey what you know — note which files you have seen, what the code structure "
                "looks like, and what constraints or requirements apply.\n"
                "3. Evaluate options — reason through at least two approaches and explain why you "
                "are choosing one over the other (e.g. 'I could rewrite the middleware, or wrap it. "
                "Wrapping is safer because it leaves the existing call sites untouched.').\n"
                "4. Anticipate failure modes — write down what could go wrong with the chosen "
                "approach and how you would diagnose it (e.g. 'If the tests fail after this, the "
                "most likely cause is that the session cookie name changed.').\n"
                "5. Decide the next single action — commit to exactly one tool call. "
                "Do not plan several calls at once; decide the next step only.\n\n"
                "Re-read the scratchpad whenever you resume after a tool result to keep your "
                "reasoning grounded in what you have already learned.\n\n"
                "## Done detection\n\n"
                "Do not give a final answer based on the task list being empty alone. "
                "Before declaring the task complete, verify all three of the following:\n\n"
                "1. Structural completion — call todo_list and confirm there are no pending, "
                "in_progress, or failed items.\n"
                "2. Verification — check the output against the original goal. For code tasks: "
                "run the tests or build with run_bash and confirm they pass. For research tasks: "
                "re-read the scratchpad and confirm the assembled answer addresses what was "
                "actually asked.\n"
                "3. Uncertainty check — read the scratchpad and ask: are there unresolved "
                "questions, assumptions that were never validated, or tasks that were cancelled "
                "rather than properly completed?\n\n"
                "If all three are satisfied, give your final answer. If any are not, re-enter "
                "the planning loop — add the outstanding items to the todo list and continue."
            ),
        }
    ]

    while True:
        user_input = input("You: ")
        if user_input.lower() == "\\exit":
            break

        messages.append({"role": "user", "content": user_input})

        # Keep looping until the LLM stops calling tools and gives a final reply
        while True:
            response = client.chat.completions.create(
                model="gemma4",
                messages=messages,
                tools=TOOL_SCHEMAS,
                temperature=0.7,
            )

            message = response.choices[0].message

            # Always append the assistant turn so the conversation stays intact
            messages.append(message)

            if message.tool_calls:
                # The LLM wants to use one or more tools — run them, then loop
                handle_tool_calls(message.tool_calls, messages, mode, working_dir)
            elif not message.content or not message.content.strip():
                # The model ended its turn with an empty message — most commonly
                # happens after a planning-only tool call (scratchpad / todo).
                # Nudge it to continue rather than silently stalling.
                messages.append({
                    "role": "user",
                    "content": "Continue.",
                })
            else:
                # No tool calls: we have the final answer
                print(f"Assistant: {message.content}")
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Coding agent with configurable tool permission gating."
    )
    parser.add_argument(
        "--mode",
        choices=["default", "acceptEdits", "dangerouslySkipPermissions"],
        default="default",
        help=(
            "Permission mode for tool execution. "
            "'default': read tools are free, everything else requires approval. "
            "'acceptEdits': read + write tools are free when inside the working directory, "
            "everything else requires approval. "
            "'dangerouslySkipPermissions': all tools run without any prompt."
        ),
    )
    cli_args = parser.parse_args()

    mode = PermissionMode(cli_args.mode)
    working_dir = Path.cwd()

    print(f"Agent started in '{mode.value}' mode  (working dir: {working_dir})")

    client = get_llm_client()
    agent_loop(client, mode, working_dir)
