# agent-harness

Companion code for the [ruxu.dev](https://www.ruxu.dev) blog post series on building a simple AI agent harness from scratch.

## Blog Post Series

1. [Build a Basic AI Agent](https://www.ruxu.dev/articles/ai/build-a-basic-ai-agent/): A minimal conversational agent with a message loop, powered by a local model via Ollama.
2. [Build an AI Agent with Tools](https://www.ruxu.dev/articles/ai/build-an-ai-agent-with-tools/): Extends the agent with a tool registry so the LLM can read/write files, search the filesystem, run shell commands, and fetch web pages.
3. `agent-memory/`: Adds durable agent and user memory plus searchable, resumable sessions to the sandboxed agent.

## Structure

```
simple-agent/          # Part 1: bare-bones agent loop
  agent.py

agent-with-tools/      # Part 2: agent with tool-calling support
  agent.py
  tools/
    filesystem.py      # read, write, search files
    shell.py           # run shell commands
    web.py             # fetch web pages
    registry.py        # tool registry & schemas
agent-memory/          # Persistent memory and session continuation
  agent.py
  memory.py             # File-backed notes and conversation sessions
```

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Setup

```bash
uv sync
```

You can also change the current Ollama agent running gemma4 for any model of your choice.

### simple-agent


```bash
uv run simple-agent/agent.py
```

### agent-with-tools

```bash
uv run agent-with-tools/agent.py
```

Type `\exit` to quit either agent.

### agent-memory

```bash
uv run agent-memory/agent.py
```

Memory is stored outside the repository by default in `~/.agent-harness/memory`:

- The agent reads and appends durable `personal` notes (lessons and environment facts) and `user` notes (preferences) through host-side memory tools.
- Each conversation is saved as a transcript and should receive an LLM-written summary; a fallback summary is generated on exit if needed.
- Type `\sessions` during a conversation, or run `uv run agent-memory/agent.py --list-sessions`, to list saved sessions.
- Resume a conversation with `uv run agent-memory/agent.py --resume SESSION_ID`.
- Change the storage location with `--memory-dir PATH`.
