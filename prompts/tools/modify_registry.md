## modify_registry

安全地查询或修改 Windows 注册表（HKCU 白名单路径）。

**白名单路径前缀：**
- Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced（任务栏/时钟/右键菜单）
- Software\Microsoft\Windows\CurrentVersion\Explorer（资源管理器）
- Control Panel\Desktop（桌面外观）
- Software\Microsoft\Windows\CurrentVersion\Themes\Personalize（个性化）

**操作限制：** 仅允许 eg query 和 eg add。禁止 delete / import / export。add 仅允许 REG_DWORD / REG_SZ / REG_QWORD。

**注意：** 禁止修改 DisableTaskMgr、NoRun 等安全策略值名。修改后通常需重启 explorer 或注销生效，告知用户即可，不要主动执行。
