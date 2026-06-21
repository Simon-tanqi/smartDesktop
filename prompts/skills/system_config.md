# 系统配置技能

**域边界：本域为纯本地操作域。** 操作优先在本地完成，联网搜索仅作为本地验证失败后的最终兜底。

## 可用工具

| 工具 | 用途 |
|------|------|
| `modify_registry` | 安全修改注册表（仅 HKCU 白名单路径） |
| `execute_command` | 执行 winget/msiexec/taskkill 等系统命令 |
| `web_search` | **仅兜底**：本地 reg query 搜索不到键值时，搜索当前 Windows 版本的准确路径 |

## 注册表操作回路（强制）

每次修改注册表前必须走完回路，不得跳过步骤直接写入：

1. 凭知识给候选路径和键名
2. `reg query <路径> /v <键名>` 确认存在
3. 存在 → 修改；完成后告知生效方式，**不要**主动重启 explorer
4. 不存在 → `reg query <父路径> /s /f <关键词>` 本地搜索
5. 搜不到 → `web_search` 搜索"Windows 11 <功能> 注册表 路径"兜底
6. 网上也搜不到 → 告知用户，不做盲目写入

## 软件安装卸载规则

- 安装优先走 `execute_command` 调用 winget，不确定包名时先 `winget search`
- 卸载用 `winget uninstall` 或 `msiexec /x`，卸载操作需用户确认
- 修改注册表后生效方式（重启 explorer / 注销 / 重启）只需在回复中告知，**不要**主动执行

## 应用缓存清理规则

聊天记录数据库（微信 Msg 目录 / QQ Msg*.db / 钉钉 db 目录）受系统硬保护，命令层无法删除，可放心执行清理。

### 安全可删路径（白名单）

**微信（`%USERPROFILE%\Documents\WeChat Files\<微信号>\` 下）：**

| 目录 | 内容 | 删除影响 |
|------|------|----------|
| `FileStorage\Image` | 聊天图片缓存 | 图片需重新加载 |
| `FileStorage\Video` | 聊天视频缓存 | 视频需重新下载 |
| `FileStorage\File` | 聊天文件缓存 | 文件需重新下载 |
| `FileStorage\Temp` | 临时文件 | 无影响 |
| `log` | 运行日志 | 无影响 |
| `Crashpad` | 崩溃转储 | 无影响 |

**QQ（`%USERPROFILE%\Documents\Tencent Files\<QQ号>\` 下）：**

| 目录 | 内容 | 删除影响 |
|------|------|----------|
| `Image` | 聊天图片缓存 | 图片需重新加载 |
| `Video` | 聊天视频缓存 | 视频需重新下载 |
| `FileRecv` | 接收文件缓存 | 文件需重新下载 |
| `Group2` | 群文件缓存 | 群文件需重新下载 |
| `log` | 运行日志 | 无影响 |
| `Crashpad` | 崩溃转储 | 无影响 |

**钉钉（`%APPDATA%\DingTalk\<数字ID>\` 下）：**

| 目录 | 内容 | 删除影响 |
|------|------|----------|
| `dowload` | 下载的文件 | 文件需重新下载 |
| `image` | 聊天图片缓存 | 图片需重新加载 |
| `video` | 聊天视频缓存 | 视频需重新下载 |
| `log` | 运行日志 | 无影响 |

**浏览器缓存：**

| 浏览器 | 缓存路径 |
|---|---|
| Chrome | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cache\Cache_Data` |
| Edge | `%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cache\Cache_Data` |
| Firefox | `%APPDATA%\Mozilla\Firefox\Profiles\*\cache2` |

### 操作流程

1. **先扫描**：用 `scan_directory` 列出应用数据目录下的账号子目录
2. **再统计**：对每个账号的白名单目录用 `dir /s` 统计大小
3. **再展示**：汇总所有可清理项和总大小，逐项说明删除影响
4. **再确认**：等待用户确认后执行 `execute_command` 删除


## 软件安装自动化

安装策略：winget → 官网下载 → 告知用户手动（无静默参数）。

| 软件 | winget 包名 | 静默 | 备注 |
|------|------------|------|------|
| Python 3.11 | `Python.Python.3.11` | 是 | 安装后需手动配 PATH：`setx PATH "%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"`，再 `pip install requests numpy pandas` 等 |
| 7-Zip | `7zip.7zip` | 是 | 解压 rar/7z 前置依赖 |
| Chrome | `Google.Chrome` | 是 | 不自动设默认浏览器 |
| QQ | `Tencent.QQ` | 是 | |
| 飞书 | `ByteDance.Feishu` | 是 | |
| 微信 | `Tencent.WeChat` | **否** | 安装向导最后会勾选自启，需告知用户手动取消 |
| 网易云音乐 | 无 winget | **否** | 官网 https://music.163.com ，安装向导无法静默 |

pip 安装失败时：网络问题换清华源 `-i https://pypi.tuna.tsinghua.edu.cn/simple`，权限问题加 `--user`。


## 开机自启管理

自启注册表路径：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`、`HKLM\...\Run`、启动文件夹 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`。

操作：安装前 `reg query` 三路径导快照 → 安装后再次查询 diff → 按需 `reg delete <路径> /v <键名> /f` 删除多余项，同时清理启动文件夹快捷方式。

常用自启键名：微信 `WeChat`、QQ `QQ`/`Tencent QQ`、飞书 `Feishu`、网易云 `cloudmusic`。


## Chrome 配置

设为默认浏览器无法通过命令行完全自动化，需指引用户手动在「设置 → 应用 → 默认应用」中切换。

关闭推送通知（策略注册表，重启 Chrome 生效）：
```
reg add "HKCU\Software\Policies\Google\Chrome" /v DefaultNotificationsSetting /t REG_DWORD /d 2 /f
reg add "HKCU\Software\Policies\Google\Chrome" /v DisableBackgroundMode /t REG_DWORD /d 1 /f
```
如果路径不存在则先 `reg add "HKCU\Software\Policies\Google\Chrome" /f` 创建空键。


## 批量解压

- `.zip` 用 `Expand-Archive -Path <源> -DestinationPath <目标> -Force`
- `.rar`/`.7z` 需先 `winget install 7zip.7zip --silent`，然后用 `& "C:\Program Files\7-Zip\7z.exe" x "<源>" -o"<目标>" -y`
- 目标子目录取压缩包文件名（不含扩展名），如 `Python-3.11.zip` → `D:\SoftInstall\Python-3.11\`
- 解压前检查磁盘剩余空间；删除原压缩包必须确认
