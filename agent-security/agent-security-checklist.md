# Agent Security Checklist

## Prompt Injection Defense

This is the biggest risk unique to LLM agents:

- Delimit context clearly — use unambiguous separators (<user_input>, <tool_result>) so the model knows what came from where
- Instruct the model explicitly — tell it in the system prompt to ignore instructions embedded in tool results or user data
- Treat external data as data, not instructions — never interpolate raw web/document content into the instruction stream without escaping
- Re-validate intent after tool use — before acting on a model response that followed a tool call, re-check it matches the original user goal

## Tool Permission Gating

- Principle of least privilege — expose only the tools a given task actually needs; don't give every agent access to everything
- Require confirmation for destructive actions — deletes, writes, external API calls that mutate state should require explicit human approval
- Scope tool parameters — validate that tool arguments are within allowed ranges/paths/targets before execution (e.g., path traversal on file tools)
- Audit log every tool call — log inputs and outputs for forensic replay

## Input/Output Validation

- Schema-validate tool inputs — use Pydantic, JSON Schema, or similar; reject anything malformed before execution
- Sanitize model output before rendering — strip or escape HTML/JS if outputs are shown in a browser
Limit output scope — if the model is supposed to return a filename, reject anything that looks like a shell command

## Loop & Resource Controls

- Hard iteration caps — the harness enforces a max number of turns/tool calls; the model never controls this
- Token budget enforcement — cap context window growth to prevent runaway loops filling memory
- Timeout per tool call — don't let a hanging external call block the agent indefinitely
- Cost circuit breakers — track cumulative API spend per session and abort if exceeded

## Secret & Credential Management

- Never put secrets in the system prompt — the model can leak them in tool calls or responses
- Use credential injection at the harness level — the harness signs/authenticates tool calls; the model never sees API keys
- Rotate credentials per session — use short-lived tokens scoped to that agent run

## Observability & Kill Switches

- Structured logging of every decision step — model input, reasoning (if CoT is exposed), tool call, tool result
- Human-in-the-loop checkpoints — define which action classes always require approval regardless of model confidence
- Session-level abort — a single signal should halt all in-flight tool calls and roll back reversible state
