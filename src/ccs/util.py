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
    except (json.JSONDecodeError, OSError):
        return None
