import argparse
import hashlib
import json
import os
from enum import Enum
from pathlib import Path
from openai import OpenAI
from tools import (
    get_tool_registry,
    get_tool_schemas,
    DockerSandbox,
    DockerSandboxError,
    ACTION_TOOLS,
    AuditLog,
    ToolValidator,
    bounded_schemas,
)
from tools.scratchpad import scratchpad as scratchpad_state
from dotenv import load_dotenv
import prompt_safety
import tool_policy
import resource_limits
import secret_management
import session_control
from resource_limits import (
    IterationCaps,
    ContextBudget,
    CostTracker,
    cap_tool_result,
)
from session_control import AbortController, FileRollback, kill_in_flight

load_dotenv()

# Raw, in-process tool registry. Action tools are replaced below with
# wrappers that dispatch into the Docker sandbox container.
RAW_TOOL_REGISTRY = get_tool_registry()

# Limit output scope: the schemas exposed to the LLM carry bounds
# (min/max on offset/limit, maxLength on command/content/url, relative-
# path format on glob patterns).  These bounds are enforced host-side by
# TOOL_VALIDATOR before any tool is allowed to run.
TOOL_SCHEMAS = bounded_schemas(get_tool_schemas())
TOOL_VALIDATOR = ToolValidator(TOOL_SCHEMAS)

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
PLANNING_TOOLS = {"todo_append", "todo_list", "todo_update",
                  "read_scratchpad", "write_scratchpad", "ask_question"}

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
) -> tuple[bool, str | None]:
    """Decide whether a tool call is permitted under the current mode.

    Returns ``(allowed, reason)``.  When ``allowed`` is False, ``reason``
    explains why and is surfaced to the LLM.  May interactively prompt
    the user when a decision cannot be made automatically.

    Three layers, evaluated in order:

      1. Tool policy (§2.2 / §2.3) — a hard gate that no mode can
         override: path scoping, shell denylist, SSRF guard.
      2. Always-confirm patterns (§2.2) — destructive calls require
         explicit confirmation even in acceptEdits, and selected
         irreversible patterns refuse even in dangerouslySkipPermissions.
      3. Mode-based decision (default / acceptEdits / skip).

    Permission rules (layer 3)
    --------------------------
    default
        Read tools and planning tools run freely.  Every other tool
        requires explicit user approval.

    acceptEdits
        Read tools and planning tools run freely.  Write tools
        (write_file, edit_file) run freely only when the target path is
        inside the working directory; otherwise the user is prompted.
        All other tools require explicit user approval.

    dangerouslySkipPermissions
        All tools run without any prompt — EXCEPT calls blocked by layer
        1 (hard policy) or layer 2 (always-confirm / irreversible).
    """
    # Layer 1: hard policy gate (path scope, shell, SSRF).
    ok, reason = tool_policy.check_tool_policy(tool_name, args, working_dir)
    if not ok:
        return False, reason

    # Layer 2: always-confirm & irreversible patterns.
    # These override even dangerouslySkipPermissions for the worst cases.
    if tool_policy.always_confirm_required(tool_name, args):
        if mode == PermissionMode.DANGEROUSLY_SKIP_PERMISSIONS:
            # Irreversible calls are refused outright in skip mode; the
            # user explicitly accepted risk for normal destructive ops,
            # but not for e.g. `rm -rf /` or `git push --force`.
            return False, (
                f"Blocked even in dangerouslySkipPermissions: {
                    reason or 'irreversible action'}. "
                "Run this command manually outside the agent if it is truly intended."
            )
        # In default / acceptEdits, force an explicit prompt.
        return _ask_permission(f"{tool_name}  [DESTRUCTIVE]", args), None

    # Planning and read tools are always free regardless of mode
    if tool_name in READ_TOOLS or tool_name in PLANNING_TOOLS:
        return True, None

    if mode == PermissionMode.DANGEROUSLY_SKIP_PERMISSIONS:
        return True, None

    if mode == PermissionMode.ACCEPT_EDITS and tool_name in WRITE_TOOLS:
        path = _resolve_tool_path(tool_name, args)
        if path and _is_within_working_dir(path, working_dir):
            # an edit that empties an existing file is a delete —
            # require confirmation rather than auto-approving.
            if _is_delete_via_write(tool_name, args, path):
                return _ask_permission(f"{tool_name}  [DELETE-via-empty]", args), None
            return True, None  # auto-approved — within the working directory
        # Path is outside the working directory → fall through to ask

    # Default mode, or acceptEdits for non-write / out-of-tree tools
    return _ask_permission(tool_name, args), None


