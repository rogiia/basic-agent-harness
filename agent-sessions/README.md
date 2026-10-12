# Session-aware agent harness

This version copies `agent-memory/` and adds session lifecycle management.
It reads existing memory sessions from `~/.agent-harness/memory`; use
`--memory-dir PATH` for an isolated store. Docker and Ollama (`gemma4`) are
required to run the agent, but not for offline session management or tests.

## Features considered

- Listing, searching and resuming existing conversations (inherited).
- Manual and automatic LLM compaction with a separate, lossless transcript.
- Titles, session information, independent branches with parent IDs.
- Archive/unarchive, explicitly confirmed deletion, full JSON export.
- Atomic checkpoints, interrupted tool-call recovery and single-writer locks.
- Restore scratchpad, todos, retry counts, tool counts and cumulative API usage.
- Project-root checks on resume; fresh sandbox, credentials and permission approvals.

Future extensions include named checkpoints/rewind, import with schema validation,
tags and retention policies, cross-machine synchronization, and a session browser.
Rewind would affect conversation state only: it cannot undo arbitrary shell effects.

## Run

```bash
uv run agent-sessions/agent.py
uv run agent-sessions/agent.py --list-sessions
uv run agent-sessions/agent.py --resume SESSION_ID
uv run agent-sessions/agent.py --rename SESSION_ID "Cache investigation"
uv run agent-sessions/agent.py --fork SESSION_ID
uv run agent-sessions/agent.py --archive SESSION_ID
uv run agent-sessions/agent.py --list-sessions --include-archived
uv run agent-sessions/agent.py --unarchive SESSION_ID
uv run agent-sessions/agent.py --export SESSION_ID > session.json
uv run agent-sessions/agent.py --delete SESSION_ID --yes
```

Interactive commands: `\help`, `\sessions`, `\info`, `\title NAME`, `\fork`,
`\compact`, and `\exit`. Fork prints a new ID to resume separately.
Management commands run without Docker or model access.

## Compaction and recovery

Automatic compaction starts at 80% of `--max-context-tokens`. It summarizes
older turns with the model, retains the current complete user/tool turn and
the system prompt, and stores summaries as conversation data, not system rules.
`messages` is resumable model context; `transcript` preserves all original
messages, and `compactions` records summary events. Search/export/read-session
can still access the original work. Summaries can omit facts; recheck original
files and transcripts when precision matters.

Model failures or non-shrinking summaries leave context unchanged. If the
remaining context is still too large, the harness stops rather than silently
discarding history. Token counts are estimates and exclude tool schemas;
leave headroom for schemas and model output. Summary calls count toward spend.

Interrupted tool calls receive an explicit “outcome unknown” result on resume;
tools are never automatically replayed. Check filesystem state before retrying.
Planning and usage state are checkpointed at tool-batch/turn boundaries; a
crash during a call can leave its effects or billing unrecorded. JSON session
writes use atomic replacement and file fsync. Linux advisory locks prevent
concurrent writers in **this version** and release automatically on process exit.
Do not simultaneously use the older `agent-memory/` writer on the same store.
An archived session must be unarchived before resuming. Session files and
exports contain conversation/tool data: treat them as sensitive.

## Tests

```bash
uv run python -m unittest discover -s agent-sessions -p 'test_*.py'
```
