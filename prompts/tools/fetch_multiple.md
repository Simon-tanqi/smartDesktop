## fetch_multiple

批量抓取多个 URL 正文（去 HTML 标签），返回合并结果。适合搜索后同时深入阅读多篇结果。

何时用：web_search 拿到多个链接后需要同时获取多篇全文、对比阅读。
何时不用：只需抓取单个 URL → 用 fetch_webpage。

参数：
- urls: list[str]（必填）URL 字符串列表，建议 3-5 个
- max_length: int（可选）单篇最大字符数，默认 8000

返回合并后的多篇正文，用 `--- Page: {url} ---` 分隔。
