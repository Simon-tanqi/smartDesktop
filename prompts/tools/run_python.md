## run_python

执行 Python 脚本。这是 execute_command 的兜底工具——仅在命令行无法胜任时才使用。

**何时使用**（严格按优先级判断）：
1. execute_command 已超时或连续失败 → 改用本工具
2. 数据处理、格式转换等需要编程逻辑的任务

**何时不使用**（优先用 execute_command）：
- 文件搜索/过滤 → 先用 execute_command 跑 PowerShell (Get-ChildItem -Recurse)
- 简单命令（dir、copy、move 等）
- 只需查看目录内容 → 用 scan_directory

**参数**：
- code: str（必填）完整的 Python 代码字符串，用 print() 输出结果
- timeout: int（可选）超时秒数，默认 300 秒

**使用示例**（仅在 execute_command 不可行后使用）：
```python
# 跨多个 CSV 做数据透视（execute_command 单条命令难以完成）
import pandas as pd
df = pd.concat([pd.read_csv(f) for f in ["a.csv", "b.csv"]])
print(df.groupby("category").sum())
```

**注意事项**：
- 代码中不要使用 input()
- 结果通过 print() 输出
- 大量输出时优先写入文件，再用 read_file 读取
