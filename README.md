# agent-harness

Companion code for the [ruxu.dev](https://www.ruxu.dev) blog post series on building a simple AI agent harness from scratch.

## Blog Post Series

1. [Build a Basic AI Agent](https://www.ruxu.dev/articles/ai/build-a-basic-ai-agent/) — A minimal conversational agent with a message loop, powered by a local model via Ollama.
2. [Build an AI Agent with Tools](https://www.ruxu.dev/articles/ai/build-an-ai-agent-with-tools/) — Extends the agent with a tool registry so the LLM can read/write files, search the filesystem, run shell commands, and fetch web pages.

## Structure

```
simple-agent/          # Part 1 — bare-bones agent loop
  agent.py

agent-with-tools/      # Part 2 — agent with tool-calling support
  agent.py
  tools/
    filesystem.py      # read, write, search files
    shell.py           # run shell commands
    web.py             # fetch web pages
    registry.py        # tool registry & schemas
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
