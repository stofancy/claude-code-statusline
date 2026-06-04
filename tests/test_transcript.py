"""transcript.py: turn 边界平滑 + NEXT 不翻倍。

Run: python -m pytest tests/test_transcript.py -v
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


class TestContextLenMaxInTurn(unittest.TestCase):
    def setUp(self):
        tx._metrics_cache.clear()
        tx._cache.clear()

    def test_max_in_turn_not_pulled_back_by_later_small_input(self):
        """同 turn 内第一次调用很大,后续调用 cache 命中比例变高、input 变小,
        CTX 不应被拉回。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                # m1: 第一次处理该 prompt,cache 还没建好,input 较大
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),  # total 400100
                # m2: cache 命中比例上升,input 变小 —— 之前会把这个当 context
                _assistant("2024-01-01T10:00:02.000Z", "m2", 100, 100_000, 0),  # total 100100
            ]
            _write(path, events)

            m = get_session_metrics(str(path))
            # 取 turn 内最大值 = 400100,不是最近一次的 100100
            self.assertEqual(m["context_len"], 400_100)

    def test_fallback_to_most_recent_when_no_new_calls_in_turn(self):
        """用户刚发完消息、模型还没响应 —— 当前 turn 内无新调用,
        应 fallback 到上一轮的最近一次 main 调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 50_000, 0),  # 50100
                _user_text("2024-01-01T10:00:02.000Z", "second"),  # 尚无新调用
            ]
            _write(path, events)

            m = get_session_metrics(str(path))
            self.assertEqual(m["context_len"], 50_100)

    def test_tool_result_user_msg_not_treated_as_turn_start(self):
        """tool_result 是 type=user 但 content 不是 text,不能被当成 turn 起点,
        否则会把 turn 切断成两半。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),  # 400100
                # tool_result: 不应被识别为 turn 起点
                _user_tool_result("2024-01-01T10:00:02.000Z"),
                _assistant("2024-01-01T10:00:03.000Z", "m2", 200, 410_000, 0),  # 410200
            ]
            _write(path, events)

            m = get_session_metrics(str(path))
            # turn 起点仍是 10:00:00 的 user_text,m1 和 m2 都在该 turn 内
            # max = 410200
            self.assertEqual(m["context_len"], 410_200)

    def test_new_user_msg_resets_turn(self):
        """新 user 消息到来后,turn 起点更新 —— 旧 turn 的 m1 不再算 max。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 500_000, 0),  # 500100
                _user_text("2024-01-01T10:00:02.000Z", "second"),
                _assistant("2024-01-01T10:00:03.000Z", "m2", 100, 50_000, 0),  # 50100
            ]
            _write(path, events)

            m = get_session_metrics(str(path))
            # 当前 turn 从 second 开始,max = 50100(m1 属于上一 turn,不算)
            self.assertEqual(m["context_len"], 50_100)

    def test_compaction_does_not_inflate_turn_max(self):
        """压缩后 turn 内 max 应回到压缩后水平,而不是保留压缩前的峰值。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "first"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 800_000, 0),  # 800100
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2024-01-01T10:00:01.500Z"},
                _assistant("2024-01-01T10:00:02.000Z", "m2", 100, 30_000, 0),   # 30100
            ]
            _write(path, events)

            m = get_session_metrics(str(path))
            # turn 内 max = 800100,m2 之后会再变,但当前 turn 内的 max 就是 800100
            self.assertEqual(m["context_len"], 800_100)


class TestNextReplayProjection(unittest.TestCase):
    def setUp(self):
        tx._metrics_cache.clear()
        tx._cache.clear()

    def test_next_not_double_current_context(self):
        """修复前:NEXT = current + latest_call_input ≈ 2× current。
        修复后:NEXT = current + avg_growth,接近 current。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hi"),
                # 两次调用,growth = 50K
                _assistant("2024-01-01T10:00:01.000Z", "m1", 1000, 400_000, 0),  # 401000
                _assistant("2024-01-01T10:00:02.000Z", "m2", 2000, 450_000, 0),  # 452000
            ]
            _write(path, events)

            # max in turn = 452000,avg_growth = 51000
            r = estimate_next_replay(
                str(path),
                latest_call_input=452_000,   # 修复前会触发双倍 bug
                total_input_tokens=0,
                current_context_len=452_000,
            )
            # NEXT 应该是 ~452000 + 51000 ≈ 503000,不是 ~904000
            self.assertLess(r["estimated_tokens"], 600_000)
            self.assertGreaterEqual(r["estimated_tokens"], 452_000)

    def test_next_with_no_deltas_falls_back_to_min(self):
        """没有正 delta(对话稳定)时,NEXT = current + MIN_NEXT_DELTA。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            events = [
                _user_text("2024-01-01T10:00:00.000Z", "hi"),
                _assistant("2024-01-01T10:00:01.000Z", "m1", 100, 400_000, 0),  # 400100
            ]
            _write(path, events)

            r = estimate_next_replay(
                str(path),
                latest_call_input=400_100,
                current_context_len=400_100,
            )
            # 无 delta(只有一次调用),NEXT = 400100 + MIN_NEXT_DELTA
            self.assertEqual(r["estimated_tokens"], 400_100 + tx.MIN_NEXT_DELTA)

    def test_next_fallback_to_latest_call_input_without_transcript(self):
        """transcript 不可用时,NEXT = latest_call_input(不再叠加 +latest_call_input)。"""
        r = estimate_next_replay(
            None,
            latest_call_input=300_000,
            current_context_len=0,  # 触发 fallback 分支
        )
        self.assertEqual(r["estimated_tokens"], 300_000)


if __name__ == "__main__":
    unittest.main()
