# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 项目概述

Codex 的自定义状态行工具，支持多提供商（DeepSeek、Anthropic、OpenAI、Gemini）。提供双行 ANSI 显示，基于 JSONL transcript 解析精确 token 统计，避免 stdin snapshot diff 机制因 context compaction 导致的振荡和重复计数。

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

两个 CLI 命令（在 `pyproject.toml` 中定义），均由 Codex 调用，JSON 数据通过 stdin 传入：

| 命令 | 模块 | 调用者 | 用途 |
|------|------|--------|------|
| `ccs-statusline` | `statusline.py:main` | 状态行刷新定时器 | 从 stdin 读取会话 JSON + 从 SQLite 读取累积数据 → 向 stdout 输出双行 ANSI |
| `ccs-tracker` | `tracker.py:main` | Hook 事件（Stop、PostToolUse、SubagentStart/Stop） | 将事件元数据持久化到 SQLite |

### 数据流

```
Codex hooks  ──stdin JSON──→ ccs-tracker ──→ SQLite (~/.Codex/statusline/usage.db)

Codex statusline tick ──stdin JSON──→ ccs-statusline
  ├── JSONL transcript ──→ 解析 message.usage → 去重 → 按模型分组 → 写入 SQLite
  ├── subagents/agent-*.jsonl ──→ 聚合子代理 token
  ├── /api/oauth/usage（带磁盘缓存）──→ 官方订阅用量（5h/7d 窗口 + 月度预算）
  └── stdout ──→ 终端 ANSI 显示
```

### 模块职责

- **`statusline.py`** — 编排管道：解析 stdin JSON → `get_session_metrics()` 解析 JSONL transcript → `update_session_tokens()` 写入 SQLite → `get_all_totals()` / `get_model_breakdown()` 读取 → 成本计算 → 渲染。保留 stdin `current_usage` 用于 LAST cost 和 NEXT replay 估算。
- **`tracker.py`** — 将 `ccs-tracker --event <stop|tool|subagent-start|subagent-stop>` 分派到 db.py 写入。退出码始终为 0（即使出现异常）以防止 hook 错误。
- **`db.py`** — 位于 `~/.Codex/statusline/usage.db` 的 SQLite（WAL 模式）。四张表：`sessions`（会话元数据、聚合令牌/轮次/子代理计数器）、`model_usage`（按模型分解的 token 追踪）、`tool_calls`、`subagent_events`。`update_session_tokens()` 和 `update_model_usage()` 直接写入 JSONL 解析值，无需 snapshot diff。
- **`cost.py`** — 多提供商定价。解析链：`~/.Codex/statusline/pricing.yaml` → 内置 `pricing.yaml` → 后备默认值。模型 ID 匹配：先精确查找，去除 `[1m]` 后缀，逐段剥离尾部版本号。`fmt_cost_multi()` 按模型分别定价加总；`fmt_last_cost()` 处理 per-call 成本（含 Anthropic cache_write）。
- **`transcript.py`** — JSONL transcript 解析核心。`get_session_metrics()`：过滤含 `message.usage` 的条目，连续去重（user 消息打断链），按 `message.model` 分组统计，聚合子代理 `agent-*.jsonl`，检测 `compact_boundary` 压缩标记。`estimate_next_replay()`：基于转录本估算下一轮重播令牌（保留用于 NEXT 显示）。
- **`renderer.py`** — 纯 ANSI 渲染。64 级健康渐变色（绿→黄→橙→红），用于 CTX 压力条和 CACHE 命中率。双行输出，`│` 分隔符，粗体/暗色着色。CACHE 百分比 = `cache_read / (input + cache_read)`（标准缓存命中率 0-100%）。TURNS 显示 `cN` 标记压缩次数。第二行尾部渲染官方用量段（`_fmt_official_usage`）：5H/7D 滚动窗口 + MO 月度预算（USD 显示 `$used/$limit`，credits 显示数量）。
- **`usage.py`** — Codex 官方订阅用量客户端。调用驱动 `/usage` 命令的 OAuth 端点 `GET /api/oauth/usage`（必带 `User-Agent: Codex/<ver>`，否则落入严格 429 桶）。读 token 顺序：`CLAUDE_CODE_OAUTH_TOKEN` → `~/.Codex/.credentials.json`（`claudeAiOauth.accessToken`）→ macOS 钥匙串。`normalize()` 归一化两种订阅形态：**美元预算型**（enterprise，滚动窗口为 null，`extra_usage.currency==USD`）与 **额度窗口型**（team/pro/max，`five_hour`/`seven_day` 利用率 + credits）。
- **`util.py`** — 从 stdin 读取 JSON 的共享辅助函数。

