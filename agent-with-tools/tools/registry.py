from tools.filesystem import read_file, glob_files, grep, write_file, edit_file
from tools.shell import run_bash
from tools.web import webfetch


def get_tool_registry():
    return {
        "run_bash":   run_bash,
        "read_file":  read_file,
        "glob_files": glob_files,
        "grep":       grep,
        "write_file": write_file,
        "edit_file":  edit_file,
        "webfetch":   webfetch,
    }


def get_tool_schemas():
    return [
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Run a bash command on the user's machine and return the output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute.",
                        }
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read lines from a file. Returns lines prefixed with line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or relative path to the file."},
                        "offset": {"type": "integer", "description": "First line to read (1-indexed). Defaults to 1."},
                        "limit": {"type": "integer", "description": "Maximum number of lines to return. Defaults to 200."},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob_files",
                "description": "Find files matching a glob pattern (e.g. '**/*.py') inside a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob pattern to match against file names."},
                        "path": {"type": "string", "description": "Root directory to search in. Defaults to '.'."},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Search file contents for a regex pattern and return matching lines with file paths and line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regular expression to search for."},
                        "path": {"type": "string", "description": "Directory to search in. Defaults to '.'."},
                        "include": {"type": "string", "description": "Filename glob to restrict which files are searched (e.g. '*.py'). Defaults to '*'."},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file, creating it (and any missing parent directories) if it does not exist.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path of the file to write."},
                        "content": {"type": "string", "description": "Full content to write to the file."},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace the first occurrence of a string in a file with a new string.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path of the file to edit."},
                        "old_string": {"type": "string", "description": "Exact string to find and replace."},
                        "new_string": {"type": "string", "description": "String to replace it with."},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "webfetch",
                "description": (
                    "Fetch a public URL (http/https only) and return its full plain-text content (up to 2 MB)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to fetch (http/https)."},
                    },
                    "required": ["url"],
                },
            },
        },
    ]
