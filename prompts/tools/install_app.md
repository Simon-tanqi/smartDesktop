## install_app

使用 Windows 包管理器或安装程序进行软件安装与卸载。

**参数：**
- `command` (必填): 完整的安装/卸载命令字符串

**允许的命令前缀：**

| 命令 | 用途 | 是否需要确认 |
|------|------|-------------|
| `winget install` | 安装软件 | 否（直接执行） |
| `winget search` | 搜索软件 | 否（直接执行） |
| `winget list` | 列出已安装 | 否（直接执行） |
| `winget upgrade` | 升级软件 | 否（直接执行） |
| `msiexec /i` | 安装 MSI | 否（直接执行） |
| `msiexec /x` | 卸载软件 | **是（必须确认）** |
| `msiexec /uninstall` | 卸载软件 | **是（必须确认）** |

**卸载确认流程：**
当用户要求卸载软件时，你必须按以下步骤操作：
1. 先调用 `execute_command` 传入卸载命令（如 `msiexec /x {产品代码}`）
2. 工具返回 `confirmation_required` 状态
3. 你应当输出 `final_message` 向用户展示即将执行的命令并请求确认
4. 用户回复"确认"后，在新一轮对话中重新调用 `execute_command` 并附加 `--confirmed` 参数

**调用示例：**

搜索软件：
```
install_app(command='winget search "notepad++"')
```

安装软件：
```
install_app(command='winget install Notepad++.Notepad++ --silent')
```

卸载软件（会触发确认）：
```
install_app(command='msiexec /x "{product-code}"')
```

**注意：** winget 安装建议加 `--silent` 参数静默安装；若安装卡住可加 `--accept-package-agreements --accept-source-agreements`。
