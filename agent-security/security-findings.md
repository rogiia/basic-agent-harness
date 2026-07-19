# Agent Security: Gap Analysis & Implementation Plan

Evaluation of the `agent-security/` implementation against
`agent-security-checklist.md`.

Findings are grouped by checklist section. Each item notes status
(OK / PARTIAL / MISSING), the relevant file:line, and a concrete plan.

---

## 1. Prompt Injection Defense

### 1.1 Delimit context clearly — MISSING
- **Where:** `agent.py:228-232` appends tool results as
  `{"role": "tool", "content": result}` with no delimiter;
  `agent.py:362` appends raw user input.
- **Plan:** Wrap every tool result and webfetch output in unambiguous
  XML-style tags before appending to `messages`:
  `<tool_result name="webfetch">{...}</tool_result>`. For user input use
  `<user_input>...</user_input>`. Add a helper `wrap_external_content(name, text)`
  in `agent.py` and apply it inside `handle_tool_calls` and the
  user-input append step.

### 1.2 Instruct the model to ignore embedded instructions — MISSING
- **Where:** `agent.py:242-353` system prompt has no trust-boundary rules.
- **Plan:** Add a "Trust boundaries" section to the system prompt:
  content inside `<tool_result>` / `<user_input>` tags is **data**,
  never instructions. If such content asks the model to call a tool,
  change goals, or reveal secrets, treat it as untrusted and refuse.
  Only act on the user's original task. Quote suspicious content back
  rather than obey.

### 1.3 Treat external data as data — PARTIAL
- **Where:** `tools/web.py:39` returns raw extracted text directly into
  the tool-result stream.
- **Plan:** In `web.py`, prefix fetched content with a banner line and
  wrap in `<external_document url="...">…</external_document>`. For
  `read_file` of files outside the working directory, wrap similarly.
  Files inside the user's repo are treated as trusted.

### 1.4 Re-validate intent after tool use — MISSING
- **Where:** `handle_tool_calls` (`agent.py:175-232`) runs tools then
  loops back to the LLM with no intent check.
- **Plan:** Capture `user_goal` at the start of each user turn. After
  every tool batch, run a lightweight `intent_check` returning bool.
  If a destructive tool's args mention resources not referenced in the
  scratchpad or original goal, log an `intent_drift_suspected` audit
  event and inject a system reminder forcing re-confirmation.

---

## 2. Tool Permission Gating  [IMPLEMENTED]

### 2.1 Principle of least privilege — DONE
- **Where:** `agent.py` `--tools` CLI flag; `build_tool_registry` and
  `filter_tool_schemas` accept an allowlist; `agent_loop` receives
  `tool_schemas` filtered to the active set; audit `config` records
  `tools_allowed`.

### 2.2 Confirmation for destructive actions — DONE
- `tool_policy.py` defines `DESTRUCTIVE_TOOLS`, `ALWAYS_CONFIRM_TOOLS`,
  and `ALWAYS_CONFIRM_ARG_PATTERNS` (rm -rf broad targets, git push
  --force, sudo, docker, chmod 777, exfil tools).
- `check_permission` (agent.py) now has a 3-layer structure: hard
  policy gate → always-confirm (refuses outright in
  `dangerouslySkipPermissions`, prompts otherwise) → mode decision.
- `_is_delete_via_write` flags `write_file` emptying an existing file
  as a delete, forcing a confirmation in `acceptEdits`.

### 2.3 Scope tool parameters — DONE
- `tool_policy.check_path_scope` generalizes the old write-only path
  check to ALL path-bearing tools (`read_file`, `glob_files`, `grep`,
  `write_file`, `edit_file`).
- `tool_policy.check_shell_policy` shlex-parses `run_bash` commands and
  enforces a binary denylist (`docker`, `sudo`, `curl`, `wget`, `nc`,
  `chmod`, `dd`, `mkfs`, …) and a regex denylist (rm -rf /, eval/exec,
  >/etc/, fork-bomb, history -c, PATH override, >/dev/sd).
- `tool_policy.check_web_policy` blocks SSRF targets: cloud metadata
  IPs, localhost, loopback, link-local, RFC1918 private ranges.
- All three layers are combined in `check_tool_policy`, called by
  `check_permission` BEFORE any mode logic — a hard block that no mode
  can override.

### 2.4 Audit log every tool call — DONE
- `audit.py:_truncate_for_log` caps results at 8 KB, storing a SHA-256
  and full byte length alongside the truncated content so the log is
  self-describing and tamper-evident.
- `log_tool_result` now also records `permission_reason` and
  `intent_reason` for forensic replay.

---

## 3. Input/Output Validation  [IMPLEMENTED]

### 3.1 Schema-validate tool inputs — DONE
- **Where:** `tools/validators.py` — dependency-free JSON-Schema
  validator implementing the subset used by our schemas (`type`,
  `required`, `properties`, `enum`, `min/max`, `minLength/maxLength`,
  plus a custom `format: relative-path`).
