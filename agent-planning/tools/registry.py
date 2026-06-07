from tools.filesystem import read_file, glob_files, grep, write_file, edit_file
from tools.shell import run_bash
from tools.web import webfetch
from tools.todo import todo_append, todo_list, todo_update
from tools.scratchpad import read_scratchpad, write_scratchpad


def get_tool_registry():
    return {
        "run_bash":          run_bash,
        "read_file":         read_file,
        "glob_files":        glob_files,
        "grep":              grep,
        "write_file":        write_file,
        "edit_file":         edit_file,
        "webfetch":          webfetch,
        "todo_append":       todo_append,
        "todo_list":         todo_list,
        "todo_update":       todo_update,
        "read_scratchpad":   read_scratchpad,
        "write_scratchpad":  write_scratchpad,
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
        {
            "type": "function",
            "function": {
                "name": "todo_append",
                "description": (
                    "Add a new item to the to-do list. "
                    "Use this to track a task you plan to work on."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique identifier for the item (e.g. '1', 'task-setup').",
                        },
                        "content": {
                            "type": "string",
                            "description": "Description of the task.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done", "cancelled", "failed"],
                            "description": "Initial status of the item. Use 'pending' for new tasks.",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "todo_list",
                "description": (
                    "Read the current to-do list. "
                    "By default shows all active items (pending, in_progress, failed). "
                    "Set include_completed=true to also see done and cancelled items. "
                    "Failed items display their retry count."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_completed": {
                            "type": "boolean",
                            "description": "If true, include done and cancelled items in the output. Defaults to false.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "todo_update",
                "description": (
                    "Update the content or status of an existing to-do item. "
                    "At least one of 'content' or 'status' must be provided. "
                    "Setting a failed item back to in_progress counts as a retry and "
                    "is tracked automatically. The response will warn when the retry "
                    "limit is reached."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "ID of the to-do item to update.",
                        },
                        "content": {
                            "type": "string",
                            "description": "New description for the item. Omit to leave unchanged.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done", "cancelled", "failed"],
                            "description": "New status for the item. Omit to leave unchanged.",
                        },
                    },
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_scratchpad",
                "description": (
                    "Read the current contents of the in-memory scratchpad. "
                    "Returns '(empty)' if nothing has been written yet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_scratchpad",
                "description": (
                    "Overwrite the entire contents of the in-memory scratchpad with new content. "
                    "The previous content is permanently replaced."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The new content to store in the scratchpad.",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
    ]