### 关键行为

**Token 统计（transcript.py:get_session_metrics）：** 从 JSONL transcript 直接解析所有 API 调用的 `message.usage`，不受 context compaction 影响（compact 只插入标记，不删除旧条目）。去重策略：连续条目 usage 四元组相同且之间无 user 消息 → 保留最后一条（去除 streaming 重复）。按 `message.model` 分组，支持多模型混合会话的精确统计。

**子代理聚合（transcript.py:_subagent_metrics）：** 扫描 `{session_id}/subagents/agent-*.jsonl`，每个子代理独立解析后合并到主指标。子代理模型可能与主会话不同（如 flash vs pro），各模型独立追踪、独立计价。

**成本计算（cost.py:fmt_cost_multi）：** 从 `model_usage` 表获取每模型分解用量，分别定价后加总。JSONL 的 `input` 已是纯非缓存输入，无需减法拆解。Anthropic 提供商的 `cache_write` 独立计费。`fmt_last_cost` 使用 stdin `current_usage` 计算最近一次调用的精确成本。

**会话聚合（db.py:get_all_totals）：** 通过 `conversation_id` 隔离不同对话。子代理与主会话共享 `session_id`，同一对话的所有统计聚合到同一行。

**下一个重播估计（transcript.py:estimate_next_replay）：** 将最近轮次大小的平均值投影到最新调用输入上，以预测下一次重播成本。对上下文窗口进行比率检查以进行颜色编码（绿色 <50%，黄色 <80%，红色 ≥80%）。

**官方用量（usage.py + statusline.py:_resolve_official_usage）：** `/api/oauth/usage` 端点限流极激进，故按 TTL（默认 300s，最低 180s，`CCS_USAGE_TTL` 可调）缓存到 `~/.Codex/statusline/usage_cache.json`；状态行每次 tick 复用缓存，仅过期时发一次同步请求（4s 超时），失败按 60s 退避并沿用旧值，绝不阻塞渲染。窗口数据以 API 为权威、缺失时回落 stdin `rate_limits`（仅 Pro/Max 提供，且为 epoch 而非 ISO）。月度预算（enterprise 美元 / team credits）仅 API 提供。置 `CCS_USAGE_API=0` 关闭全部网络请求。

## 配置

安装通过 `install.sh` 进行，它创建 `~/.Codex/statusline/venv`，pip 安装包，并输出用于合并到 `~/.Codex/settings.json` 的 hook + statusLine JSON。请参阅 `examples/settings.json` 获取完整配置。

定价覆盖位于 `~/.Codex/statusline/pricing.yaml`（如果缺失，则使用内置表）。设置 `CCS_DEBUG=1` 以将原始 hook JSON 写入 `~/.Codex/statusline/debug.log`。

官方用量相关环境变量：`CCS_USAGE_API=0` 关闭 `/api/oauth/usage` 网络请求（仅用 stdin 的 5h/7d 窗口）；`CCS_USAGE_TTL` 调整缓存刷新间隔（秒，最低 180）；`CLAUDE_CODE_OAUTH_TOKEN` 显式提供 OAuth token（否则自动从凭证文件/钥匙串读取）。

## 数据库生命周期

位于 `~/.Codex/statusline/usage.db` 的 SQLite。30 天未更新的会话将被自动清理。删除 `.db` 文件以重置所有数据。`db.ensure_session()` 中的会话级幂等性缓存（`_known_sessions` 集合）在清理时被清除。