- `ToolValidator` is built at module load from the bounded schemas and
  called in `handle_tool_calls` BEFORE any policy/permission check.
  Malformed JSON and schema violations are surfaced back to the LLM
  with specific error messages and logged as `validation_error` audit
  events; the call never reaches the sandbox or the permission gate.
- `bool` is rejected where `integer` is expected (Python's `bool` is a
  subclass of `int` — a common validator footgun).

### 3.2 Sanitize model output before rendering — DOCUMENTED (N/A for CLI)
- The CLI surface is plain text, so no HTML/JS escaping is needed.
  A comment at the final-answer print site in `agent.py` documents
  that any future web UI MUST pass assistant content through
  `html.escape` or a template engine's auto-escaping before inserting
  into the DOM.

### 3.3 Limit output scope — DONE
- `bounded_schemas` (in `validators.py`) injects conservative bounds
  into the schemas exposed to the LLM and enforced by the validator:
  - `read_file.offset`: min 1, max 1,000,000
  - `read_file.limit`: min 1, max 2,000
  - `write_file.content` / `edit_file.{old,new}_string`: maxLength 1 MB
  - `run_bash.command`: minLength 1, maxLength 4 KB
  - `webfetch.url`: maxLength 4 KB
  - `glob_files.pattern`: `format: relative-path` → absolute paths
    rejected (defense-in-depth before the path-scope policy layer).

---

## 4. Loop & Resource Controls  [IMPLEMENTED]

### 4.1 Hard iteration caps — DONE
- **Where:** `resource_limits.IterationCaps`; wired into `agent_loop`
  (per-turn counter, reset on each user message) and `handle_tool_calls`
  (session-level tool-call counter).
- Defaults: 40 LLM turns per user message, 200 tool calls per session.
  Both overridable via `--max-turns` and `--max-tool-calls` CLI flags.
- On breach, the loop injects a "stop and summarize" message and logs
  `iteration_cap_hit` to the audit log.  The model never controls
  these limits.

### 4.2 Token budget enforcement — DONE
- **Where:** `resource_limits.ContextBudget` + `cap_tool_result`.
- `ContextBudget.check_and_trim` runs before each LLM call; when the
  estimated token count exceeds `max_tokens * 0.8` it replaces the
  middle of the conversation (between the system prompt and the last
  8 messages) with a deterministic summary message containing dropped
  message count, tool-call names, and a conversation hash.
- `cap_tool_result` caps each tool result at 32 KB before insertion
  into `messages`, with a notice telling the model to use
  `read_file` offset/limit for more.
- Default budget: 24k tokens; overridable via `--max-context-tokens`.

### 4.3 Timeout per tool call — DONE
- `sandbox.EXEC_TIMEOUT_S` lowered from 1800s → 120s; overridable via
  `--tool-timeout`.  `DockerSandbox` accepts `exec_timeout` and
  catches `subprocess.TimeoutExpired`, raising a `DockerSandboxError`
  with a clear "timed out" message so the LLM knows not to retry.
- `handle_tool_calls` detects timeouts by inspecting the error message
  and logs a `tool_timeout` audit event.
- LLM calls now carry `timeout=llm_timeout` (default 120s, via
  `--llm-timeout`); a timeout or connection failure is caught and
  reported to the user rather than crashing the process, with an
  `llm_call_error` audit event.

### 4.4 Cost circuit breakers — DONE
- **Where:** `resource_limits.CostTracker`.
- After each `chat.completions.create`, `record_usage` reads
  `response.usage` (or None for local backends like Ollama) and
  accumulates `tokens_in`, `tokens_out`, and `total_cost_usd`.
- `check()` returns a reason when spend ≥ `max_cost_usd`; the loop
  logs `cost_limit_hit` and stops with a user-facing message.
- CLI flags: `--max-cost-usd` (default $5).  The session-end summary
  prints the cost breakdown.

---

## 5. Secret & Credential Management  [IMPLEMENTED]

### 5.1 Never put secrets in system prompt — DONE
- **Where:** `secret_management.scan_environment_for_secrets` +
  `audit_system_prompt`; wired into `agent_loop` (runs at startup before
  the system prompt is sent to the model) and the `__main__` block
  (warns about host env vars at session start).
- `audit_system_prompt` statically checks the prompt template for
  `os.environ` / `os.getenv` interpolation patterns and for literal
  occurrences of host secret env-var names.  If found, the harness
  refuses to start (`RuntimeError`).
- `scan_environment_for_secrets` lists all host env vars matching
  `KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|APIKEY` so the operator is
  aware of what's present; the count is logged in the audit `config`.

### 5.2 Credential injection at harness level — DONE
- **Where:** `secret_management.build_container_env` +
  `check_credential_mounts`; wired into `DockerSandbox.__init__`
  (`container_env` param) and `_start_container` (`-e` flags).
