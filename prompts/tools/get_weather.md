## get_weather
查询指定城市的实时天气和未来 3 天预报（使用 wttr.in 免费 API，无需 API Key）。

**参数**：
- `city`（可选，默认 "Shenzhen"）：城市名，支持中文（"北京"）、拼音（"Beijing"）、英文（"London"）

**返回信息**：当前温度、体感温度、湿度、天气描述、风向风速、能见度、紫外线指数、未来 3 天预报

返回 JSON：`{"success": true, "weather": {"city": "...", "temp_C": "28", "desc": "晴", ...}}`