def _is_delete_via_write(tool_name: str, args: dict, path: str) -> bool:
    """Return True if a write_file/edit_file call effectively deletes a file.

    - ``write_file`` overwriting an existing file with empty/whitespace
      content is treated as a delete.
    - ``edit_file`` replacing content with empty content on a file that
      becomes empty is also flagged.
    """
    if tool_name == "write_file":
        content = args.get("content", "")
        if not content.strip():
            try:
                if Path(path).exists() and Path(path).stat().st_size > 0:
                    return True
            except OSError:
                pass
    return False


def build_tool_registry(
    sandbox: DockerSandbox,
    allowed_tools: set[str] | None = None,
) -> dict:
    """Return a tool registry routing action tools into the container.

    Planning/stateful tools (todo, scratchpad, ask_question) run
    in-process on the host so their state survives across calls.
    Action tools (filesystem, shell, web) are dispatched into the
    long-lived Docker sandbox container.

    If *allowed_tools* is given, only tools in that set are exposed —
    this is the principle of least privilege (checklist §2.1).  ``None``
    means "expose everything".
    """
    registry = {}
    for name, fn in RAW_TOOL_REGISTRY.items():
        if allowed_tools is not None and name not in allowed_tools:
            continue
        if name in ACTION_TOOLS:
            def _dispatch(_n=name, _s=sandbox, **kwargs):
                return _s.run_tool(_n, kwargs)
            registry[name] = _dispatch
        else:
            registry[name] = fn
    return registry


