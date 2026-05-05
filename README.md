# Claude Code Statusline

专为 **DeepSeek 供应商** 优化的 Claude Code 生产级状态行。通过 ANSI 多行渲染展示 token 成本、会话指标、子代理活动、重放预估等关键数据。

## 功能

- **自定义成本计算**：基于实际供应商定价（DeepSeek/Anthropic/OpenAI/Gemini），不依赖 Claude Code 内置的 Anthropic 美元估值
- **重放 token 预估**：解析 JSONL 转录文件，估算下一轮 API 调用的重放 token 数，预防上下文膨胀
- **子代理追踪**：通过 SubagentStart/Stop 钩子实时追踪运行中的子代理
- **SQLite 持久化**：会话、轮次、工具调用、子代理事件的完整历史记录
- **ANSI 多行显示**：4 行彩色布局，展示模型、上下文占比、token 统计、缓存命中率、成本、时长、轮次等
- **优雅降级**：钩子缺失、转录损坏、数据库不可写等场景下不会阻塞 Claude Code

## 显示效果

```text
DeepSeek-V4-Pro   CTX 38%   NEXT 142k
IN 12.4M   OUT 1.8M   CACHE 34.2M (73%)
¥3.42   2h13m   48 turns
TOOLS 132   AGENTS 14 (3 running)
```

颜色规则：上下文 < 50%（绿色）、50-80%（黄色）、> 80%（红色）。

## 安装

```bash
git clone https://github.com/YOUR_ORG/claude-code-statusline.git
cd claude-code-statusline
bash install.sh
```

或通过 pip 直接安装：

```bash
pip install git+https://github.com/YOUR_ORG/claude-code-statusline.git
```

安装后将输出的 JSON 合并到 `~/.claude/settings.json`。

## 架构

```
Claude Code Stop hook ──→ ccs-tracker ──→ ~/.claude/statusline/usage.db
Claude Code PostToolUse ──→ ccs-tracker ──→ (同上)
SubagentStart/Stop     ──→ ccs-tracker ──→ (同上)

Claude Code statusline tick ──→ ccs-statusline
  ├── stdin ← session JSON（模型、token、成本等）
  ├── sqlite ← 查询累计统计数据
  └── stdout → 4 行 ANSI 文本
```

### 模块

| 文件 | 职责 |
|------|------|
| `statusline.py` | CLI 入口 `ccs-statusline`：读取 stdin JSON + sqlite 数据 → 渲染多行 ANSI |
| `tracker.py` | CLI 入口 `ccs-tracker`：接收钩子事件 JSON → 写入 sqlite |
| `db.py` | SQLite 持久层：schema 管理、会话/轮次/工具/子代理 CRUD、过期清理 |
| `cost.py` | 多供应商成本计算：加载 pricing.yaml、按模型匹配定价、计算分项和汇总成本 |
| `transcript.py` | JSONL 转录解析：统计轮次、估算重放 token 数、检测增长趋势 |
| `renderer.py` | ANSI 渲染引擎：颜色规则、数字格式化、4 行布局 |
| `pricing.yaml` | 可扩展定价表：DeepSeek/Anthropic/OpenAI/Gemini，价格单位 CNY |

## 定价配置

内置定价表位于 `src/ccs/pricing.yaml`，用户可通过 `~/.claude/statusline/pricing.yaml` 覆盖。

当前 DeepSeek V4-Pro 折扣价（2.5 折，至 2026/05/31）：

| 项目 | 价格（元 / 百万 tokens） |
|------|--------------------------|
| 输入（缓存未命中） | ¥3.00 |
| 输入（缓存命中） | ¥0.025 |
| 输出 | ¥6.00 |

修改定价表后下次状态行刷新自动生效，无需重启 Claude Code。

## 钩子配置

`~/.claude/settings.json` 中需要配置 4 个钩子 + 状态行：

| 钩子事件 | 用途 |
|----------|------|
| `Stop` | 记录轮次完成、更新轮次计数 |
| `PostToolUse` | 记录每次工具调用 |
| `SubagentStart` | 追踪子代理启动 |
| `SubagentStop` | 追踪子代理停止 |

完整配置示例见 `examples/settings.json`。

## 兼容性

- Linux (x86_64, aarch64)
- macOS (Apple Silicon, Intel)
- Windows (WSL2)
- Windows Terminal（需要 ANSI 支持）

## 数据库

位置：`~/.claude/statusline/usage.db`（WAL 模式 SQLite）

- 过期会话清理：超过 30 天无活动的会话自动标记并删除
- 可手动删除 `rm ~/.claude/statusline/usage.db` 重置所有数据

## 故障排查

| 问题 | 解决 |
|------|------|
| 状态行不刷新 | 检查 `statusLine` 配置是否正确，启动新会话 |
| 成本显示为 `-` | 确认 pricing.yaml 未损坏，`pip install pyyaml` |
| NEXT 显示为 `-` | 正常 — 会话初期无转录文件时显示 `-` |
| 工具/子代理计数不增 | 确认 `hooks` 配置正确，钩子未冲突 |
| DB 锁定 | 删除 `~/.claude/statusline/usage.db` 重建 |

## 许可

MIT
