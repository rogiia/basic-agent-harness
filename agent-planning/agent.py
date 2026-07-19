import os
import json
from openai import OpenAI
from tools import get_tool_registry, get_tool_schemas
from dotenv import load_dotenv

load_dotenv()

TOOL_REGISTRY = get_tool_registry()
TOOL_SCHEMAS = get_tool_schemas()

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def get_llm_client():
    return OpenAI(
        base_url="http://localhost:11434/v1",
        api_key="."
    )


def handle_tool_calls(tool_calls, messages):
    """Execute each tool the LLM requested and append the results to messages."""
    for tool_call in tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        print(f"  [tool] {name}({args})")

        if name not in TOOL_REGISTRY:
            result = f"Error: unknown tool '{
                name}'. Available tools: {list(TOOL_REGISTRY.keys())}"
        else:
            try:
                result = TOOL_REGISTRY[name](**args)
            except TypeError as e:
                result = (
                    f"Error: invalid arguments for tool '{name}': {e}. "
                    "Check the tool schema and retry with the correct arguments."
                )

        print(f"  [tool result] {result[:200]}{
              '...' if len(result) > 200 else ''}")

        # The LLM needs the result tied back to the specific tool call id
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })


def agent_loop(client):
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
                "Items carry a status: pending, in_progress, done, cancelled, or failed.\n\n"

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
                handle_tool_calls(message.tool_calls, messages)
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
    client = get_llm_client()
    agent_loop(client)