def filter_tool_schemas(schemas: list[dict], allowed: set[str] | None) -> list[dict]:
    """Return only the schemas whose tool name is in *allowed*.

    ``None`` means "no filtering".
    """
    if allowed is None:
        return schemas
    return [s for s in schemas if s["function"]["name"] in allowed]


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
    tool_registry: dict,
    audit: AuditLog,
    user_goal: str = "",
    iteration_caps: IterationCaps | None = None,
    tool_timeout: float = 120.0,
    abort_controller: AbortController | None = None,
    file_rollback: FileRollback | None = None,
    container_name: str | None = None,
    plan_state: dict | None = None,
):
    """Execute each tool the LLM requested and append the results to messages.

    Every call passes through these layers, in order, and each decision
    is recorded in the audit log:

      0. Abort check (§6.3) + iteration cap (§4.1).
      1. Schema validation (§3.1/§3.3).
      2. Permission gate (§2.2/§2.3) — may prompt the user.
      3. Execution: action tools run inside the Docker sandbox container;
         planning tools run in-process on the host.  A container failure
         or timeout is reported back to the LLM as an error.
      4. File rollback snapshot (§6.3) before write/edit.
    """
    caps = iteration_caps or IterationCaps()
    abort = abort_controller or AbortController()
    rollback = file_rollback or FileRollback()
    for tool_call in tool_calls:
        # session-level abort: stop dispatching immediately.
        if abort.triggered:
            audit.log_session_abort(
                f"tool dispatch halted: {abort.reason}"
            )
            messages.append({
                "role": "user",
                "content": (
                    "Session abort requested. Stop calling tools and "
                    "give the user a concise summary of what was done."
                ),
            })
            return

        # §4.1 session-level tool-call cap.
        cap_reason = caps.bump_tool_call()
        if cap_reason is not None:
            audit.log("iteration_cap_hit", scope="session",
                      tool_calls=caps.tool_calls)
            # Inject a single stop message and abort this batch.
            messages.append({
                "role": "user",
                "content": cap_reason,
            })
            return

        name = tool_call.function.name

        # --approve-plan: if the user asked for plan approval and
        # the model is now trying to run an action tool for the first
        # time, pause and show the scratchpad + todo list.  Planning
        # tools (scratchpad/todo/ask_question) run freely so the model
        # can build its plan before the gate fires.
        if plan_state and not plan_state.get("approved"):
            if name in ACTION_TOOLS and not plan_state.get("prompted"):
                plan_state["prompted"] = True
                scratch = scratchpad_state.read()
                print("\n  [plan approval required]")
                print(f"  The agent wants to run '{name}'.")
                if scratch and scratch != "(empty)":
                    print(f"  --- scratchpad ---\n{scratch[:1000]}")
                print("  --- end plan ---")
                try:
                    ans = input("  Approve this plan? [y/n]: ").strip().lower()
                except EOFError:
                    ans = "n"
                if ans in ("y", "yes"):
                    plan_state["approved"] = True
                    audit.log("plan_approved", tool=name)
                else:
                    audit.log("plan_rejected", tool=name)
                    result = (
                        "The user rejected the plan. Do not proceed with "
                        "action tools. Revise the plan in the scratchpad "
                        "and ask the user for confirmation again."
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": prompt_safety.wrap_tool_result(name, result),
                    })
                    continue

        # Schema-validate tool inputs: parse and validate before
        # any policy/permission check.  A malformed call is reported
        # back to the LLM with the specific errors so it can correct
        # and retry — it never reaches the sandbox or the permission
        # gate.
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as e:
            print(f"  [tool] {name}(<unparseable args>)")
            audit.log(
                "validation_error",
                tool=name,
                raw_arguments=tool_call.function.arguments,
                errors=[f"invalid JSON: {e}"],
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": prompt_safety.wrap_tool_result(
                    name,
                    f"Error: tool arguments were not valid JSON: {e}. "
                    "Resend the call with correctly formatted JSON arguments.",
                ),
            })
            continue

        print(f"  [tool] {name}({args})")

        permission_allowed = False
        permission_reason: str | None = None
        container_error = False
        container_reason = None
        intent_drift = False
        intent_reason: str | None = None
        validation_errors: list[str] | None = None
        result: str

        # schema + bounds validation.
        v_ok, v_errs = TOOL_VALIDATOR.validate(name, args)
        if not v_ok:
            validation_errors = v_errs
            result = (
                "Error: tool arguments failed schema validation:\n- "
                + "\n- ".join(v_errs)
                + "\nCheck the tool schema and retry with valid arguments."
            )
            print(f"  [validation] {v_errs}")
            audit.log(
                "validation_error",
                tool=name,
                args=args,
                errors=v_errs,
            )
            audit.log_tool_result(
                tool_call_id=tool_call.id,
                name=name,
                args=args,
                permission_allowed=False,
                permission_reason="schema validation failed",
                container_error=False,
                container_reason=None,
                intent_drift=False,
                intent_reason=None,
                result=result,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": prompt_safety.wrap_tool_result(name, result),
            })
            continue

        # Re-validate intent after tool use: flag high-risk calls
        # whose arguments reference resources not present in the user's
        # original goal or the scratchpad.  This catches the model
        # acting on instructions injected via prior tool output.
        ok, reason = prompt_safety.intent_check(
            user_goal=user_goal,
            scratchpad=scratchpad_state.read(),
            tool_name=name,
            tool_args=args,
        )
        if not ok:
            intent_drift = True
            intent_reason = reason
            print(f"  [intent drift] {reason}")
            audit.log(
                "intent_drift_suspected",
                tool=name,
                args=args,
                reason=reason,
            )

        if name not in tool_registry:
            result = (
                f"Error: unknown tool '{name}'. "
                f"Available tools: {list(tool_registry.keys())}"
            )
        else:
            # When intent drift is detected, force an explicit user
            # confirmation even in auto-approve modes (but honor
            # dangerouslySkipPermissions, where the user has accepted
            # all risk).  The reason is shown in the prompt.
            if intent_drift and mode != PermissionMode.DANGEROUSLY_SKIP_PERMISSIONS:
                permission_allowed = _ask_permission(
                    f"{name}  [INTENT DRIFT: {reason}]",
                    args,
                )
                permission_reason = None
            else:
                permission_allowed, permission_reason = check_permission(
                    name, args, mode, working_dir,
                )

            # §6.1 log the permission decision as a standalone event.
            audit.log_permission_decision(
                tool=name,
                args=args,
                mode=mode.value,
                allowed=permission_allowed,
                reason=permission_reason,
            )

            if not permission_allowed:
                result = (
                    f"Permission denied: {
                        permission_reason or 'the user did not allow ' + name + ' to run.'} "
                    "Do not retry this tool call without asking the user first."
                )
            else:
                # File rollback: snapshot the original file before
                # any write/edit so the session can offer to revert on
                # abort.  Only files inside the working dir are
                # snapshotted (writes outside are already blocked by §2.3).
                if name in ("write_file", "edit_file"):
                    target_path = args.get("path", "")
                    if target_path:
                        rollback.snapshot(target_path)

                try:
                    result = tool_registry[name](**args)
                except DockerSandboxError as e:
                    container_error = True
                    container_reason = str(e)
                    result = (
                        f"Container error: {e}. The sandbox container could "
                        "not execute this tool. Do not retry without changing "
                        "the approach or asking the user."
                    )
                    # §4.3 distinguish timeouts from other container errors
                    # for the audit log.
                    if "timed out" in str(e).lower():
                        audit.log("tool_timeout", tool=name,
                                  timeout_s=tool_timeout)
                except TypeError as e:
                    result = (
                        f"Error: invalid arguments for tool '{name}': {e}. "
                        "Check the tool schema and retry with the correct arguments."
                    )

        print(f"  [tool result] {result[:200]}{
              '...' if len(result) > 200 else ''}")

        # Treat external data as data: mark web pages and files read
        # from outside the working directory as <external_document> so
        # the model does not treat their bytes as instructions.
        result = prompt_safety.mark_external_content(
            tool_name=name,
            tool_args=args,
            result=result,
            working_dir=working_dir,
        )

        audit.log_tool_result(
            tool_call_id=tool_call.id,
            name=name,
            args=args,
            permission_allowed=permission_allowed,
            permission_reason=permission_reason,
            container_error=container_error,
            container_reason=container_reason,
            intent_drift=intent_drift,
            intent_reason=intent_reason,
            result=result,
        )

        # Delimit context clearly: wrap the tool result so the model
        # can distinguish data coming back from a tool from the user's
        # instructions.  This is the core prompt-injection defense.
        #
        # cap the result length before insertion to bound context
        # growth (the audit log above already has the full content).
        capped = cap_tool_result(result)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": prompt_safety.wrap_tool_result(name, capped),
        })


