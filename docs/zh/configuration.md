# 配置参考

> [English](../en/configuration.md)

## Hook 事件

Claude Code hooks 是追踪会话事件的机制。以下 hooks 必须在 `~/.claude/settings.json` 中注册：

| Hook | 事件类型 | 用途 |
|------|----------|------|
| `Stop` | `stop` | 轮次边界标记——确保数据库中存在会话记录 |
| `PostToolUse` | `tool` | 统计工具调用次数 |
| `PostToolUseFailure` | `tool` | 统计失败的工具调用次数（相同事件类型） |
| `SubagentStart` | `subagent-start` | 追踪子代理启动 |
| `SubagentStop` | `subagent-stop` | 追踪子代理完成 |

## 状态行配置

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline/venv/bin/ccs-statusline",
    "padding": 2,
    "refreshInterval": 15
  }
}
```

| 字段 | 说明 |
|------|------|
| `type` | 必须为 `"command"` |
| `command` | `ccs-statusline` 可执行文件路径（安装脚本自动检测） |
| `padding` | 状态行周围的垂直内边距。默认 `2` |
| `refreshInterval` | 更新间隔（秒）。默认 `15`。值越小 CPU 占用越高 |

## 定价覆盖

默认定价内置于 `src/ccs/pricing.yaml`。如需覆盖：

1. 创建 `~/.claude/statusline/pricing.yaml`
2. 复制内置文件的结构
3. 根据需要修改价格
4. 修改在下次 statusline/tracker 调用时生效（无需重启）

优先级：用户文件 → 内置文件 → 后备默认值。

所有价格默认以 **CNY（人民币元）每百万 tokens** 为单位。如需使用 USD：
```yaml
default_currency: USD
```

### 内置定价表

| 提供商 | 模型 | 输入 | 缓存命中 | 缓存写入 | 输出 |
|--------|------|------|-----------|----------|------|
| DeepSeek | V4-Pro（2.5 折优惠） | ¥3.00 | ¥0.025 | — | ¥6.00 |
| DeepSeek | V4-Flash | ¥1.00 | ¥0.02 | — | ¥2.00 |
| DeepSeek | R1 | ¥4.00 | ¥0.25 | — | ¥16.00 |
| Anthropic | Opus 4.7 | ¥107.73 | ¥10.77 | ¥215.46 | ¥538.65 |
| Anthropic | Sonnet 4.6 | ¥21.55 | ¥2.15 | ¥43.09 | ¥107.73 |
| Anthropic | Haiku 4.5 | ¥7.18 | ¥0.72 | ¥14.37 | ¥35.91 |
| OpenAI | GPT-4o | ¥17.95 | ¥8.98 | — | ¥71.80 |
| OpenAI | GPT-4o-mini | ¥1.08 | ¥0.54 | — | ¥4.31 |
| OpenAI | GPT-5 | ¥8.98 | ¥1.80 | — | ¥71.80 |
| Google | Gemini 2.5 Pro | ¥8.98 | ¥0.89 | — | ¥35.91 |
| Google | Gemini 2.5 Flash | ¥1.08 | ¥0.11 | — | ¥4.31 |

**注意**：DeepSeek V4-Pro 2.5 折优惠有效期至北京时间 2026/05/31 23:59。优惠结束后请更新 `pricing.yaml`。

## 环境变量

| 变量 | 用途 |
|------|------|
| `CCS_DEBUG=1` | 将原始 hook JSON 数据写入 `~/.claude/statusline/debug.log`，用于故障排查 |

## 多提供商设置

以 DeepSeek（或其他提供商）作为 API 后端，同时保持状态行追踪精确：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash"
  }
}
```

状态行会从 Claude Code 的会话 JSON 中检测模型，并根据你的定价表进行计价——无需额外配置。

## 数据库

- **位置**：`~/.claude/statusline/usage.db`
- **引擎**：SQLite（WAL 模式）
- **生命周期**：超过 30 天的旧会话自动清理
- **重置**：删除 `.db` 文件即可重置所有累积数据

### 表结构

| 表 | 内容 |
|----|------|
| `sessions` | 会话元数据、累积 token 计数器、轮次数、工具/子代理计数器 |
| `model_usage` | 按模型 `(session_id, model_id)` 的 token 分解，用于精确成本计算 |
| `tool_calls` | 每轮次的工具调用记录 |
| `subagent_events` | 子代理启动/停止事件 |
