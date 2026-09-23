"""黄金快照 E2E：每个 tests/e2e/<case>/ 目录是一个完整场景。

对每个用例，在隔离的 HOME、冻结的时间下以子进程运行 ``ccs-statusline``，
把 stdout（含 ANSI 颜色）与 ``expected.ansi`` 逐字节比对。不访问网络。

用例目录约定：
  stdin.json          Claude Code 传给状态行的 JSON；``{case}`` 替换为用例目录的绝对路径
  transcript.jsonl    会话 transcript（可选）
  transcript/subagents/agent-*.jsonl   子代理 transcript（可选）
  home/*              复制到 ~/.claude/statusline/ 的文件：缓存、pricing.yaml 等（可选）
  env.json            额外环境变量（可选）
  expected.ansi       期望输出（黄金快照）

更新快照：CCS_UPDATE_GOLDEN=1 python -m pytest tests/test_e2e.py
更新前必须看懂 diff，并在提交说明里解释输出为什么变化。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CASES_DIR = Path(__file__).parent / "e2e"
FROZEN_NOW = 1790164800  # 2026-09-23T12:00:00Z

# 冻结时间后再调用状态行入口；后台价格刷新改为空操作，保证不碰网络。
_DRIVER = """
import datetime as _dt, sys, time
now = float(sys.argv[1])
time.time = lambda: now
import ccs.catalog, ccs.cost
class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime.fromtimestamp(now, tz)
ccs.cost.datetime = _Frozen
ccs.catalog._spawn_refresh = lambda: None
from ccs.statusline import main
main()
"""

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _cases() -> list[str]:
    return sorted(p.name for p in CASES_DIR.iterdir() if (p / "stdin.json").exists())


def _run(case_dir: Path, tmp_path: Path) -> str:
    work = tmp_path / "case"
    shutil.copytree(case_dir, work)
    home = tmp_path / "home"
    state = home / ".claude" / "statusline"
    state.mkdir(parents=True)
    if (work / "home").is_dir():
        for f in (work / "home").iterdir():
            shutil.copy(f, state / f.name)
    # 缓存文件的 mtime 与冻结时间对齐，保证它们被视为新鲜
    for f in state.iterdir():
        os.utime(f, (FROZEN_NOW - 60, FROZEN_NOW - 60))

    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "CCS_LANG": "zh",
        "CCS_BALANCE_API": "0",
    }
    if (work / "env.json").exists():
        env.update(json.loads((work / "env.json").read_text(encoding="utf-8")))

    stdin = (work / "stdin.json").read_text(encoding="utf-8").replace("{case}", str(work))
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(FROZEN_NOW)],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("case", _cases())
def test_golden(case, tmp_path):
    case_dir = CASES_DIR / case
    expected_file = case_dir / "expected.ansi"
    actual = _run(case_dir, tmp_path)

    if os.getenv("CCS_UPDATE_GOLDEN") == "1":
        expected_file.write_text(actual, encoding="utf-8")
        return

    assert expected_file.exists(), f"缺少快照，先用 CCS_UPDATE_GOLDEN=1 生成：{expected_file}"
    expected = expected_file.read_text(encoding="utf-8")
    assert _ANSI.sub("", actual) == _ANSI.sub("", expected), "文本内容与快照不一致"
    assert actual == expected, "文本一致但颜色与快照不一致"
