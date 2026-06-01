# Fix: Stop Hook JSON Validation Failed

## 问题

Claude Code 会话中反复出现 `Stop hook error: JSON validation failed`，导致 Stop hook 失败。

## 根因

`ccs-tracker --event stop` 在两个场景下静默退出（`sys.exit(0)`），stdout 无任何输出：

1. **stdin 无数据**（`read_stdin_json()` 返回 `None`）→ 第 104-105 行
2. **处理过程抛异常**（`handler(data)` 异常）→ 第 110-112 行

Claude Code harness 将 hook 的 stdout 解析为 JSON。空字符串不是合法 JSON → `JSONDecodeError: Expecting value` → harness 报 `JSON validation failed`。

## 修复

`src/ccs/tracker.py` 第 103-112 行，在两处 `sys.exit(0)` 之前输出 `{}`（合法空 JSON）：

```diff
     data = read_stdin_json()
     if not data:
+        print("{}")
         sys.exit(0)
 
     try:
         db.init_db()
         handler(data)
     except Exception as exc:
         print(f"ccs-tracker error ({event}): {exc}", file=sys.stderr)
+        print("{}")
         sys.exit(0)
```

## 验证

| 场景 | stdin | 改前 stdout | 改后 stdout |
|------|-------|------------|------------|
| 1 | 空 | (empty) → JSON parse error | `{}` → valid |
| 2 | 非法 JSON | (empty) → JSON parse error | `{}` → valid |
| 3 | 合法 JSON | 正常 | 不变 |
