## fetch_webpage
抓取指定 URL 的网页正文，自动去除 HTML 标签、导航栏、页脚等噪音。

**参数**：
- `url`（必填）：目标网页地址，必须以 http:// 或 https:// 开头
- `max_length`（可选，默认 3000）：返回文本的最大字符数，超出部分截断

**典型用法**：
- 搜索后深入阅读：先用 web_search 获取链接，再用 fetch_webpage 抓取内容
- 提取文章正文：`fetch_webpage("https://example.com/article")`

返回 JSON：`{"success": true, "url": "...", "title": "...", "text": "..."}`
