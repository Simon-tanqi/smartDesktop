# Roadmap：规划中的能力

> 本文档记录规划中、将在后续版本实现的能力，包含数据源选型、实现要点与安全约束等设计思路。
> 能力落地后需在 `core/intent_router.py` 注册对应 L1 直连路由，并同步更新本文档。

## 背景（2026-08-20）

以下三个能力此前曾在 `core/intent_router.py` 的 `_try_l1` 中预留路由，当前版本已安全降级为 LLM 通用流程，规划在后续版本实现专属工具：`query_yesterday_weather`（昨日天气查询）、`set_system_volume`（系统音量设置）、`set_desktop_icons_visible`（桌面图标显隐）。
全项目 grep 核实：三个工具当前无实现（不在 `tools/system_tools.py`、不在 `prompts/tools.yaml`、不在 TOOL_REGISTRY），仅在 `tools/skill_fixation.py` 白名单中被引用。

为保障路由链路干净，已移除 L1 直连路由条目（2026-08-20）：
- 删除 `_try_l1` 中三处直连 return（exec_action 分支），相关问句走 LLM 流程；
- 删除专属正则 `_L1_YESTERDAY_WEATHER` / `_L1_VOLUME` / `_L1_DESKTOP_ICONS` / `_L1_DESKTOP_ICONS_SHOW`；
- 保留 `_parse_volume_level`（注释标注待复用）与 `_L1_WEATHER`（通用天气 degraded 分支仍有效）；
- `tools/skill_fixation.py` 的 `LOW_RISK_TOOL_WHITELIST` 仍含三个名字（L38-46），实现工具后一并处理或移除。

---

## 1. query_yesterday_weather — 昨日天气查询

**预期签名**：`query_yesterday_weather(latitude: float, longitude: float) -> str`（与旧路由参数逐字对齐：北京默认 39.9042, 116.4074）

**数据源选型建议**（按推荐优先级）：

| 数据源 | API | 免费额度 | 备注 |
|---|---|---|---|
| 和风天气 QWeather | `https://devapi.qweather.com/v7/historical-weather` | 免费版有限请求/日 | 国内稳定、中文友好、有历史天气接口 |
| 高德开放平台 | `https://restapi.amap.com/v3/weather/weatherInfo` | 每日 10 万次 | 需 key，历史数据需另查 |
| Open-Meteo | `https://archive-api.open-meteo.com/v1/archive` | 免费无 key | 海外源，需处理时区/中文 |

**推荐**：和风天气（国内访问稳定、接口语义与"昨日天气"匹配），备选 Open-Meteo（免 key，便于离线演示）。

**实现要点**：
- API Key 不硬编码，走 `config/settings.py` 环境变量（如 `QWEATHER_API_KEY`）；无 key 时返回降级文案而非报错。
- 参数化查询：`latitude/longitude` + `date=today-1`（昨日日期由服务端计算，注意时区用 Asia/Shanghai）。
- 输出格式：`昨日（YYYY-MM-DD）北京天气：晴，最高 32℃ / 最低 24℃，东南风 2 级。` 风格与现回复一致。

**缓存策略**：
- 按 `(lat, lon, date)` 三级 key 缓存，TTL 24h（历史天气数据不可变，可长期缓存）；
- 复用项目现有 SQLite 存储（`memory/stores/sqlite_store.py` 的 `_get_conn_simple`）或内存 LRU 均可，避免重复调 API；
- 失败熔断：连续 3 次失败后退避 10 分钟，避免演示时反复打接口。

**重挂 L1 直连**（恢复 `_try_l1` 分支）：
```python
if _L1_YESTERDAY_WEATHER.search(text) and not has_task_signal:
    return ("", False, "query_yesterday_weather", {"latitude": 39.9042, "longitude": 116.4074})
```
需同时恢复正则 `_L1_YESTERDAY_WEATHER`，并在 `prompts/tools.yaml` + `TOOL_REGISTRY` 注册工具。

---

## 2. set_system_volume — 设置系统音量

**预期签名**：`set_system_volume(level: int) -> str`（level 0-100）

**实现方案（Windows）**：直接复用现有 `system_audio` 工具，无需新写音频逻辑。

