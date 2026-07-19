def ask_question(question: str) -> str:
    """Ask the user a clarifying question and return their answer."""
    print(f"\n  [agent] {question}")
    try:
        answer = input("  Your answer: ").strip()
    except EOFError:
        return "(no answer — EOF)"
    return answer if answer else "(no answer provided)"
