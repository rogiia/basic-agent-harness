import subprocess


def run_bash(command: str) -> str:
    """Run a bash command and return its output."""
    result = subprocess.run(
        command, shell=True, text=True, capture_output=True
    )
    output = result.stdout
    if result.stderr:
        output += f"\nSTDERR:\n{result.stderr}"
    return output or "(no output)"
