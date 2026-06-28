# 系统配置规则

## 注册表修改流程（知识优先）

1. **直接用训练知识**：Windows 注册表路径是公开系统知识，agent 训练数据中已包含。接到任务后直接给出候选路径和键名，不要先搜。
2. `reg query <路径> /v <键名>` 验证键是否存在
3. 存在 → 读取当前值 → 修改 → 告知用户生效方式（不主动重启 explorer）
4. 不存在 → `reg query <父路径> /s /f <关键词>` 本地搜索
5. 本地也搜不到 → web_search 搜 "Windows 11 <功能> 注册表 路径"
6. 网上也搜不到 → 告知用户，不盲目写入

## 常见路径速查（兜底参考）

| 功能 | 路径 | 键名 |
|------|------|------|
| 任务栏显秒 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced` | `ShowSecondsInSystemClock` |
| 时间格式（短） | `HKCU\Control Panel\International` | `sShortTime` |
| 时间格式（长） | `HKCU\Control Panel\International` | `sTimeFormat` |
| 日期格式（短） | `HKCU\Control Panel\International` | `sShortDate` |
| 日期格式（长） | `HKCU\Control Panel\International` | `sLongDate` |
| 桌面壁纸 | `HKCU\Control Panel\Desktop` | `WallPaper` |
| 深色/浅色模式 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` | `AppsUseLightTheme` / `SystemUsesLightTheme` |
| 任务栏位置 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StuckRects3` | `Settings`（二进制，需特殊处理） |

## 软件安装卸载
- 安装优先走 winget，不确定包名先 winget search
- 卸载用 winget uninstall 或 msiexec /x，需用户确认
