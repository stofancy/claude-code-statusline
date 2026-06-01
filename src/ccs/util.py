"""Shared utilities for ccs modules."""

import json
import sys


def _force_utf8_stdio() -> None:
    """Ensure stdout/stderr emit UTF-8 regardless of platform console encoding.

    On Windows, Python defaults to the active code page (e.g. cp1252) for
    console streams. Renderer output contains box-drawing characters that crash
    cp1252 encoders. Calling reconfigure() once at import time fixes this for
    every downstream `print()`.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_stdio()


def read_stdin_json() -> dict | None:
    try:
        if hasattr(sys.stdin, "reconfigure"):
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, OSError, ValueError):
        # ValueError catches UnicodeDecodeError (subclass) when stdin bytes
        # cannot be decoded with the configured encoding (e.g., cp1252 on
        # Windows receiving UTF-8 pipe data).
        return None


def exit_with_json(data: dict, *, stderr_msg: str | None = None) -> None:
    """Print *data* as JSON to stdout and exit with code 0.

    The Claude Code hook harness parses hook command stdout as JSON.
    Without this, an empty stdout causes ``JSONDecodeError: Expecting value``
    (reported as "JSON validation failed" in the Claude Code UI).

    If *stderr_msg* is provided, it is printed to stderr before the JSON
    output.  The harness may or may not surface stderr, so error context
    should also be embedded in *data* when relevant (e.g.
    ``{"error": "..."}``).
    """
    if stderr_msg is not None:
        print(stderr_msg, file=sys.stderr)
    print(json.dumps(data))
    sys.exit(0)