- 现有实现：`tools/system_tools.py:1709` `def system_audio(operation, value, device)`，基于 **pycaw + comtypes**：
  - `from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume`
  - `from ctypes import POINTER, cast`
  - `AudioUtilities.GetSpeakers()` → `IAudioEndpointVolume` → `SetMasterVolumeLevelScalar(value/100, None)`
  - 依赖：`pip install pycaw comtypes`（项目已按此模式运行，见 `prompts/tools.yaml:350` 的 `system_audio` 声明与 `system_tools.py` 的 execute_command 音频重定向 L311-355）。
- **推荐实现**：新工具内部直接调 `system_audio(operation="set_volume", value=level)`，保持单一音频入口（符合 `prompts/skills/system_config.md` "system_audio 是唯一正确入口" 的约束）。
- 返回值：`成功：已设置音量为 45%（0-100）` 或错误信息透传。

**重挂 L1 直连**（恢复 `_try_l1` 分支）：
```python
if _L1_VOLUME.search(text) and not has_task_signal:
    level = _parse_volume_level(text)   # 函数已保留，直接复用
    if level is not None:
        return ("", False, "set_system_volume", {"level": level})
```
需同时恢复正则 `_L1_VOLUME`，并在 `prompts/tools.yaml` + `TOOL_REGISTRY` 注册工具。

---

## 3. set_desktop_icons_visible — 显示/隐藏桌面图标

**预期签名**：`set_desktop_icons_visible(visible: bool) -> str`（旧路由仅拦截显示方向，传 `visible=True`）

**实现方案（Windows 注册表 + Shell 刷新）**：
- 注册表项：`HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced`，值名 `HideIcons`（REG_DWORD）：
  - `0` = 显示桌面图标；`1` = 隐藏桌面图标。
- 写注册表后必须通知 Shell 刷新，否则桌面不即时生效：
  - `SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)`（`from ctypes.wintypes import ...` / `shell32.SHChangeNotify`）；
  - 或重启 `explorer.exe`（不推荐，影响用户体验）。
- 备选 Shell API：`IFolderView` / `IShellFolder` 的 `SetItemCount`（复杂且 COM 易泄漏，优先注册表方案）。
- 权限：HKCU 下写注册表无需管理员权限，工具免确认（低风险）。
- 安全约束：**只写 HideIcons 一个值**，禁止触碰其他注册表项；实现时用 `winreg.OpenKey(..., KEY_SET_VALUE)` 精确操作，失败回滚并返回错误信息。
- 旧路由语义：仅处理"显示/放回桌面图标"方向；"隐藏/藏起来"不在路由范围（回落 LLM），实现时保持该语义。

**重挂 L1 直连**（恢复 `_try_l1` 分支）：
```python
if _L1_DESKTOP_ICONS.search(text) and not has_task_signal:
    _hide_only = bool(re.search(r"隐藏|藏起来|不显示", text)) and not bool(
        re.search(r"显示|放回|放到|拿出来|找回来", text))
    if not _hide_only and _L1_DESKTOP_ICONS_SHOW.search(text):
        return ("", False, "set_desktop_icons_visible", {"visible": True})
```
需同时恢复正则 `_L1_DESKTOP_ICONS` / `_L1_DESKTOP_ICONS_SHOW`，并在 `prompts/tools.yaml` + `TOOL_REGISTRY` 注册工具。

---

## 收尾清单（三个工具实现后统一执行）

1. 恢复 `core/intent_router.py` `_try_l1` 三处直连分支 + 四个正则（见上文各节代码）；
2. 在 `prompts/tools.yaml` 声明三个工具（参考 `system_audio` 的 tier/desc/params 格式）；
3. 在 `tools/` 实现并注册到 `TOOL_REGISTRY`（参考 `tools/system_tools.py` 现有工具写法）；
4. 处理 `tools/skill_fixation.py` `LOW_RISK_TOOL_WHITELIST`：确认三个工具真实注册后再保留，否则移除；
5. 运行验证：`python -m py_compile core/intent_router.py` + 相关路由用例，基线 34 passed / 2 failed 不得出现新失败；
6. 删除本文件对应条目（或标记已完成）。
