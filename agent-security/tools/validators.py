"""Lightweight JSON-Schema validator for tool inputs (checklist §3.1, §3.3).

We deliberately avoid a third-party dependency (``jsonschema`` /
``pydantic``) so the host-side validation needs no new install and no
Docker image rebuild.  This validator implements the small subset of
JSON-Schema Draft 7 actually used by ``tools/registry.get_tool_schemas``:

  - ``type``      (object, string, integer, boolean)
  - ``required``
  - ``properties``
  - ``enum``
  - ``minimum`` / ``maximum``
  - ``minLength`` / ``maxLength``

§3.3 (limit output scope) is enforced by the same schemas: bounds on
``offset``, ``limit``, ``command`` length, ``content`` length, and a
rejection of absolute-path glob patterns are baked into the schemas and
therefore checked here.

The validator returns ``(ok, errors)`` where ``errors`` is a list of
human-readable strings suitable for surfacing back to the LLM.
"""

from __future__ import annotations

import json
from typing import Any


class ValidationError(Exception):
    """Raised when a tool's args fail schema validation."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _check_type(value: Any, expected: str) -> str | None:
    if expected == "object":
        if not isinstance(value, dict):
            return f"expected object, got {type(value).__name__}"
    elif expected == "string":
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
    elif expected == "integer":
        # bool is a subclass of int — reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"expected integer, got {type(value).__name__}"
    elif expected == "boolean":
        if not isinstance(value, bool):
            return f"expected boolean, got {type(value).__name__}"
    else:
        return f"unknown type '{expected}'"
    return None


def validate_args(args: dict[str, Any], schema: dict) -> tuple[bool, list[str]]:
    """Validate *args* against a tool's JSON-Schema function spec.

    ``schema`` is the inner ``{"type": "object", "properties": ...}``
    dict — i.e. ``tool["function"]["parameters"]``.

    Returns ``(ok, errors)``.
    """
    errs: list[str] = []

    # Top-level type check.
    if "type" in schema and schema["type"] != "object":
        msg = _check_type(args, schema["type"])
        if msg:
            return False, [msg]

    # required fields.
    required = schema.get("required", [])
    for field in required:
        if field not in args:
            errs.append(f"missing required field '{field}'")

    properties = schema.get("properties", {})
    for name, value in args.items():
        if name not in properties:
            # Extra unknown fields are reported (strict mode). The LLM
            # should not invent parameters the schema doesn't list.
            errs.append(f"unknown field '{name}'")
            continue
        errs.extend(_validate_value(value, properties[name], name))

    return (len(errs) == 0), errs


# ---------------------------------------------------------------------------
# Bounded schemas (§3.3 limit output scope)
# ---------------------------------------------------------------------------

def bounded_schemas(raw_schemas: list[dict]) -> list[dict]:
    """Return a copy of *raw_schemas* with §3.3 bounds injected.

    We mutate copies of the per-tool parameter schemas to add:

      - ``read_file.offset``:  minimum 1, maximum 1000000
      - ``read_file.limit``:   minimum 1, maximum 2000
      - ``write_file.content``: maxLength 1_048_576  (1 MB)
      - ``edit_file.old_string`` / ``new_string``: maxLength 1_048_576
      - ``run_bash.command``:   minLength 1, maxLength 4096
      - ``webfetch.url``:       maxLength 4096
      - ``glob_files.pattern``: reject absolute paths (pattern check
        implemented in ``_validate_value`` via a custom constraint —
        here we add ``format: relative-path`` which our validator
        treats specially).

    The bounds are conservative defaults; they can be tuned without
    touching the validator.
    """
    out: list[dict] = []
    for entry in raw_schemas:
        fn = entry["function"]
        params = json.loads(json.dumps(fn["parameters"]))  # deep copy
        name = fn["name"]
        props = params.setdefault("properties", {})

        if name == "read_file":
            props.setdefault("offset", {}).setdefault("minimum", 1)
            props["offset"]["maximum"] = 1000000
            props.setdefault("limit", {}).setdefault("minimum", 1)
            props["limit"]["maximum"] = 2000

        elif name in ("write_file", "edit_file"):
            for field in ("content", "old_string", "new_string"):
                if field in props:
                    props[field]["maxLength"] = 1_048_576  # 1 MB

        elif name == "run_bash":
            props.setdefault("command", {}).setdefault("minLength", 1)
            props["command"]["maxLength"] = 4096

        elif name == "webfetch":
            props.setdefault("url", {}).setdefault("maxLength", 4096)

        elif name == "glob_files":
            # Custom constraint enforced by the validator: reject
            # patterns that start with "/" (absolute path) since those
            # would escape the working-dir scoping.
            props.setdefault("pattern", {})["format"] = "relative-path"

        out.append({"type": "function", "function": {**fn, "parameters": params}})
    return out


# ---------------------------------------------------------------------------
# Validator registry
# ---------------------------------------------------------------------------

class ToolValidator:
    """Validate tool call arguments against their schemas."""

    def __init__(self, schemas: list[dict]):
        self._params: dict[str, dict] = {}
        for entry in schemas:
            fn = entry["function"]
            self._params[fn["name"]] = fn["parameters"]

    def validate(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, list[str]]:
        schema = self._params.get(tool_name)
        if schema is None:
            return False, [f"unknown tool '{tool_name}'"]
        return validate_args(args, schema)


def _validate_value(value: Any, schema: dict, path: str) -> list[str]:
    """Validate a single value against its property schema."""
    errs: list[str] = []

    if "type" in schema:
        msg = _check_type(value, schema["type"])
        if msg:
            errs.append(f"{path}: {msg}")
            return errs

    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: '{value}' is not one of {schema['enum']}")

    if schema.get("type") == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: length {len(value)} < minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: length {len(value)} > maxLength {schema['maxLength']}")
        # §3.3 custom format: relative-path (reject absolute glob patterns).
        if schema.get("format") == "relative-path" and value.startswith("/"):
            errs.append(f"{path}: absolute paths are not allowed here (must be relative)")

    if schema.get("type") == "integer":
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > maximum {schema['maximum']}")

    return errs


__all__ = [
    "ValidationError",
    "validate_args",
    "bounded_schemas",
    "ToolValidator",
]
