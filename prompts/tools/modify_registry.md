## modify_registry

安全地执行 Windows 注册表查询或修改。

**重要安全约束：**
- 仅允许操作 **HKEY_CURRENT_USER (HKCU)** 根键
- 仅允许以下路径前缀：
  - Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced（任务栏/时钟/右键菜单等）
  - Software\Microsoft\Windows\CurrentVersion\Explorer（资源管理器常规）
  - Control Panel\Desktop（桌面外观/视觉效果）
  - Software\Microsoft\Windows\CurrentVersion\Themes\Personalize（个性化主题）
  - Software\Microsoft\Windows\CurrentVersion\Explorer\Accent（强调色）
- 仅允许 **query**（查询）和 **add**（新增/修改）操作
- add 操作仅允许 REG_DWORD、REG_SZ、REG_QWORD 类型
- 禁止修改 DisableTaskMgr、NoRun、NoClose 等安全策略值名
- 严禁 delete / import / export / save / restore 等危险操作

**参数：**
- `command` (必填): 完整的 reg 命令字符串

**调用示例：**

查询当前时钟是否显示秒：
```
modify_registry(command='reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowSecondsInSystemClock')
```

开启任务栏时钟显示秒：
```
modify_registry(command='reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowSecondsInSystemClock /t REG_DWORD /d 1 /f')
```

关闭任务栏时钟显示秒：
```
modify_registry(command='reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowSecondsInSystemClock /t REG_DWORD /d 0 /f')
```

**注意：** 修改注册表后通常需要**重启 explorer.exe** 或**注销重新登录**才能生效。如果用户要求立即生效，使用 execute_command 执行 `taskkill /f /im explorer.exe && start explorer.exe`。
