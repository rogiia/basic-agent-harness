import glob as glob_module
import re
from pathlib import Path


def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read lines from a file, with optional offset and limit."""
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    lines = p.read_text(errors="replace").splitlines()
    selected = lines[offset - 1: offset - 1 + limit]
    return "\n".join(f"{offset + i}: {line}" for i, line in enumerate(selected))


def glob_files(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern inside a directory."""
    matches = glob_module.glob(f"{path}/**/{pattern}", recursive=True)
    matches += glob_module.glob(f"{path}/{pattern}")
    unique = sorted(set(matches))
    return "\n".join(unique) if unique else "(no matches)"


def grep(pattern: str, path: str = ".", include: str = "*") -> str:
    """Search file contents for a regex pattern, optionally filtering by filename glob."""
    results = []
    for filepath in glob_module.glob(f"{path}/**/{include}", recursive=True):
        fp = Path(filepath)
        if not fp.is_file():
            continue
        try:
            for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                if re.search(pattern, line):
                    results.append(f"{filepath}:{i}: {line}")
        except OSError:
            pass
    return "\n".join(results) if results else "(no matches)"


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating it if it does not exist."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} bytes to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace the first occurrence of old_string with new_string in a file."""
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    original = p.read_text()
    if old_string not in original:
        return f"Error: string not found in {path}"
    p.write_text(original.replace(old_string, new_string, 1))
    return f"Edited {path}"
