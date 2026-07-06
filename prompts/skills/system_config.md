# 系统配置规则

## 能力边界速查表（必读）

以下为各系统操作与正确工具的映射。**对号入座，禁止用错工具。**

### 音频域 — 必须用 system_audio（Windows 无原生 CLI）
| 操作 | 工具调用 | 说明 |
|------|----------|------|
| 设置播放音量 | `system_audio(operation="set_volume", value=N)` | N 为 0-100 |
| 静音 | `system_audio(operation="mute")` | |
| 取消静音 | `system_audio(operation="unmute")` | |
| 设置麦克风音量 | `system_audio(operation="set_mic_volume", value=N)` | N 为 0-100 |
| 列出音频设备 | `system_audio(operation="list_devices")` | 返回设备名/ID/状态 |
| 切换默认设备 | `system_audio(operation="set_default_device", device="关键词")` | 部分系统需手动 |

> **严禁对以上操作使用 execute_command 或 run_python。** system_audio 是唯一正确入口。

### 注册表域 — 必须用 execute_command
| 操作 | 路径/键 | 值 |
|------|---------|-----|
| 桌面图标显隐 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel` → `{guid}` | 0=显示, 1=隐藏 |
| 任务栏显秒 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced` → `ShowSecondsInSystemClock` | 1 |
| 深色/浅色模式 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize` → `AppsUseLightTheme`/`SystemUsesLightTheme` | 0=深色, 1=浅色 |
| 时间格式 | `HKCU\Control Panel\International` → `sShortTime`/`sTimeFormat`/`sShortDate`/`sLongDate` | 字符串 |
| 壁纸 | `HKCU\Control Panel\Desktop` → `WallPaper` | 图片路径 |
| 输入法 | `HKCU\Software\Microsoft\Input\...` | 按需查询 |

> 先用 `reg query` 验证，再用 `reg add` 修改。low_risk_registry_patterns 列表内的操作免确认。

### 网络域
| 操作 | 工具 |
|------|------|
| WiFi 开关/连接/列表 | `execute_command(netsh ...)` |
| 蓝牙 | 引导用户手动操作（无标准 CLI，bleak 不稳定） |

### 其他
| 操作 | 工具 |
|------|------|
| 截图 | `take_screenshot` |
| 软件安装/卸载 | `execute_command(winget ...)` |
| 软件下载 | `web_search` → `web_download` |

## 操作原则
1. 收到任务后先对照能力边界表确定正确工具，一步到位
2. 有专用工具 → 专用工具；有 CLI → execute_command；两者都无 → 告知用户
3. 注册表修改用 reg query 验证再 reg add；不主动重启 explorer
4. 同一工具对同一子目标失败 2 次即停止，不再重试，告知用户
