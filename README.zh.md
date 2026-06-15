# Claude Code Statusline

[English](README.md) | **中文**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

面向 **多提供商**（DeepSeek、Anthropic、OpenAI、Gemini）优化的 [Claude Code](https://code.claude.com) 生产级自定义状态行。双行 ANSI 显示，支持精确成本计算、基于 JSONL transcript 的 token 统计、缓存命中率、压缩检测和子代理聚合。

## 为什么需要这个工具

Claude Code 内置的 `cost.total_cost_usd` 假定使用 Anthropic USD 定价。如果你使用 DeepSeek（¥/CNY）、Gemini 或 OpenAI，内置成本数据毫无意义。更关键的是，`context_window.total_input_tokens` 在每次 context compaction 后会重置，使得增量 snapshot-diff 累积变得不可靠。本工具：

- 直接解析 Claude Code **磁盘上的 JSONL transcript**，获取权威 token 计数
- 使用 **实际提供商定价表** 计算成本（多币种、按模型）
- 将 **缓存命中率** 显示为标准的 0–100% 指标
- 检测并显示 **压缩次数**（TURNS 上的 `cN` 后缀）
- 估算 **下一轮重播令牌数**，在上下文溢出前发出预警
- 将 **子代理 transcript token**（来自 `subagents/agent-*.jsonl`）聚合到主会话
- 显示 **Claude 官方订阅用量**——即 `/usage` 命令背后的数据：5 小时/7 天滚动窗口及月度预算（Enterprise 美元消耗或 Team/Max credits）
- 在通过非 Anthropic 提供商运行时显示 **提供商余额 / 额度 / 配额**（DeepSeek ¥ 余额、OpenAI $ 额度、MiniMax 配额 %）

## 显示效果

```
DeepSeek-V4-Pro │ CTX ▓▓▓░░░░░ 38% │ NEXT 248.000k→150 [¥0.014] │ TOTAL ¥0.80 │ LAST ¥0.012
TURNS 47c2 │ IN 512.384k │ OUT 84.030k │ CACHE 96.351% 13.376M │ TOOLS 63 │ AGENTS 2/1r │ 57m35s
```

![Claude Code 状态行演示](docs/screenshot.png)

**第一行：** 模型名称 · 上下文压力（64 级渐变色条 + 百分比）· 重播估算及预测成本 · 会话累计成本 · 上一轮成本

**第二行：** 轮次计数（`cN` = N 次压缩）· 非缓存输入令牌 · 输出令牌 · 缓存命中率 + 绝对值 · 工具调用次数 · 子代理（总计/运行中）· 会话持续时间

颜色规则：上下文压力 0–100% 采用绿 → 黄 → 红渐变；缓存健康度同样使用绿 → 黄 → 红（反色——命中率高为绿色）。256 色 64 级 ANSI 色阶。

### 指标释义

**第一行** — 上下文 + 成本：

| 字段 | 示例 | 含义 |
|------|------|------|
| `MODEL` | `DeepSeek-V4-Pro` | 当前模型名称 |
| `CTX ▓▓░░ 38%` | `CTX ▓▓▓░░░░░ 38%` | 上下文压力。基于 transcript `context_len`（最近一次 API 调用总令牌数）计算，不受 compaction 重置影响。绿 <50%，黄 50–80%，红 >80% |
| `NEXT` | `248.000k→150 [¥0.014]` | 预计下一轮上下文 = 当前 `context_len` + 平均每轮增长增量。→ 预估输出 [预测成本]。超过 50% 变黄，超过 80% 变红 |
| `TOTAL` | `¥0.80` | 会话累计成本。按模型分别计价后加总 |
| `LAST` | `¥0.012` | 最近一次 API 调用的精确成本。使用 stdin 的 `current_usage`，非累计值 |

**第二行** — 会话统计：

| 字段 | 示例 | 含义 |
|------|------|------|
| `TURNS` | `47c2` | 轮次数。`cN` 后缀 = N 次 context compaction。无后缀 = 零压缩 |
| `IN` | `512.384k` | 总非缓存输入令牌。从 JSONL transcript 解析，不受压缩重置影响 |
| `OUT` | `84.030k` | 所有 API 调用的总输出令牌 |
| `CACHE` | `96.351% 13.376M` | 缓存命中率 + 缓存读取令牌绝对值。公式：`cache_read / (input + cache_read)`。反色——命中率越高越绿 |
| `TOOLS` | `63` | 工具调用次数（含 `PostToolUseFailure` 钩子记录的失败调用） |
| `AGENTS` | `2/1r` | 子代理事件：总启动数 / 当前运行数（`r` 后缀）。无运行中时只显示总数 |
| 持续时间 | `57m35s` | 会话已用时间，从 `started_at` 时间戳计算 |

**CACHE 说明**：百分比使用 JSONL 解析的 cache_read 与 input 计算，而非 snapshot 值。非缓存输入（`IN`）已排除 cache_read，避免重复计数。

**NEXT 估算逻辑**：取转录中最近几轮（最近 5 个 user→assistant 周期）的平均大小，投射到最新 API 调用的输入上。通过半切分比率检测增长趋势（稳定/上升/下降）。当 `estimated_tokens / ctx_window_size` 超过 50% 变黄，超过 80% 变红作为溢出预警。

## 官方订阅用量

当你以 Claude.ai 订阅账户登录 Claude Code 后，状态行可在第二行末尾追加你的**官方用量**——与 `/usage` 命令显示的数字一致：

```
… │ 5H 24% 4h57m │ 7D 81% 5d14h │ MO 8.18k/25k 33%    Pro / Max / Team
… │ MO $85.10/$250 34%                                 Enterprise
```

| 字段 | 含义 |
|------|------|
| `5H` | 5 小时滚动窗口利用率（%）+ 重置倒计时 |
| `7D` | 7 天（每周）窗口利用率（%）+ 重置倒计时 |
| `MO` | 月度超额预算。**Enterprise** → 美元消耗 `$已用/$上限`；**Team / Max / Pro** → credits `已用/上限` |

自动识别两种订阅形态：

- **窗口 / credits 型**（Pro、Max、Team）：显示 5H + 7D 滚动窗口及重置倒计时；若启用超额用量，再显示月度 credits 预算。
- **Enterprise（按席位）**：滚动窗口不适用（API 返回 `null`），仅显示月度**美元**预算。

**数据来源。** OAuth 保护的 `GET /api/oauth/usage` 端点（`/usage` 命令本身调用的同一个）；当该 API 不可用时，5H/7D 窗口回落到状态行 stdin 的 `rate_limits` 字段。该端点限流极激进，故响应缓存到磁盘（`~/.claude/statusline/usage_cache.json`），最多每 5 分钟刷新一次——网络请求**绝不阻塞渲染**（4 秒超时，失败沿用旧值并按 60 秒退避重试）。OAuth token 读取顺序：`CLAUDE_CODE_OAUTH_TOKEN` → `~/.claude/.credentials.json` → macOS 钥匙串。

设 `CCS_USAGE_API=0` 可关闭全部网络请求（Pro/Max 的 stdin 5H/7D 窗口仍会显示）。

## 提供商余额 / 额度

当前模型属于**非 Anthropic 提供商**时（通常是代理模式），官方用量段会被 `BAL` 段替换，显示该提供商的剩余余额、额度或配额：

```
… │ BAL ¥110.00          DeepSeek（货币余额）
… │ BAL $4.90            OpenAI（剩余额度）
… │ BAL general 97%      MiniMax（配额 %，按健康度着色）
```

| 提供商 | 数据来源 | 凭证 |
|--------|----------|------|
| DeepSeek | `GET /user/balance`（官方） | `DEEPSEEK_API_KEY` |
| OpenAI | `GET /dashboard/billing/credit_grants`（未公开） | `OPENAI_SESSION_KEY`（浏览器会话 key） |
| MiniMax | `GET /v1/token_plan/remains`（配额） | `MINIMAX_API_KEY`，或自动从 `~/.minimaxi/credentials.json` / `mmx` CLI 状态发现 |
| Anthropic | 委托给上文「官方订阅用量」 | — |

提供商从模型 ID 解析，含**代理模式**——真实模型名通过 `ANTHROPIC_DEFAULT_*_MODEL_NAME` 还原。与官方用量一样，响应磁盘缓存以规避限流，且绝不阻塞渲染。

设 `CCS_BALANCE_API=0` 关闭全部余额网络请求；`CCS_BALANCE_TTL` 调整缓存刷新间隔（秒，最低 `180`，默认 `300`）。

## 快速开始

```bash
git clone https://github.com/stofancy/claude-code-statusline.git
cd claude-code-statusline
bash install.sh
```

安装脚本会在 `~/.claude/statusline/venv` 创建虚拟环境（Windows 使用 `Scripts/*.exe`，POSIX 使用 `bin/*`），并输出适配当前平台的 `settings.json` 配置片段。路径采用正斜杠 `~/` 格式，因为 Claude Code 通过 bash 调用 hook，Windows 反斜杠路径会被破坏。

本地克隆采用 **editable 模式**（`pip install -e`）安装，修改源码后无需重装，下次状态行刷新即自动生效。

或通过 pip 安装：

```bash
pip install git+https://github.com/stofancy/claude-code-statusline.git
```

然后将 hook + statusline 配置合并到 `~/.claude/settings.json`（参见 `examples/settings.json`）。重启 Claude Code。

## 环境要求

- Python 3.11+
- `pyyaml`（自动安装）
- SQLite3（标准库自带）

## 定价

内置定价表位于 `src/ccs/pricing.yaml`。可通过 `~/.claude/statusline/pricing.yaml` 覆盖。

所有价格均以 **CNY（¥）** 每百万 token 计。

| 提供商 | 模型 | 输入 | 缓存命中 | 缓存写入 | 输出 |
|----------|-------|-------|-----------|-------------|--------|
| DeepSeek | V4-Pro（2.5× 优惠） | 3.00 | 0.025 | — | 6.00 |
| DeepSeek | V4-Flash | 1.00 | 0.02 | — | 2.00 |
| DeepSeek | R1 | 4.00 | 0.25 | — | 16.00 |
| Anthropic | Opus 4.7 | 107.73 | 10.77 | 215.46 | 538.65 |
| Anthropic | Sonnet 4.6 | 21.55 | 2.15 | 43.09 | 107.73 |
| Anthropic | Haiku 4.5 | 7.18 | 0.72 | 14.37 | 35.91 |
| OpenAI | GPT-4o | 17.95 | 8.98 | — | 71.80 |
| OpenAI | GPT-4o-mini | 1.08 | 0.54 | — | 4.31 |
| OpenAI | GPT-5 | 8.98 | 1.80 | — | 71.80 |
| Google | Gemini 2.5 Pro | 8.98 | 0.89 | — | 35.91 |
| Google | Gemini 2.5 Flash | 1.08 | 0.11 | — | 4.31 |
| 智谱（GLM） | GLM-5.2 / 5.1 | 8.00 | 2.00 | 免费 | 28.00 |
| 智谱（GLM） | GLM-4.7 | 4.00 | 0.80 | 免费 | 16.00 |
| 智谱（GLM） | GLM-4.5-Air | 1.20 | 0.24 | 免费 | 8.00 |
| 智谱（GLM） | GLM-4.7-Flash | 免费 | 免费 | 免费 | 免费 |

DeepSeek V4-Pro 2.5× 折扣有效期至北京时间 2026/05/31 23:59。到期后请更新 `pricing.yaml`。

GLM 标价按输入长度分档，上表记录 `≥32k` 档。GLM 全系缓存写入促销免费，GLM-4.7-Flash 完全免费。精确的各档价格见 `pricing.yaml` 的 `zhipu` 块。

如果你使用基于 USD 的提供商，可将其价格转换为人民币并通过用户定价文件覆盖，或设置 `default_currency: USD` 并相应更新各提供商的价格。

## 架构

```
                        ┌─ Stop hook ───────────────────┐
                        ├─ PostToolUse ─── ccs-tracker ──┤
                        ├─ SubagentStart ── (hook events) ├── SQLite
                        └─ SubagentStop ─────────────────┘  (tool calls, subagent events)

Claude Code statusline tick (every 15s) → ccs-statusline
  ├── stdin JSON ─── model, cost, context_window, transcript_path
  ├── JSONL transcript ─── parse message.usage entries → dedup → per-model aggregation
  ├── subagents/agent-*.jsonl ─── aggregate subagent token usage
  ├── /api/oauth/usage ─── official subscription usage (disk-cached, 5min TTL)
  ├── provider balance API ─── DeepSeek / OpenAI / MiniMax balance (disk-cached)
  ├── SQLite ─── write parsed values to sessions + model_usage tables
  └── stdout ─── 2-line ANSI text
```

### 模块

| 文件 | CLI 入口 | 用途 |
|------|-----------|---------|
| `statusline.py` | `ccs-statusline` | 编排：stdin → transcript → DB → 渲染 |
| `tracker.py` | `ccs-tracker` | Hook 事件收集器 → 持久化到 SQLite |
| `db.py` | — | SQLite 模式（4 张表）、直接写入、CRUD、清理 |
| `cost.py` | — | 多提供商定价、按模型成本加总、模型 ID 解析 |
| `transcript.py` | — | JSONL 解析、去重、token 统计、子代理聚合、重播估算 |
| `usage.py` | — | 官方 `/api/oauth/usage` 客户端：token 发现、磁盘缓存请求、两种订阅形态归一化 |
| `balance.py` | — | 多提供商余额/额度/配额客户端（DeepSeek、OpenAI、MiniMax），含代理模式模型解析 |
| `i18n.py` | — | 渲染标签的极简本地化层（`CCS_LANG`、`locales/*.yaml`） |
| `renderer.py` | — | 256 色条、token 格式化、双行布局 |
| `util.py` | — | 共享 stdin JSON 读取器 |
| `pricing.yaml` | — | 可配置的提供商定价表 |

### Token 统计原理

Token 统计数据**直接从 Claude Code 的 JSONL transcript 读取**——这是每次 API 调用的权威记录。这避免了 snapshot-diff 累积的三个问题：

1. **压缩振荡**：`total_input_tokens` 在压缩后重置，导致上下文回填期间每次 tick 都产生虚假的"新调用"。JSONL 条目是不可变的——压缩会插入 `compact_boundary` 标记，但不会删除历史条目。
2. **多调用合并**：15 秒 tick 间隔内的多次 API 调用被合并为一次累积。JSONL 则记录每次调用的独立数据。
3. **cache_read 重复计数**：公式 `per_call_total_input = input + cache_read` 会夸大输入。JSONL 将 `input_tokens`（非缓存）与 `cache_read_input_tokens`（缓存命中）分开记录。

**去重策略**：`(input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)` 四元组完全相同的连续 usage 条目视为流式重复——每个组中只保留最后一条。用户消息会打断连续链，防止跨轮次误去重。

### 子代理聚合方式

Claude Code 将子代理的 API 调用存储在 `subagents/agent-*.jsonl` 文件中（位于主 transcript 所在的同一会话目录）。这些文件使用相同的 JSONL 格式，并以相同方式解析。

子代理通常使用不同的模型（例如子代理使用 `deepseek-v4-flash`，主会话使用 `deepseek-v4-pro`）。每个模型的 token 被独立追踪并按各自费率定价——混合模型会话产生准确的累计成本。

## 配置

### Hook 事件

| Hook | 用途 |
|------|---------|
| `Stop` | 轮次边界标记 |
| `PostToolUse` | 统计工具调用次数 |
| `PostToolUseFailure` | 统计失败的工具调用次数 |
| `SubagentStart` | 追踪子代理启动 |
| `SubagentStop` | 追踪子代理完成 |

### 环境变量

| 变量 | 用途 |
|----------|---------|
| `CCS_DEBUG=1` | 将原始 hook 数据写入 `~/.claude/statusline/debug.log` |
| `CCS_USAGE_API=0` | 关闭官方 `/api/oauth/usage` 查询（Pro/Max 的 stdin 5H/7D 窗口仍显示） |
| `CCS_USAGE_TTL` | 官方用量缓存刷新间隔（秒，最低 `180`，默认 `300`） |
| `CLAUDE_CODE_OAUTH_TOKEN` | 显式提供 OAuth token；否则自动从凭证文件 / 钥匙串读取 |
| `CCS_BALANCE_API=0` | 关闭提供商余额/额度/配额查询 |
| `CCS_BALANCE_TTL` | 提供商余额缓存刷新间隔（秒，最低 `180`，默认 `300`） |
| `DEEPSEEK_API_KEY` / `OPENAI_SESSION_KEY` / `MINIMAX_API_KEY` | `BAL` 余额段的提供商凭证 |
| `CCS_LANG` | 界面语言——`en`（默认）或 `zh`；未设时回落到 `$LANG`/`$LC_ALL` |
| `CCS_CURRENCY` | 覆盖显示币种（通过 `pricing.yaml` 的 `fx_rates` 换算） |

## 数据库

位置：`~/.claude/statusline/usage.db`（SQLite，WAL 模式）。

| 表 | 内容 |
|-------|----------|
| `sessions` | 会话元数据、累计 token 计数器、轮次计数、工具/子代理计数器 |
| `model_usage` | 按 `(session_id, model_id)` 细分的 token 数据，用于精确成本计算 |
| `tool_calls` | 每轮工具调用记录 |
| `subagent_events` | 子代理启动/停止事件 |

超过 30 天未更新的会话会自动清理。删除 `usage.db` 可重置所有累计数据。

## 故障排除

| 现象 | 解决方法 |
|---------|-----|
| 状态行未显示 | 检查 `~/.claude/settings.json` 中的 `statusLine` 配置，重启 Claude Code |
| 成本显示 `-` | 检查 `pricing.yaml` 是否存在；验证模型 ID 是否匹配定价条目 |
| NEXT 显示 `-` | 正常——会话早期尚无 transcript；首次响应后即会显示 |
| CACHE 异常高/低 | 缓存命中率 = `cache_read / (input + cache_read)`；>90% 对 DeepSeek 属于正常 |
| TURNS 显示 `cN` | 压缩次数——正常，表示上下文被压缩的次数 |
| 重启后数字异常 | 删除 `usage.db` 重置累计计数器 |

## 兼容性

- Linux（x86_64、aarch64）
- macOS（Apple Silicon、Intel）
- Windows（WSL2）
- 支持 ANSI 的 Windows Terminal

## 详细文档

详见专用文档（中英双语，互相引用）：

- **English**: [Architecture](docs/en/architecture.md) · [Installation](docs/en/installation.md) · [Configuration](docs/en/configuration.md)
- **中文**: [架构](docs/zh/architecture.md) · [安装](docs/zh/installation.md) · [配置](docs/zh/configuration.md)

## 许可证

MIT
