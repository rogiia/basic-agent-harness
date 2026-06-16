"""Loop & resource controls (checklist §4).

This module centralises every limit that prevents the agent from
running away with itself:

  §4.1  IterationCaps      — hard caps on turns and tool calls.
  §4.2  ContextBudget      — token budget with automatic context trimming.
  §4.3  (timeouts live in sandbox.py / agent.py — wired here via a small
         ToolTimeout helper that classifies timeout events for the audit
         log).
  §4.4  CostTracker        — cumulative API spend circuit breaker.

All of these are *host-side* controls: the model never gets to vote on
them.  They are evaluated between tool calls and before each LLM call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 4.1 Hard iteration caps
# ---------------------------------------------------------------------------

# Defaults are chosen to be generous enough for real coding tasks but
# short enough that a stuck loop is killed quickly.
DEFAULT_MAX_TURNS_PER_USER_MSG = 40
DEFAULT_MAX_TOOL_CALLS_PER_SESSION = 200


@dataclass
class IterationCaps:
    """Counters that enforce hard iteration caps (§4.1).

    The harness — never the model — owns these limits.  Two counters
    are tracked:

      - ``turns``: incremented once per LLM response within a single
        user turn.  Breach → stop calling tools, ask the model for a
        summary.
      - ``tool_calls``: incremented once per tool dispatch, across the
        whole session.  Breach → same.

    Both are *hard* caps: when hit, ``check_and_bump`` returns the
    reason and the caller must stop the loop.
    """

    max_turns_per_user_msg: int = DEFAULT_MAX_TURNS_PER_USER_MSG
    max_tool_calls_per_session: int = DEFAULT_MAX_TOOL_CALLS_PER_SESSION
    turns: int = 0
    tool_calls: int = 0

    def reset_turn(self) -> None:
        """Reset the per-turn counter at the start of each user message."""
        self.turns = 0

    def bump_turn(self) -> str | None:
        """Increment the turn counter; return a reason if breached."""
        self.turns += 1
        if self.turns > self.max_turns_per_user_msg:
            return (
                f"Reached the per-turn iteration cap "
                f"({self.max_turns_per_user_msg} LLM turns). Stop calling "
                f"tools and give the user a concise summary of progress."
            )
        return None

    def bump_tool_call(self) -> str | None:
        """Increment the tool-call counter; return a reason if breached."""
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls_per_session:
            return (
                f"Reached the session tool-call cap "
                f"({self.max_tool_calls_per_session} tool calls). Stop "
                f"calling tools and give the user a concise summary."
            )
        return None

    @property
    def breached(self) -> bool:
        return self.turns > self.max_turns_per_user_msg or \
               self.tool_calls > self.max_tool_calls_per_session


# ---------------------------------------------------------------------------
# 4.2 Token budget enforcement
# ---------------------------------------------------------------------------

# Above this fraction of the model's context window we trim older
# messages.  Trimming is intentionally conservative: we only fire when
# the next request risks overflowing, and we never touch the system
# prompt or the last few turns.
DEFAULT_MAX_CONTEXT_TOKENS = 24_000          # conservative for a 32k model
DEFAULT_KEEP_RECENT_MESSAGES = 8             # never trim the last N messages
DEFAULT_MAX_TOOL_RESULT_CHARS = 32 * 1024    # 32 KB cap before insertion


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token.

    Good enough for budget decisions; the real BPE count is only
    needed for billing, which §4.4 handles via the API's own
    ``usage`` field.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def _message_token_count(msg: Any) -> int:
    """Estimate tokens in a single chat message."""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_tokens(content)
    # OpenAI tool-call message objects expose .content; some also carry
    # tool_calls as a list of objects.  We only count text here.
    return estimate_tokens(str(content))


@dataclass
class ContextBudget:
    """Track cumulative tokens and trim the message history (§4.2).

    ``check_and_trim`` is called before each LLM request.  If the
    estimated token count exceeds ``max_tokens * trim_threshold`` it
    replaces the middle of the conversation (everything between the
    system prompt and the most recent ``keep_recent`` messages) with a
    single ``system`` summary message.  The system prompt and the
    latest turns are always preserved.
    """

    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    trim_threshold: float = 0.8
    keep_recent: int = DEFAULT_KEEP_RECENT_MESSAGES
    last_estimate: int = 0
    trims: int = 0

    def estimate_total(self, messages: list) -> int:
        return sum(_message_token_count(m) for m in messages)

    def check_and_trim(self, messages: list) -> tuple[bool, str | None]:
        """Trim *messages* in place if over budget.

        Returns ``(trimmed, reason)``.  When ``trimmed`` is True a
        summary message has been spliced in and the caller should log
        a ``context_trimmed`` audit event.
        """
        self.last_estimate = self.estimate_total(messages)
        if self.last_estimate <= int(self.max_tokens * self.trim_threshold):
            return False, None

        # We always keep messages[0] (system prompt) and the last
        # ``keep_recent`` messages.  Everything in between is a
        # candidate for trimming.
        if len(messages) <= self.keep_recent + 1:
            return False, None  # too short to trim meaningfully

        cut_start = 1
        cut_end = len(messages) - self.keep_recent
        dropped = messages[cut_start:cut_end]

        summary = self._summarize(dropped)
        messages[cut_start:cut_end] = [{
            "role": "system",
            "content": summary,
        }]
        self.trims += 1
        self.last_estimate = self.estimate_total(messages)
        return True, (
            f"Context trimmed to ~{self.last_estimate} tokens "
            f"(dropped {len(dropped)} messages, replaced with a summary)."
        )

    @staticmethod
    def _summarize(dropped: list) -> str:
        """Build a compact summary of the dropped messages.

        This is a deterministic, no-LLM summary: it records what tools
        were called and a hash of the conversation so the agent can
        still reference "what was tried" without the full content.
        """
        tool_calls: list[str] = []
        total_chars = 0
        for m in dropped:
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if content:
                total_chars += len(str(content))
            tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    name = getattr(getattr(tc, "function", None), "name", None)
                    if not name and isinstance(tc, dict):
                        name = tc.get("function", {}).get("name")
                    if name:
                        tool_calls.append(name)
        blob = str([
            (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) for m in dropped
        ])
        digest = hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]
        lines = [
            "[context-trim summary]",
            f"Earlier conversation dropped to stay within the token budget.",
            f"Dropped messages: {len(dropped)} ({total_chars} chars).",
            f"Tools called in dropped section: {', '.join(tool_calls) or 'none'}.",
            f"Conversation hash (first 16 hex): {digest}.",
            "Re-read any files you need rather than relying on the dropped context.",
        ]
        return "\n".join(lines)


def cap_tool_result(result: str, limit: int = DEFAULT_MAX_TOOL_RESULT_CHARS) -> str:
    """Cap a tool result before it is inserted into messages (§4.2).

    Long results (e.g. reading a 50k-line file) are truncated to
    ``limit`` chars with a notice appended so the model knows there is
    more it can re-fetch with offset/limit.
    """
    if len(result) <= limit:
        return result
    return (
        result[:limit]
        + f"\n\n[... result truncated to {limit} chars for context budget; "
        + f"use read_file with offset/limit to see more ...]"
    )


# ---------------------------------------------------------------------------
# 4.4 Cost circuit breakers
# ---------------------------------------------------------------------------

# Default per-session spend cap in USD.  Generous for local Ollama
# (where usage is typically 0) but a real guardrail for hosted APIs.
DEFAULT_MAX_COST_USD = 5.0

# Rough per-1k-token prices in USD for common hosted models.  Only used
# when the API response doesn't carry explicit pricing.  Override with
# --price-in / --price-out on the CLI if needed.
DEFAULT_PRICE_PER_1K_IN = 0.000150
DEFAULT_PRICE_PER_1K_OUT = 0.000600


@dataclass
class CostTracker:
    """Accumulate API spend and abort when the cap is hit (§4.4).

    After each ``chat.completions.create`` call, the caller invokes
    ``record_usage`` with the ``response.usage`` object (or None for
    local backends that don't report usage).  ``check`` returns a
    reason string when the session cap is exceeded.
    """

    max_cost_usd: float = DEFAULT_MAX_COST_USD
    price_in: float = DEFAULT_PRICE_PER_1K_IN
    price_out: float = DEFAULT_PRICE_PER_1K_OUT
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    calls: int = 0

    def record_usage(self, usage: Any | None) -> None:
        """Record token usage from an OpenAI-style ``response.usage``."""
        self.calls += 1
        if usage is None:
            return
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        if pt is None and isinstance(usage, dict):
            pt = usage.get("prompt_tokens")
            ct = usage.get("completion_tokens")
        pt = pt or 0
        ct = ct or 0
        self.total_tokens_in += pt
        self.total_tokens_out += ct
        self.total_cost_usd = (
            self.total_tokens_in / 1000.0 * self.price_in
            + self.total_tokens_out / 1000.0 * self.price_out
        )

    def check(self) -> str | None:
        if self.total_cost_usd >= self.max_cost_usd:
            return (
                f"Cost limit reached: ${self.total_cost_usd:.4f} >= "
                f"${self.max_cost_usd:.4f} cap. Stop and report to the user."
            )
        return None

    def summary(self) -> str:
        return (
            f"calls={self.calls} tokens_in={self.total_tokens_in} "
            f"tokens_out={self.total_tokens_out} cost=${self.total_cost_usd:.4f}"
        )


__all__ = [
    "DEFAULT_MAX_TURNS_PER_USER_MSG",
    "DEFAULT_MAX_TOOL_CALLS_PER_SESSION",
    "IterationCaps",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "DEFAULT_KEEP_RECENT_MESSAGES",
    "DEFAULT_MAX_TOOL_RESULT_CHARS",
    "estimate_tokens",
    "ContextBudget",
    "cap_tool_result",
    "DEFAULT_MAX_COST_USD",
    "DEFAULT_PRICE_PER_1K_IN",
    "DEFAULT_PRICE_PER_1K_OUT",
    "CostTracker",
]