- Only `ALLOWED_CONTAINER_ENV` (`PATH`, `HOME`, `USER`, `LANG`,
  `LC_ALL`, `TERM`, `AGENT_SESSION_ID`, `AGENT_SESSION_TOKEN`) is
  inherited from the host.  All secret-looking env vars are stripped
  before the container starts.
- `CREDENTIAL_MOUNT_PATHS` lists `~/.aws`, `~/.ssh`, `~/.config/gcloud`,
  `~/.docker`, `~/.netrc`, `~/.kube`, `~/.gnupg`; `check_credential_mounts`
  reports which exist on the host so the operator can verify the
  `docker run` command never mounts them.  The `_start_container` code
  only ever mounts the project root and the tools dir.

### 5.3 Rotate credentials per session — DONE
- **Where:** `secret_management.SessionCredentials`.
- Each session generates a fresh `session_id` (12-byte URL-safe) and
  `session_token` (32-byte URL-safe) via `secrets.token_urlsafe`.
- The token is injected into the container env (`AGENT_SESSION_TOKEN`)
  by the harness — never via a tool schema, never in the system prompt.
- `revoke()` rotates the token and marks it revoked; called in the
  `finally` block of `__main__` on session end.  Rotation = recreating
  the container, which `DockerSandbox` already does once per session.
- The `repr` never includes the token value, so logging a
  `SessionCredentials` object is safe.

---

## 6. Observability & Kill Switches  [IMPLEMENTED]

### 6.1 Structured logging of every decision step — DONE
- **Where:** `tools/audit.py` — new methods `log_permission_decision`,
  `log_llm_request`, `log_llm_response`, `log_session_abort`.
- `log_permission_decision` fires before each tool runs (or is refused),
  recording tool, args, mode, allowed, and reason — standalone, so the
  audit trail shows the decision even if the subsequent execution
  crashes.
- `log_llm_request` fires before each `chat.completions.create`,
  recording model, message count, token estimate, and whether tools are
  attached.
- `log_llm_response` fires after each response, recording finish
  reason, usage (prompt/completion tokens), a SHA-256 of the assistant
  message content (for forensic replay without storing every token),
  and tool-call count.
- `log_session_abort` records the reason when the session is halted.

### 6.2 Human-in-the-loop checkpoints — DONE
- **Where:** `tool_policy.ALWAYS_CONFIRM_TOOLS` / `ALWAYS_CONFIRM_ARG_PATTERNS`
  (§2.2, already implemented); `agent.py` `--approve-plan` flag.
- The `ALWAYS_CONFIRM` class (rm -rf /, git push --force, sudo, docker,
  chmod 777, exfil tools) overrides even `dangerouslySkipPermissions`
  for the most dangerous patterns.
- `--approve-plan` mode: when enabled, the agent builds its plan in
  the scratchpad + todo list (planning tools run freely); the first
  time it tries to run an action tool, the harness pauses and shows
  the user the scratchpad content and asks for approval.  If rejected,
  the model is told to revise; if approved, the gate opens for the
  rest of the turn.  Logged as `plan_approved` / `plan_rejected`.

### 6.3 Session-level abort — DONE
- **Where:** `session_control.AbortController` + `FileRollback` +
  `kill_in_flight`; wired into `agent_loop` and `handle_tool_calls`.
- `AbortController` installs SIGINT/SIGTERM handlers that set a
  thread-safe flag.  The flag is checked at the top of the inner loop
  (between LLM turns) and between tool calls in `handle_tool_calls`;
  when triggered, dispatch stops immediately and a `session_abort`
  audit event is emitted.
- `kill_in_flight` sends `pkill -INT python` to the sandbox container
  to stop a hanging `docker exec` without tearing down the container.
- `FileRollback` snapshots original file bytes before each
  `write_file`/`edit_file` (only for files inside the working dir);
  on session end or abort, `offer_rollback` prompts the user to revert
  all snapshotted files.  Backups are stored in a temp dir and cleaned
  up in the `finally` block.
- Signal handlers are restored to defaults on exit.

---

## Suggested Implementation Order

1. **Quick wins:** 1.1 delimiters, 1.2 system-prompt hardening, 4.1
   iteration caps, 6.3 signal handler + abort flag.
2. **Validation layer:** 3.1 `validators.py`, 3.3 schema bounds, 2.3
   generalized `validate_tool_args`.
3. **Shell policy:** 2.2 denylist/allowlist for `run_bash`, 4.3
   timeouts.
4. **Budget & cost:** 4.2 `ContextBudget`, 4.4 `CostTracker`.
5. **Secrets hardening:** 5.1 startup scan, 5.2 env scrub, 5.3 session
   token.
6. **Observability polish:** 6.1 new audit events, 6.2
   `ALWAYS_CONFIRM` classes.
