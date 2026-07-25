import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ccs import codex_transcript as ct


def _write(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def test_session_metrics_from_codex_rollout():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout-2026-07-10T00-00-00-test.jsonl"
        _write(path, [
            {
                "timestamp": "2026-07-10T00:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": "sid", "model": "gpt-5.5"},
            },
            {"timestamp": "2026-07-10T00:00:01.000Z", "type": "turn_context", "payload": {}},
            {
                "timestamp": "2026-07-10T00:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 400,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 20,
                        },
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 400,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 20,
                        },
                        "model_context_window": 874000,
                    },
                    "rate_limits": {
                        "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 9999999999},
                        "secondary": {"used_percent": 60.0, "window_minutes": 10080, "resets_at": 9999999999},
                        "plan_type": "pro",
                    },
                },
            },
            {"timestamp": "2026-07-10T00:00:03.000Z", "type": "turn_context", "payload": {}},
            {"timestamp": "2026-07-10T00:00:04.000Z", "type": "event_msg", "payload": {"type": "exec_command_begin"}},
            {"timestamp": "2026-07-10T00:00:05.000Z", "type": "event_msg", "payload": {"type": "post_compact"}},
            {
                "timestamp": "2026-07-10T00:00:06.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 2500,
                            "cached_input_tokens": 1400,
                            "output_tokens": 150,
                            "reasoning_output_tokens": 30,
                        },
                        "last_token_usage": {
                            "input_tokens": 1500,
                            "cached_input_tokens": 1000,
                            "output_tokens": 50,
                            "reasoning_output_tokens": 10,
                        },
                        "model_context_window": 874000,
                    },
                },
            },
        ])

        metrics = ct.session_metrics(path)

    assert metrics["session_id"] == "sid"
    assert metrics["model_id"] == "gpt-5.5"
    assert metrics["turn_count"] == 2
    assert metrics["tool_call_count"] == 1
    assert metrics["compaction_count"] == 1
    assert metrics["input"] == 1100
    assert metrics["cache_read"] == 1400
    assert metrics["output"] == 180
    assert metrics["context_len"] == 1500
    assert metrics["replay_tokens"] == 2000
    assert metrics["model_usage"]["gpt-5.5"]["input"] == 1100
    assert metrics["context_window_size"] == 874000
    assert metrics["official_usage"]["windows"][0]["label"] == "5H"
    assert metrics["official_usage"]["windows"][1]["label"] == "7D"


def test_latest_rollout_picks_newest_mtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = root / "2026" / "07" / "09" / "rollout-old.jsonl"
        new = root / "2026" / "07" / "10" / "rollout-new.jsonl"
        old.parent.mkdir(parents=True)
        new.parent.mkdir(parents=True)
        old.write_text("{}\n", encoding="utf-8")
        new.write_text("{}\n", encoding="utf-8")
        old.touch()
        new.touch()

        assert ct.latest_rollout(root) == new
