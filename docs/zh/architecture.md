# 架构设计

> [English](../en/architecture.md)

## 概述

Claude Code Statusline 是一个为 Claude Code 设计的双行 ANSI 状态行工具，提供精确的多提供商 token 追踪和成本计算。它通过真实的提供商定价表和基于 JSONL transcript 的指标，取代了内置的成本显示功能。

## 数据流

```
                        ┌─ Stop hook ───────────────────┐
                        ├─ PostToolUse ─── ccs-tracker ──┤
                        ├─ SubagentStart ── (hook 事件) ──├── SQLite
                        └─ SubagentStop ─────────────────┘  (tool calls, subagent events)

Claude Code statusline 定时器 (每 15 秒) → ccs-statusline
  ├── stdin JSON ─── model, cost, context_window, transcript_path
  ├── JSONL transcript ─── 解析 message.usage 条目 → 去重 → 按模型聚合
  ├── subagents/agent-*.jsonl ─── 聚合子代理 token 用量
  ├── SQLite ─── 将解析值写入 sessions + model_usage 表
  └── stdout ─── 2 行 ANSI 文本
```

## CLI 入口点

两个 CLI 命令在 `pyproject.toml` 中定义，均由 Claude Code 通过 stdin 传入 JSON 调用：

| 命令 | 模块 | 调用者 | 用途 |
|------|------|--------|------|
| `ccs-statusline` | `statusline.py:main` | 状态行刷新定时器 | 从 stdin 读取会话 JSON + 从 SQLite 读取累积数据 → 向 stdout 输出双行 ANSI |
| `ccs-tracker` | `tracker.py:main` | Hook 事件（Stop、PostToolUse、SubagentStart/Stop） | 将事件元数据持久化到 SQLite。退出码始终为 0，防止 hook 错误 |

## 模块分解

### `statusline.py` — 编排管道

每次状态行定时触发时（默认每 15 秒）执行的主管道：

1. **读取 stdin**：解析 Claude Code 的会话 JSON（model、cost、context_window、transcript_path）
2. **解析 transcript**：调用 `get_session_metrics()` 从 JSONL 提取 token 用量
3. **写入数据库**：将解析后的指标持久化到 `sessions` 和 `model_usage` 表
4. **读取汇总**：从 SQLite 聚合所有 token（包括子代理）
5. **计算成本**：`fmt_cost_multi()` 计算累积成本，`fmt_last_cost()` 计算最近一次调用成本
6. **估算重播**：`estimate_next_replay()` 预测 NEXT 成本
7. **渲染输出**：双行 ANSI 输出

stdin 中的 `current_usage` 保留用于 LAST 成本（最近一次 API 调用）和 NEXT 重播估算（基于最近轮次的平均投影）。

### `tracker.py` — Hook 事件收集器

将 `--event` 类型分派到 `db.py` 处理器：

| 事件 | 动作 |
|------|------|
| `stop` | 确保会话存在，更新元数据 |
| `tool` | 记录工具调用，递增计数器 |
| `subagent-start` | 记录启动事件，递增运行中计数 |
| `subagent-stop` | 记录停止事件，递减运行中计数 |

### `db.py` — SQLite 持久化层

数据库位置：`~/.claude/statusline/usage.db`（WAL 模式，自动创建）。

四张表：

- **`sessions`** — 会话元数据，聚合 token 计数器，轮次/子代理/工具计数器
- **`model_usage`** — 按模型 `(session_id, model_id)` 的 token 分解，用于精确的多模型成本计算
- **`tool_calls`** — 每轮次的工具调用记录
- **`subagent_events`** — 子代理启动/停止事件

重要的是，`update_session_tokens()` 和 `update_model_usage()` 直接写入 JSONL 解析值——无需快照差异累积。会话使用 `conversation_id` 在并发 Claude Code 实例之间进行隔离。超过 30 天未更新的旧会话会被自动清理。

