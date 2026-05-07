# 安装指南

> [English](../en/installation.md)

## 系统要求

- **Python 3.11+**
- **pyyaml**（通过 pip 自动安装）
- **SQLite3**（Python 标准库，无需额外安装）
- **支持 ANSI 的终端**：Linux、macOS、Windows Terminal，或任何支持 ANSI 转义序列的终端

## 快速安装

```bash
git clone https://github.com/stofancy/claude-code-statusline.git
cd claude-code-statusline
bash install.sh
```

安装脚本将：
1. 检测 Python 3.11+
2. 在 `~/.claude/statusline/venv` 创建虚拟环境
3. 通过 pip 安装包
4. 输出需要合并到 `~/.claude/settings.json` 的 JSON 配置

## pip 安装

```bash
pip install git+https://github.com/stofancy/claude-code-statusline.git
```

或通过 PyPI（如果已发布）：
```bash
pip install claude-code-statusline
```

## 安装后配置

安装完成后，将 hook 和 statusline 配置合并到 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event stop"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event tool"
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event tool"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event subagent-start"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event subagent-stop"
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline/venv/bin/ccs-statusline",
    "padding": 2,
    "refreshInterval": 15
  }
}
```

合并配置后，重启 Claude Code（或启动新会话）。

## 验证安装

启动 Claude Code 会话后，状态行应在 15 秒内出现。初始显示为：

```
ccs: waiting for session data...
```

对话开始后，双行显示将激活。

## 示例配置

完整参考配置请参见 [`examples/settings.json`](../../examples/settings.json)，其中包含多提供商设置（DeepSeek、Anthropic、OpenAI、Gemini）的环境变量配置。

## 平台支持

| 平台 | 状态 |
|------|------|
| Linux（x86_64、aarch64） | 完全支持 |
| macOS（Apple Silicon、Intel） | 完全支持 |
| Windows（WSL2） | 完全支持 |
| Windows Terminal | 支持（需要 ANSI 支持） |
| 原生 Windows（无 WSL） | 未经测试 |
