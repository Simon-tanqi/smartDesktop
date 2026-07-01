# 系统配置规则

## 注册表
- 优先用训练知识直接给出路径+键名，`reg query` 验证 → `modify_registry` 修改。不主动重启 explorer
- 本地搜不到再 `web_search`，仍无果则告知用户不盲写
- 常见路径：任务栏显秒 `HKCU\...Explorer\Advanced` → `ShowSecondsInSystemClock`；时间格式 `HKCU\Control Panel\International` → `sShortTime`/`sTimeFormat`/`sShortDate`/`sLongDate`；壁纸 `HKCU\Control Panel\Desktop` → `WallPaper`；深色模式 `HKCU\...Themes\Personalize` → `AppsUseLightTheme`/`SystemUsesLightTheme`

## 软件安装/卸载
- 下载：`fetch_webpage(render_js=True)` 获取直链 → `web_download`；SPA 页面自动提取 .exe/.msi
- 备选：`winget search` → `winget install`；卸载：`winget uninstall` 或 `msiexec /x`，需确认

## 音频（音量/麦克风）
- 无标准 cmdlet，必须 `run_python` + `pycaw`（已安装）。关键：需同时设置 Console/Multimedia/Communications 三个 role 的音量（遍历 range(3)），否则设置页显示不一致
- `vol.GetMasterVolumeLevelScalar()` 获取当前音量(0~1)；`vol.SetMasterVolumeLevelScalar(v, None)` 设置

## 网络
- WiFi：`netsh interface set interface "WLAN" admin=enabled/disabled`（开关）; `netsh wlan connect`（连接）; `netsh wlan show networks`（列表）
- 蓝牙：无标准 CLI，`run_python` + `bleak` 或引导用户手动操作

## 操作原则
有标准 cmdlet → `execute_command`；无标准 cmdlet → `run_python`；UI 点击自动化不支持 → 走命令行/Python 路线
