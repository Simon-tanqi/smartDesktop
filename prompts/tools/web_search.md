## web_search
使用 Bing 搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。

**参数**：
- `query`（必填）：搜索关键词
- `max_results`（可选，默认 5）：返回结果数量，最大 10

**典型用法**：
- 搜索最新 AI 新闻：`web_search("2026年AI发展最新动态")`
- 查询实时信息：`web_search("Python 3.14 release date")`
- 获取技术文档：`web_search("Flask blueprint tutorial")`

返回 JSON：`{"success": true, "results": [{"title": "...", "url": "...", "snippet": "..."}]}`