### `cost.py` — 多提供商定价

定价解析链：
```
~/.claude/statusline/pricing.yaml → 内置 pricing.yaml → 后备默认值
```

模型 ID 匹配：
1. 精确查找
2. 去除 `[1m]` 后缀（DeepSeek 上下文窗口标记）
3. 逐步剥离尾部版本号段（例如 `gpt-4o-20250501` → `gpt-4o`）

两个成本函数：
- `fmt_cost_multi()` — 从 `model_usage` 分解按模型加总成本，支持混合模型会话（例如主模型 = deepseek-v4-pro，子代理 = deepseek-v4-flash）
- `fmt_last_cost()` — 使用 stdin `current_usage` 值计算的单次调用成本，支持 Anthropic `cache_write`

### `transcript.py` — JSONL Transcript 解析器

**核心函数**：`get_session_metrics(transcript_path)`

方法：解析所有包含 `message.usage` 的 JSONL 条目，去重 streaming 重复项，按 `message.model` 分组。

**去重策略**：连续条目中 `(input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)` 四元组完全相同时视为 streaming 重复——每组仅保留最后一条。用户消息会打断链条，防止跨轮次的错误去重。

**子代理聚合**：`_subagent_metrics()` 扫描 `{session_id}/subagents/agent-*.jsonl`，对每个文件进行相同解析，并合并到主指标中。子代理可能使用不同的模型（例如 flash vs pro）——每个模型独立追踪、独立计价。

**重播估算**：`estimate_next_replay()` 从 `recent_turn_sizes()` 获取最近轮次大小的平均值，并投影到最新 API 调用输入上。通过 `detect_growth_trend()` 使用半分割比率比较（>15% 变化 = 上升/下降）检测增长趋势。

### `renderer.py` — ANSI 渲染

256 色引擎，两个指标均使用 64 级健康渐变色（绿 → 黄 → 橙 → 红）：

- **CTX 压力条**：当前水平的上下文压力
- **CACHE 百分比**：缓存命中率（反向——高命中率 = 绿色）

输出格式：
```
DeepSeek-V4-Pro │ CTX ▓▓▓░░░░░ 38% │ NEXT 248.000k→150 [¥0.014] │ TOTAL ¥0.80 │ LAST ¥0.012
TURNS 47c2 │ IN 512.384k │ OUT 84.030k │ CACHE 96.351% 13.376M │ TOOLS 63 │ AGENTS 2/1r │ 57m35s
```

**第 1 行**：模型名称 · CTX 压力条 + 百分比 · 重播估算 + 预测成本 · 会话累积成本 · 最近一次调用成本

**第 2 行**：轮次数（`cN` = N 次压缩） · 非缓存输入 tokens · 输出 tokens · 缓存命中率 + 绝对值 · 工具调用数 · 子代理（总数/运行中） · 会话时长

## 关键设计决策

### JSONL Transcript 优于快照差异

Token 统计数据**直接从 Claude Code 的 JSONL transcript 读取**，而非累积 `context_window.total_input_tokens` 快照。这种方法避免了：

1. **压缩振荡**：压缩后 `total_input_tokens` 重置，导致每次更新时出现幻影"新调用"。JSONL 条目是不可变的——压缩仅插入 `compact_boundary` 标记，从不删除历史记录。
2. **多调用合并**：15 秒更新间隔内的多次 API 调用会被合并为一次累积。JSONL 则单独记录每次调用。
3. **cache_read 重复计数**：公式 `per_call_total_input = input + cache_read` 会膨胀总计。JSONL 分离了 `input_tokens`（非缓存）和 `cache_read_input_tokens`。

### 会话隔离

每个 Claude Code 对话通过 `session_id` 标识。子代理共享相同的 `session_id`（通过 `agent_id` 区分），因此所有 hook 事件自然聚合到同一会话——无需子代理关联逻辑。`conversation_id` 列保留供将来使用；目前始终等于 `session_id`。
