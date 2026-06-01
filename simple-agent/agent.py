import os
from openai import OpenAI


def get_llm_client():
    return OpenAI(
        base_url="http://localhost:11434/v1",
        api_key=""
    )


def agent_loop(client):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."}
    ]

    while True:
        user_input = input("You: ")
        if user_input.lower() == "\\exit":
            break

        messages.append({"role": "user", "content": user_input})

        response = client.chat.completions.create(
            model="gemma4",
            messages=messages,
            temperature=0.7,
        )

        reply = response.choices[0].message.content
        print(f"Assistant: {reply}")

        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    client = get_llm_client()
    agent_loop(client)
