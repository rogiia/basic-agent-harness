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
        # base_url="http://localhost:11434/v1",
        api_key=os.environ.get("OPENAI_API_KEY")
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
            result = TOOL_REGISTRY[name](**args)

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
                "You are a helpful assistant. You have tools to read and write files, "
                "search the file system, and fetch web pages. Use them to help the user."
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
                model="gpt-5.4-mini",  # "gemma4",
                messages=messages,
                tools=TOOL_SCHEMAS,
                # temperature=0.7,
            )

            message = response.choices[0].message

            # Always append the assistant turn so the conversation stays intact
            messages.append(message)

            if message.tool_calls:
                # The LLM wants to use one or more tools — run them, then loop
                handle_tool_calls(message.tool_calls, messages)
            else:
                # No tool calls: we have the final answer
                print(f"Assistant: {message.content}")
                break


if __name__ == "__main__":
    client = get_llm_client()
    agent_loop(client)
