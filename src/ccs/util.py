"""Shared utilities for ccs modules."""

import json
import sys


def read_stdin_json() -> dict | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None
