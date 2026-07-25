"""transcript.py: 稳态 context_len + NEXT 不翻倍 + 轮次语义。

Run: python -m unittest tests.test_transcript -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.transcript as tx
from ccs.transcript import get_session_metrics, estimate_next_replay


def _write(path: Path, events: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _user_text(ts: str, text: str) -> dict:
    return {"type": "user",
            "message": {"role": "user", "content": text},
            "timestamp": ts}


def _user_tool_result(ts: str) -> dict:
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "content": "result data",
                                     "tool_use_id": "x"}]},
            "timestamp": ts}


def _user_multi_tool_results(ts: str) -> dict:
    """多个无 text 的 block——旧 _extract_text 会拼出 '\\n' 假阳性。"""
    return {"type": "user",
            "message": {"role": "user",
                        "content": [
                            {"type": "tool_result", "content": "a", "tool_use_id": "1"},
                            {"type": "tool_result", "content": "b", "tool_use_id": "2"},
                        ]},
            "timestamp": ts}


def _assistant(ts: str, msg_id: str,
               it: int, cr: int, cw: int, ot: int = 50) -> dict:
    return {"type": "assistant",
            "message": {
                "id": msg_id,
                "role": "assistant",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": it,
                    "cache_read_input_tokens": cr,
                    "cache_creation_input_tokens": cw,
                    "output_tokens": ot,
                },
                "content": [{"type": "text", "text": "thinking"}],
            },
            "timestamp": ts}


class TestContextLenStableRecent(unittest.TestCase):
    def setUp(self):
        tx._metrics_cache.clear()
        tx._cache.clear()

    def test_uses_most_recent_stable_not_turn_max(self):
        """context_len 取最近稳态，而不是历史峰值。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),
                _assistant("2024-01-01T10:00:02.000Z", "m2", 100, 100_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 100_100)

    def test_cold_cache_spike_does_not_lock_context_len(self):
        """cache rebuild 尖峰（含后续恢复）不应锁住 CTX。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "work"),
                _assistant("2024-01-01T10:00:01.000Z", "m1",
                           it=11_497, cr=217_326, cw=294),
                _assistant("2024-01-01T10:00:02.000Z", "m2",
                           it=55, cr=0, cw=567_315),
                _assistant("2024-01-01T10:00:03.000Z", "m3",
                           it=11_580, cr=217_748, cw=551),
                _assistant("2024-01-01T10:00:04.000Z", "m4",
                           it=11_573, cr=218_594, cw=229),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 230_396)

            # tip 落在尖峰：无后继时靠 rebuild 形态回落
            path2 = Path(tmp) / "s2.jsonl"
            _write(path2, events[:3])
            tx._metrics_cache.clear()
            tx._cache.clear()
            m2 = get_session_metrics(str(path2))
            self.assertEqual(m2["context_len"], 229_117)

    def test_residual_cache_read_spike_filtered_with_next(self):
        """带 residual cache_read 的尖峰，有后继回到稳态时应被过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "work"),
                _assistant("2024-01-01T10:00:01.000Z", "m1",
                           it=10_000, cr=200_000, cw=0),
                _assistant("2024-01-01T10:00:02.000Z", "m2",
                           it=50, cr=1_000, cw=500_000),  # 501050
                _assistant("2024-01-01T10:00:03.000Z", "m3",
                           it=10_000, cr=201_000, cw=0),  # 211000
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 211_000)

    def test_out_of_order_timestamps_use_time_not_file_order(self):
        """main 调用按 timestamp 排序，而非 JSONL 文件顺序。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hi"),
                # 文件里先写较新的大值，再写较旧的小值
                _assistant("2024-01-01T10:00:02.000Z", "m2", 100, 300_000, 0),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 100_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 300_100)

    def test_fallback_to_most_recent_when_no_new_calls_in_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 50_000, 0),
                _user_text("2024-01-01T10:00:02.000Z", "second"),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 50_100)

    def test_tool_result_user_msg_not_treated_as_turn_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),
                _user_tool_result("2024-01-01T10:00:02.000Z"),
                _assistant("2024-01-01T10:00:03.000Z", "m2", 200, 410_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 410_200)
            # 轮次只计文本 user
            self.assertEqual(m["turn_count"], 1)

    def test_multi_tool_result_not_counted_as_turn(self):
        """多个 tool_result 不得因 '\\n' 假阳性计入轮次。"""
        self.assertEqual(tx._extract_text(_user_multi_tool_results("t")), "")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 50_000, 0),
                _user_multi_tool_results("2024-01-01T10:00:02.000Z"),
                _assistant("2024-01-01T10:00:03.000Z", "m2", 100, 51_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["turn_count"], 1)

    def test_slash_command_echo_not_counted_as_turn(self):
        """/model 等本地命令回显不是对话轮次。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hello"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 10_000, 0),
                _user_text("2024-01-01T10:00:02.000Z",
                           "<command-name>/model</command-name>\n"
                           "<command-message>model</command-message>"),
                _user_text("2024-01-01T10:00:03.000Z",
                           "<local-command-stdout>Set model to Opus</local-command-stdout>"),
                _user_text("2024-01-01T10:00:04.000Z", "continue please"),
                _assistant("2024-01-01T10:00:05.000Z", "m2", 100, 11_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["turn_count"], 2)

    def test_new_user_msg_uses_latest_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 500_000, 0),
                _user_text("2024-01-01T10:00:02.000Z", "second"),
                _assistant("2024-01-01T10:00:03.000Z", "m2", 100, 50_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 50_100)
            self.assertEqual(m["turn_count"], 2)

    def test_compaction_uses_post_compact_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 800_000, 0),
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2024-01-01T10:00:01.500Z"},
                _assistant("2024-01-01T10:00:02.000Z", "m2", 100, 30_000, 0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 30_100)

    def test_subagent_turns_not_merged_into_main_turn_count(self):
        """子代理 token 合并，但 turn_count / context_len 保持主会话。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "sess.jsonl"
            sub_dir = root / "sess" / "subagents"
            sub_dir.mkdir(parents=True)
            _write(main, [
                _user_text("2024-01-01T10:00:00.000Z", "main"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 10_000, 0),
            ])
            _write(sub_dir / "agent-x.jsonl", [
                _user_text("2024-01-01T10:00:02.000Z", "sub1"),
                _assistant("2024-01-01T10:00:03.000Z", "s1", 500, 0, 0),
                _user_text("2024-01-01T10:00:04.000Z", "sub2"),
                _assistant("2024-01-01T10:00:05.000Z", "s2", 700, 0, 0),
            ])
            m = get_session_metrics(str(main))
            self.assertEqual(m["turn_count"], 1)
            self.assertEqual(m["context_len"], 10_100)
            self.assertEqual(m["input"], 100 + 500 + 700)


class TestNextReplayProjection(unittest.TestCase):
    def setUp(self):
        tx._metrics_cache.clear()
        tx._cache.clear()

    def test_next_not_double_current_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hi"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 1000, 400_000, 0),
                _assistant("2024-01-01T10:00:02.000Z", "m2", 2000, 450_000, 0),
            ]
            _write(path, events)
            r = estimate_next_replay(
                str(path),
                latest_call_input=452_000,
                total_input_tokens=0,
                current_context_len=452_000,
            )
            self.assertLess(r["estimated_tokens"], 600_000)
            self.assertGreaterEqual(r["estimated_tokens"], 452_000)

    def test_next_with_no_deltas_falls_back_to_min(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hi"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),
            ]
            _write(path, events)
            r = estimate_next_replay(
                str(path),
                latest_call_input=400_100,
                current_context_len=400_100,
            )
            self.assertEqual(r["estimated_tokens"], 400_100 + tx.MIN_NEXT_DELTA)

    def test_next_fallback_to_latest_call_input_without_transcript(self):
        r = estimate_next_replay(
            None,
            latest_call_input=300_000,
            current_context_len=0,
        )
        self.assertEqual(r["estimated_tokens"], 300_000)

    def test_cache_write_to_cache_read_transition_not_inflating_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hello"),
                _assistant("2024-01-01T10:00:01.000Z", "m1",
                           it=2750, cr=0, cw=80000),
                _assistant("2024-01-01T10:00:02.000Z", "m2",
                           it=2000, cr=80000, cw=0),
                _assistant("2024-01-01T10:00:03.000Z", "m3",
                           it=2200, cr=80500, cw=0),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 82700)
            r = estimate_next_replay(
                str(path),
                latest_call_input=0,
                current_context_len=82700,
            )
            self.assertLess(r["estimated_tokens"], 90_000)
            self.assertGreaterEqual(r["estimated_tokens"], 82700)

    def test_cold_cache_spike_excluded_from_next_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "work"),
                _assistant("2024-01-01T10:00:01.000Z", "m1",
                           it=11_000, cr=218_000, cw=200),
                _assistant("2024-01-01T10:00:02.000Z", "m2",
                           it=55, cr=0, cw=567_000),
                _assistant("2024-01-01T10:00:03.000Z", "m3",
                           it=11_100, cr=218_500, cw=200),
            ]
            _write(path, events)
            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 229_800)
            r = estimate_next_replay(
                str(path),
                current_context_len=m["context_len"],
            )
            # 真实增长仅 ~600；NEXT 应贴近 229.8k，绝不能到 250k+
            self.assertLess(r["estimated_tokens"], 232_000)
            self.assertGreaterEqual(r["estimated_tokens"], 229_800)


if __name__ == "__main__":
    unittest.main()
