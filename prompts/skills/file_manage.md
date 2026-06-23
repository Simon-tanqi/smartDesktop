# 文件操作专用规则


## 操作流程

1. **先扫描**：用 scan_directory 确认目录内容
2. **再定位**：目标文件夹不明确时，用 resolve_folder 查找真实文件夹名（支持拼音/英文匹配）
3. **再操作**：源路径必须包含完整文件名（含扩展名），不得猜测

## 终端命令规范

- 可使用 execute_command 在终端执行 Windows 命令（dir / copy / move / del / mkdir 等）
- 路径必须使用绝对路径，分隔符用 \\ 或 / 均可，推荐用 \\
- 命令执行失败时，根据错误信息修正，不要原样重试

## 工具选择规则

文件搜索/过滤（包括递归遍历、按大小/日期/类型过滤、跨多盘搜索等）：
- **第一选择：execute_command**，使用 PowerShell（如 Get-ChildItem -Recurse | Where-Object）
- 不要在第一轮就假定命令行会失败——先用 execute_command
- 仅在 execute_command 实际超时或失败后，改用 run_python 重试；参数不同则允许，相同参数连续失败才停止

## 禁止行为

- 不得跳过扫描直接操作
- 不得仅凭名称猜测文件是否存在
