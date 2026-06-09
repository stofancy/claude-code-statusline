"""Hook event collector CLI. Called by Claude Code hooks to persist session events.

Usage:
  ccs-tracker --event <stop|tool|subagent-start|subagent-stop>

JSON data is read from stdin (piped by Claude Code).
"""

import json
import os
import sys
import time
from pathlib import Path

from .renderer import _fmt_model_name
from . import db
from .util import exit_with_json, read_stdin_json

DEBUG_LOG = Path.home() / ".claude" / "statusline" / "debug.log"


def _debug(event: str, data: dict) -> None:
    if os.getenv("CCS_DEBUG") != "1":
        return
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a") as f:
            f.write(f"\n=== {event} @ {int(time.time())} ===\n")
            safe = {k: v for k, v in data.items() if k not in ("message_context", "last_assistant_message")}
            f.write(json.dumps(safe, default=str))
            f.write("\n")
    except Exception:
        pass


def _handle_stop(data: dict) -> None:
    _debug("stop", data)
    session_id = data.get("session_id", "")
    if not session_id:
        return
    model = data.get("model", {})
    model_id = model.get("id", "") if isinstance(model, dict) else ""
    display_name = model.get("display_name", "") if isinstance(model, dict) else ""
    model_name = _fmt_model_name(model_id, display_name) if model_id else ""
    db.ensure_session(session_id, model_id, model_name)


def _handle_tool(data: dict) -> None:
    _debug("tool", data)
    session_id = data.get("session_id", "")
    if not session_id:
        return
    tool_name = data.get("tool_name", "unknown")
    db.ensure_session(session_id)
    db.record_tool_call(session_id, tool_name)


def _handle_subagent_start(data: dict) -> None:
    _debug("subagent-start", data)
    session_id = data.get("session_id", "")
    if not session_id:
        return
    db.ensure_session(session_id)
    db.record_subagent_start(session_id,
                             data.get("agent_type", "unknown"),
                             data.get("agent_id", "unknown"))


def _handle_subagent_stop(data: dict) -> None:
    _debug("subagent-stop", data)
    session_id = data.get("session_id", "")
    if not session_id:
        return
    db.ensure_session(session_id)
    db.record_subagent_stop(session_id,
                            data.get("agent_type", "unknown"),
                            data.get("agent_id", "unknown"))


_HANDLERS = {
    "stop": _handle_stop,
    "tool": _handle_tool,
    "subagent-start": _handle_subagent_start,
    "subagent-stop": _handle_subagent_stop,
}


def main() -> None:
    event = ""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--event" and i + 1 < len(args):
            event = args[i + 1]
            break

    if not event:
        print("Usage: ccs-tracker --event <stop|tool|subagent-start|subagent-stop>", file=sys.stderr)
        sys.exit(1)

    handler = _HANDLERS.get(event)
    if not handler:
        print(f"Unknown event: {event}", file=sys.stderr)
        sys.exit(1)

    data = read_stdin_json()
    # Harness parses hook stdout as JSON; empty string → "JSON validation failed".
    # Emit a valid empty object so the harness sees a clean no-op result.
    if not data:
        exit_with_json({})

    try:
        db.init_db()
        handler(data)
    except Exception as exc:
        # Distinguish "error" from "no data" by embedding error detail in the JSON
        # payload.  The harness does NOT capture stderr, so the error message MUST
        # travel through stdout to be visible.
        exit_with_json(
            {"error": str(exc)},
            stderr_msg=f"ccs-tracker error ({event}): {exc}",
        )

    # Success path: handler completed without error.  This is the most common
    # path (valid stdin, DB write succeeds) and MUST also emit valid JSON —
    # the harness parses stdout on EVERY hook invocation, not just errors.
    exit_with_json({})


if __name__ == "__main__":
    main()
