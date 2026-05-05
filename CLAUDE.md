# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Claude Code 的自定义状态行工具，为非 Anthropic 提供商（DeepSeek、OpenAI、Gemini）优化。提供双行 ANSI 显示，包含准确的成本计算、令牌指标、缓存效率跟踪和子代理聚合。

## 构建与开发命令

```bash
# 安装依赖（在虚拟环境中）
pip install -e .

# 直接运行 CLI 入口
python -m ccs.statusline          # ccs-statusline 入口（从 stdin 读取 JSON）
python -m ccs.tracker             # ccs-tracker 入口（--event <type>）

# 类型检查（如可用）
mypy src/ccs/

# 测试（如添加测试）
python -m pytest tests/
```

没有 lint/formatter 配置。包不声明开发依赖。

## 架构

### 入口点

两个 CLI 命令（在 `pyproject.toml` 中定义），均由 Claude Code 调用，JSON 数据通过 stdin 传入：

| 命令 | 模块 | 调用者 | 用途 |
|------|------|--------|------|
| `ccs-statusline` | `statusline.py:main` | 状态行刷新定时器 | 从 stdin 读取会话 JSON + 从 SQLite 读取累积数据 → 向 stdout 输出双行 ANSI |
| `ccs-tracker` | `tracker.py:main` | Hook 事件（Stop、PostToolUse、SubagentStart/Stop） | 将事件元数据持久化到 SQLite |

### 数据流

```
Claude Code hooks  ──stdin JSON──→ ccs-tracker ──→ SQLite (~/.claude/statusline/usage.db)
Claude Code statusline tick ──stdin JSON + SQLite──→ ccs-statusline ──stdout──→ 终端 ANSI 显示
```

### 模块职责

- **`statusline.py`** — 编排管道：解析 stdin JSON → 与上次快照比较以检测新 API 调用 → 累积到 SQLite → 解析转录本以估算重播 → 按 conversation_id 汇总所有会话 → 计算成本 → 渲染
- **`tracker.py`** — 将 `ccs-tracker --event <stop|tool|subagent-start|subagent-stop>` 分派到 db.py 写入。在处理程序分派之前，退出码始终为 0（即使出现异常）以防止 hook 错误。
- **`db.py`** — 位于 `~/.claude/statusline/usage.db` 的 SQLite（WAL 模式）。四张表：`sessions`（会话元数据、聚合令牌/轮次/子代理计数器、工具计数）、`model_usage`（按模型分解的 token 追踪，独立 snapshot 检测）、`tool_calls`、`subagent_events`。关键设计：官方文档确认子代理与主会话共享 `session_id`，所有 hook 事件天然归入同一会话行，无需外部子代理链接机制。
- **`cost.py`** — 多提供商定价。解析链：`~/.claude/statusline/pricing.yaml` → 内置 `pricing.yaml` → 后备默认值。支持 `input_per_1m`、`cache_read_per_1m`、`output_per_1m` 键。模型 ID 通过去除 `[1m]` 后缀和尾随版本号进行规范化。
- **`transcript.py`** — 解析 JSONL 转录本文件（通过 mtime 缓存）以估算下一轮重播令牌和轮次计数。使用粗略的 `len(text)/3.5` 令牌估计。如果转录本路径不可用，则回退到 `latest_call_input`。
- **`renderer.py`** — 纯 ANSI 渲染。64 级健康渐变色（绿→黄→橙→红），用于上下文压力条和缓存命中率。双行输出，由 `│` 分隔符分隔，粗体/暗色着色，人类可读的令牌/持续时间格式。
- **`util.py`** — 从 stdin 读取 JSON 的共享辅助函数。

### 关键行为

**累积（db.py:accumulate）：** 双层累积架构。(1) 会话层级（`sessions` 表）：当 `total_input_tokens` 快照与 `last_snapshot` 不同时，检测新 API 调用并累加到聚合计数器，用于显示。(2) 模型层级（`model_usage` 表）：每个模型独立维护 snapshot 和计数器，避免主会话/子代理不同上下文之间的 snapshot 振荡导致的重复计数，同时为 `fmt_cost_multi()` 提供精确的按模型定价数据。

**成本计算（cost.py:fmt_cost_multi）：** 按模型分别定价后加总，解决旧 `fmt_cost` 用单一模型定价混合 token 的问题。从 `model_usage` 表获取每模型分解用量，对 pro/flash 等不同模型使用各自定价，求和得到精确累积成本。`fmt_last_cost` 保留用于 per-call 精确成本。

**会话聚合（db.py:get_all_totals）：** 通过 `conversation_id` 隔离不同对话（并发 Claude Code 实例互不干扰）。子代理与主会话共享 `session_id`（官方文档确认），因此同一对话的所有统计天然聚合到同一行，无需外部链接机制。

**下一个重播估计（transcript.py:estimate_next_replay）：** 将最近轮次大小的平均值投影到最新调用输入上，以预测下一次重播成本。对上下文窗口进行比率检查以进行颜色编码（绿色 <50%，黄色 <80%，红色 ≥80%）。

## 配置

安装通过 `install.sh` 进行，它创建 `~/.claude/statusline/venv`，pip 安装包，并输出用于合并到 `~/.claude/settings.json` 的 hook + statusLine JSON。请参阅 `examples/settings.json` 获取完整配置。

定价覆盖位于 `~/.claude/statusline/pricing.yaml`（如果缺失，则使用内置表）。设置 `CCS_DEBUG=1` 以将原始 hook JSON 写入 `~/.claude/statusline/debug.log`。

## 数据库生命周期

位于 `~/.claude/statusline/usage.db` 的 SQLite。30 天未更新的会话将被自动清理。删除 `.db` 文件以重置所有数据。`db.ensure_session()` 中的会话级幂等性缓存（`_known_sessions` 集合）在清理时被清除。