def agent_loop(
    client,
    mode: PermissionMode,
    working_dir: Path,
    tool_registry: dict,
    audit: AuditLog,
    tool_schemas: list[dict] | None = None,
    iteration_caps: IterationCaps | None = None,
    context_budget: ContextBudget | None = None,
    cost_tracker: CostTracker | None = None,
    llm_timeout: float | None = 120.0,
    tool_timeout: float = 120.0,
    abort_controller: AbortController | None = None,
    file_rollback: FileRollback | None = None,
    container_name: str | None = None,
    approve_plan: bool = False,
):
    # resource controls: the harness — never the model — owns these.
    iteration_caps = iteration_caps or IterationCaps()
    context_budget = context_budget or ContextBudget()
    cost_tracker = cost_tracker or CostTracker()
    # session abort & rollback
    abort_controller = abort_controller or AbortController()
    file_rollback = file_rollback or FileRollback()
    sandbox_exec_timeout = tool_timeout

    # --approve-plan: when True, the first time the model tries to
    # run an action tool after writing to the scratchpad, we pause and
    # ask the user to approve the plan before any action runs.
    plan_approved = not approve_plan  # if flag off, always approved
    plan_approval_pending = False
    # Mutable state shared across handle_tool_calls invocations within
    # a single user turn.
    plan_state: dict | None = {"approved": plan_approved,
                               "prompted": False} if approve_plan else None

    # Statically audit the system prompt for secret interpolation
    # before it ever reaches the model.  This is a defense-in-depth
    # check: the prompt below is a static string with no env-var
    # interpolation, but if someone later edits it to inject a key,
    # this raises immediately at startup.
    _system_prompt_body = (
        "You are a capable coding and research assistant.\n\n"
        + prompt_safety.TRUST_BOUNDARIES
        + "\n\n"

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

        "## Execution environment (Docker sandbox)\n\n"
        "Action tools — read_file, write_file, edit_file, glob_files, grep, run_bash, "
        "webfetch — execute inside a Docker container. The user's project directory is "
        "bind-mounted into that container at the same path as on the host, and the "
        "container's working directory is the project root. You can only see and modify "
        "files inside the project mount; the rest of the container's filesystem is a "
        "minimal, read-only-by-convention Linux image. If the session was started with "
        "network disabled, run_bash and webfetch that need network will fail — treat that "
        "as expected, not as a bug to work around. If a tool returns 'Container error: "
        "...', do not retry the same call — adjust the approach or ask the user to run it "
        "manually. Planning tools run on the host, not in the container, so their state "
        "persists across calls.\n\n"

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
    )
    _prompt_warnings = secret_management.audit_system_prompt(
        _system_prompt_body)
    if _prompt_warnings:
        for w in _prompt_warnings:
            print(f"  [secret-safety] {w}")
        raise RuntimeError(
            "System prompt failed secret-safety audit. Refusing to start. "
            "See secret_management.audit_system_prompt for details."
        )

    messages = [
        {
            "role": "system",
            "content": _system_prompt_body,
        }
    ]

    while True:
        user_input = input("You: ")
        if user_input.lower() == "\\exit":
            break

        audit.log_user_message(user_input)
        # Delimit context clearly: wrap user input so the model can
        # tell it apart from tool output.  Keep the raw goal for the
        # intent check after tool calls.
        current_user_goal = user_input
        messages.append({
            "role": "user",
            "content": prompt_safety.wrap_user_input(user_input),
        })

        # reset the per-turn counter for each new user message.
        iteration_caps.reset_turn()

        # Keep looping until the LLM stops calling tools and gives a final reply
        while True:
            # session-level abort: check before every LLM turn.
            if abort_controller.triggered:
                audit.log_session_abort(
                    f"loop halted between turns: {abort_controller.reason}"
                )
                print(f"\n  [abort] {abort_controller.reason}")
                print("Assistant: Session aborted. Summarizing what was done.")
                break

            # hard iteration cap: stop the loop if the model has
            # taken too many turns for a single user message.
            cap_reason = iteration_caps.bump_turn()
            if cap_reason is not None:
                audit.log("iteration_cap_hit", scope="per_turn",
                          turns=iteration_caps.turns,
                          tool_calls=iteration_caps.tool_calls)
                messages.append({
                    "role": "user",
                    "content": cap_reason,
                })

            # cost circuit breaker: abort the session if spend
            # exceeds the configured cap.
            cost_reason = cost_tracker.check()
            if cost_reason is not None:
                audit.log("cost_limit_hit", **{
                    "cost_usd": cost_tracker.total_cost_usd,
                    "cap_usd": cost_tracker.max_cost_usd,
                    "tokens_in": cost_tracker.total_tokens_in,
                    "tokens_out": cost_tracker.total_tokens_out,
                    "calls": cost_tracker.calls,
                })
                print(f"\n  [cost] {cost_reason}")
                print(f"Assistant: I've hit the session cost limit "
                      f"(${cost_tracker.total_cost_usd:.4f}). "
                      "Stopping here; increase --max-cost-usd if you need more.")
                break

            # token budget: trim older messages before the next
            # request so context growth stays bounded.
            trimmed, trim_reason = context_budget.check_and_trim(messages)
            if trimmed:
                audit.log("context_trimmed", message_count=len(messages),
                          estimate_tokens=context_budget.last_estimate,
                          reason=trim_reason)

            # log the LLM request before sending.
            active_schemas = tool_schemas if tool_schemas is not None else TOOL_SCHEMAS
            audit.log_llm_request(
                model="gemma4",
                message_count=len(messages),
                token_estimate=context_budget.estimate_total(messages),
                has_tools=bool(active_schemas),
            )

            try:
                response = client.chat.completions.create(
                    model="gemma4",
                    messages=messages,
                    tools=active_schemas,
                    temperature=0.7,
                    timeout=llm_timeout,   # §4.3 LLM call timeout
                )
            except Exception as e:
                #  surface an LLM-call failure (including timeout)
                # to the user and the audit log rather than crashing.
                audit.log("llm_call_error", error=str(e))
                print(f"\n  [llm error] {e}")
                print("Assistant: I could not reach the model. "
                      "Please check the endpoint and retry.")
                break

            # record usage for cost tracking.  Local backends
            # (Ollama) may not populate usage; that's fine — the
            # tracker simply records zero tokens for the call.
            cost_tracker.record_usage(getattr(response, "usage", None))

            message = response.choices[0].message

            # log the LLM response metadata + content hash for
            # forensic replay (without storing every token in the log).
            _content_str = message.content or ""
            _content_hash = hashlib.sha256(
                _content_str.encode("utf-8", errors="replace")
            ).hexdigest() if _content_str else None
            _finish = None
            try:
                _finish = response.choices[0].finish_reason
            except (IndexError, AttributeError):
                pass
            _usage_dict = None
            _raw_usage = getattr(response, "usage", None)
            if _raw_usage:
                _usage_dict = {
                    "prompt_tokens": getattr(_raw_usage, "prompt_tokens", None),
                    "completion_tokens": getattr(_raw_usage, "completion_tokens", None),
                }
            audit.log_llm_response(
                model="gemma4",
                finish_reason=_finish,
                usage=_usage_dict,
                message_hash=_content_hash,
                tool_call_count=len(
                    message.tool_calls) if message.tool_calls else 0,
            )

            # Always append the assistant turn so the conversation stays intact
            messages.append(message)
            audit.log_assistant_message(message.content, message.tool_calls)

            if message.tool_calls:
                # The LLM wants to use one or more tools — run them, then loop
                handle_tool_calls(
                    message.tool_calls,
                    messages,
                    mode,
                    working_dir,
                    tool_registry,
                    audit,
                    user_goal=current_user_goal,
                    iteration_caps=iteration_caps,
                    tool_timeout=sandbox_exec_timeout,
                    abort_controller=abort_controller,
                    file_rollback=file_rollback,
                    container_name=container_name,
                    plan_state=plan_state,
                )
            elif not message.content or not message.content.strip():
                # The model ended its turn with an empty message — most commonly
                # happens after a planning-only tool call (scratchpad / todo).
                # Nudge it to continue rather than silently stalling.
                messages.append({
                    "role": "user",
                    "content": "Continue.",
                })
            else:
                # No tool calls: we have the final answer.
                # Sanitize model output before rendering: the CLI
                # surface is plain text, so no HTML/JS escaping is
                # needed here.  If a web UI is added in the future, the
                # assistant content MUST be passed through html.escape
                # (or a template engine's auto-escaping) before being
                # inserted into the DOM — never trust model output.
                print(f"Assistant: {message.content}")
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Coding agent with Docker-sandboxed tool execution and audit logging."
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
    parser.add_argument(
        "--sandbox-dir",
        default=None,
        help=(
            "Directory bind-mounted into the Docker sandbox container as the "
            "project root. Defaults to the current working directory. Action "
            "tools can only read and write files inside this mount."
        ),
    )
    parser.add_argument(
        "--network",
        choices=["bridge", "none"],
        default="bridge",
        help=(
            "Network mode for the sandbox container. 'bridge' (default) gives "
            "the container normal network access (needed for webfetch and for "
            "run_bash commands that download things). 'none' fully isolates the "
            "container from the network."
        ),
    )
    parser.add_argument(
        "--log-dir",
        default="./logs",
        help=(
            "Directory where the JSONL audit log for this session is written. "
            "Defaults to './logs'. One file per session."
        ),
    )
    parser.add_argument(
        "--tools",
        default=None,
        help=(
            "Comma-separated allowlist of tool names to expose to the agent "
            "(principle of least privilege, checklist §2.1).  "
            "Example: --tools read_file,glob_files,grep,run_bash.  "
            "Default: all tools are exposed."
        ),
    )
    parser.add_argument(
        "--tool-timeout",
        type=float,
        default=120.0,
        help=(
            "§4.3 Per-tool-call timeout in seconds.  A tool that does not "
            "finish in this time is killed and reported to the LLM as a "
            "timeout.  Default: 120."
        ),
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=120.0,
        help=(
            "§4.3 Timeout for each LLM completion call in seconds. "
            "Default: 120."
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help=(
            "§4.1 Max LLM turns per user message before the loop stops "
            f"and asks for a summary.  Default: {
                resource_limits.DEFAULT_MAX_TURNS_PER_USER_MSG}."
        ),
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=None,
        help=(
            "§4.1 Max tool calls for the whole session.  Default: "
            f"{resource_limits.DEFAULT_MAX_TOOL_CALLS_PER_SESSION}."
        ),
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=None,
        help=(
            "§4.2 Token budget for the message history.  When exceeded, "
            "older messages are trimmed and replaced with a summary. "
            f"Default: {resource_limits.DEFAULT_MAX_CONTEXT_TOKENS}."
        ),
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help=(
            "§4.4 Cumulative API spend cap in USD.  When exceeded, the "
            "session aborts gracefully.  Default: "
            f"{resource_limits.DEFAULT_MAX_COST_USD:.2f}  (no effect for "
            "local backends that don't report usage)."
        ),
    )
    parser.add_argument(
        "--approve-plan",
        action="store_true",
        default=False,
        help=(
            "§6.2 Require human approval of the agent's plan before any "
            "action tool runs.  The agent builds its plan in the "
            "scratchpad + todo list; the first time it tries to run an "
            "action tool, the user is shown the plan and asked to approve."
        ),
    )
    cli_args = parser.parse_args()

    # Principle of least privilege: parse the optional tool allowlist.
    allowed_tools: set[str] | None = None
    if cli_args.tools:
        requested = {t.strip() for t in cli_args.tools.split(",") if t.strip()}
        unknown = requested - set(RAW_TOOL_REGISTRY.keys())
        if unknown:
            print(
                f"Error: unknown tool(s) in --tools: {sorted(unknown)}. "
                f"Available: {sorted(RAW_TOOL_REGISTRY.keys())}"
            )
            raise SystemExit(2)
        allowed_tools = requested

    mode = PermissionMode(cli_args.mode)
    working_dir = Path.cwd()
    sandbox_root = Path(cli_args.sandbox_dir).resolve(
    ) if cli_args.sandbox_dir else working_dir

    tools_dir = Path(__file__).resolve().parent / "tools"

    # Secret & credential management
    # Warn about secret-looking env vars on the host (informational).
    secret_env = secret_management.scan_environment_for_secrets()
    if secret_env:
        print(f"  [secret-safety] {len(secret_env)
                                   } host env var(s) look like secrets:")
        for name, src in secret_env:
            print(f"    - {name} (from {src})")
        print("  These will NOT be passed to the sandbox container "
              "(only an allowlist is inherited).")

    # 5.2 Verify no credential mount paths would leak into the container.
    cred_mounts = secret_management.check_credential_mounts()
    if cred_mounts:
        print(
            f"  [secret-safety] Host credential paths present (NOT mounted into container):")
        for p in cred_mounts:
            print(f"    - {p}")

    # Generate per-session credentials.  The token is injected into
    # the container env; it never appears in the system prompt or tool
    # schemas.
    session_creds = secret_management.SessionCredentials()
    container_env = session_creds.container_env()

    try:
        sandbox = DockerSandbox(
            project_root=sandbox_root,
            tools_dir=tools_dir,
            network=cli_args.network,
            exec_timeout=cli_args.tool_timeout,   # §4.3
            container_env=container_env,          # §5.2 / §5.3
        )
    except DockerSandboxError as e:
        print(f"Could not start sandbox: {e}")
        raise SystemExit(1)

    tool_registry = build_tool_registry(sandbox, allowed_tools=allowed_tools)
    active_schemas = filter_tool_schemas(TOOL_SCHEMAS, allowed_tools)

    # resource controls
    iteration_caps = IterationCaps(
        max_turns_per_user_msg=cli_args.max_turns or resource_limits.DEFAULT_MAX_TURNS_PER_USER_MSG,
        max_tool_calls_per_session=cli_args.max_tool_calls or resource_limits.DEFAULT_MAX_TOOL_CALLS_PER_SESSION,
    )
    context_budget = ContextBudget(
        max_tokens=cli_args.max_context_tokens or resource_limits.DEFAULT_MAX_CONTEXT_TOKENS,
    )
    cost_tracker = CostTracker(
        max_cost_usd=cli_args.max_cost_usd if cli_args.max_cost_usd is not None else resource_limits.DEFAULT_MAX_COST_USD,
    )

    audit = AuditLog(Path(cli_args.log_dir))
    audit.log_config(
        mode=mode.value,
        working_dir=str(working_dir),
        sandbox_root=str(sandbox_root),
        container=sandbox.container,
        network=cli_args.network,
        tools_allowed=sorted(allowed_tools) if allowed_tools else "ALL",
        tools_host_side=[n for n in tool_registry if n not in ACTION_TOOLS],
        tools_container_side=sorted(
            n for n in tool_registry if n in ACTION_TOOLS),
        tool_timeout_s=cli_args.tool_timeout,
        llm_timeout_s=cli_args.llm_timeout,
        max_turns_per_user_msg=iteration_caps.max_turns_per_user_msg,
        max_tool_calls_per_session=iteration_caps.max_tool_calls_per_session,
        max_context_tokens=context_budget.max_tokens,
        max_cost_usd=cost_tracker.max_cost_usd,
        session_id=session_creds.session_id,                   # §5.3
        secret_env_count=len(secret_env),                       # §5.1
        credential_mounts_found=[str(p) for p in cred_mounts],  # §5.2
        container_env_allowlist=sorted(container_env.keys()),  # §5.2
    )

    print(f"Agent started in '{
          mode.value}' mode  (working dir: {working_dir})")
    print(f"Sandbox container: {sandbox.container}  (mount: {
          sandbox_root}, network: {cli_args.network})")
    print(f"Session:          {session_creds.session_id}")
    print(f"Resource limits: turns={iteration_caps.max_turns_per_user_msg} "
          f"tool_calls={iteration_caps.max_tool_calls_per_session} "
          f"ctx_tokens={context_budget.max_tokens} "
          f"cost=${cost_tracker.max_cost_usd:.2f} "
          f"tool_timeout={cli_args.tool_timeout}s llm_timeout={cli_args.llm_timeout}s")
    if cli_args.approve_plan:
        print(f"Plan approval:    ENABLED (action tools wait for human approval)")
    print(f"Audit log:         {audit.path}")
    print("Type \\exit to quit.  (Ctrl-C to abort a session)\n")

    # session abort controller — installs SIGINT/SIGTERM handlers.
    abort_controller = AbortController()
    abort_controller.install_signal_handlers()
    # file rollback — snapshots before write/edit for revert on abort.
    file_rollback = FileRollback()

    client = get_llm_client()
    try:
        agent_loop(
            client, mode, working_dir, tool_registry, audit,
            tool_schemas=active_schemas,
            iteration_caps=iteration_caps,
            context_budget=context_budget,
            cost_tracker=cost_tracker,
            llm_timeout=cli_args.llm_timeout,
            tool_timeout=cli_args.tool_timeout,
            abort_controller=abort_controller,
            file_rollback=file_rollback,
            container_name=sandbox.container,
            approve_plan=cli_args.approve_plan,
        )
    finally:
        abort_controller.remove_signal_handlers()
        # offer to revert file changes on exit / abort.
        if file_rollback.snapshot_count > 0:
            file_rollback.offer_rollback()
        file_rollback.cleanup()
        session_creds.revoke()   # §5.3 rotate on exit
        sandbox.close()
        audit.close()
        print(f"\nSandbox container removed. Audit log written to {
              audit.path}")
        print(f"Session cost: {cost_tracker.summary()}")
