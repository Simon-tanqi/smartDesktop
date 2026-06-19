"""搜索工具：文件搜索 + 网页搜索"""
import os
import re
import json

import requests

from . import register_tool, path_params

SKIP_DIRS = {
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "$RECYCLE.BIN", "System Volume Information", "Recovery",
    "node_modules", "__pycache__", ".git", ".vscode", ".idea",
    "AppData", "Local Settings", "Application Data", "Temp"
}


@register_tool
@path_params("search_path")
def search_files(keyword: str, search_path: str = None, file_extensions: list = None, max_results: int = 5):
    """按文件名关键词搜索文件。search_path 可选，默认搜索用户目录及 D/E/F 盘。"""
    if not search_path:
        candidates = [
            os.path.expanduser("~"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "D:\\", "E:\\", "F:\\"
        ]
    else:
        candidates = [search_path]
    results = []
    for base in candidates:
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base, topdown=True, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS]
            depth = root[len(base):].count(os.sep)
            if depth > 3:
                continue
            for f in files:
                if keyword.lower() in f.lower():
                    if file_extensions:
                        if os.path.splitext(f)[1].lower() not in file_extensions:
                            continue
                    results.append(os.path.join(root, f))
                    if len(results) >= max_results:
                        return json.dumps(results, ensure_ascii=False)
    return json.dumps(results, ensure_ascii=False)


# ═══════════════════════════════════════════
#  网页浏览工具
# ═══════════════════════════════════════════

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def _strip_html(html: str) -> str:
    """去除 HTML 标签，提取纯文本"""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@register_tool
def web_search(query: str, max_results: int = 5):
    """使用 Bing 搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。"""
    try:
        url = "https://www.bing.com/search"
        resp = requests.get(url, params={"q": query}, headers={"User-Agent": _USER_AGENT}, timeout=10)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return json.dumps({"success": False, "error": f"搜索请求失败: {str(e)}"}, ensure_ascii=False)

    results = []
    # 解析 b_algo 结果块
    blocks = re.split(r'<li class="b_algo"', html)[1:]
    for block in blocks[:max_results]:
        # 标题 + 链接
        title_m = re.search(r'<h2[^>]*>.*?<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        # 摘要（b_caption 内的 p）
        snippet_m = re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.DOTALL)

        if title_m:
            title = _strip_html(title_m.group(2)).strip()
            link = title_m.group(1)
            snippet = _strip_html(snippet_m.group(1))[:300] if snippet_m else ""
            results.append({"title": title, "url": link, "snippet": snippet})

    if not results:
        return json.dumps({"success": True, "results": [], "message": "未找到相关结果"}, ensure_ascii=False)

    return json.dumps({"success": True, "results": results}, ensure_ascii=False)


@register_tool
def fetch_webpage(url: str, max_length: int = 3000):
    """抓取指定 URL 的网页正文（去除 HTML 标签）。适合在搜索后深入阅读某个页面。"""
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        if resp.encoding and resp.encoding.lower() != "utf-8":
            resp.encoding = resp.apparent_encoding or "utf-8"
        raw = resp.text
    except Exception as e:
        return json.dumps({"success": False, "error": f"网页抓取失败: {str(e)}"}, ensure_ascii=False)

    body_m = re.search(r"<body[^>]*>(.*?)</body>", raw, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else raw

    for tag in ["nav", "footer", "header", "aside", "noscript", "iframe"]:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)

    text = _strip_html(body)
    if len(text) > max_length:
        text = text[:max_length] + f"\n… [内容截断，原文约 {len(text)} 字符]"

    title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
    title = _strip_html(title_m.group(1)) if title_m else resp.url

    return json.dumps({"success": True, "url": resp.url, "title": title, "text": text}, ensure_ascii=False)


@register_tool
def get_weather(city: str = "Shenzhen"):
    """查询城市天气。优先使用 wttr.in API，失败时自动回退到必应搜索。city 支持中文名、拼音或英文。"""
    # 主路径：wttr.in
    try:
        url = f"https://wttr.in/{city}?format=j1&lang=zh"
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        weather = {
            "city": city,
            "source": "wttr.in",
            "temp_C": current["temp_C"],
            "feels_like": current["FeelsLikeC"],
            "humidity": current["humidity"],
            "desc": current["lang_zh"][0]["value"] if "lang_zh" in current else current["weatherDesc"][0]["value"],
            "wind": f"{current['winddir16Point']} {current['windspeedKmph']}km/h",
        }
        forecast = []
        for day in data["weather"][:3]:
            forecast.append({
                "date": day["date"],
                "max_C": day["maxtempC"],
                "min_C": day["mintempC"],
                "desc": day["hourly"][4]["lang_zh"][0]["value"] if "lang_zh" in day["hourly"][4] else day["hourly"][4]["weatherDesc"][0]["value"],
            })
        weather["forecast"] = forecast
        return json.dumps({"success": True, "weather": weather}, ensure_ascii=False)
    except Exception:
        pass  # 回退到搜索

    # 回退路径：必应搜索
    try:
        query = f"{city} 天气 明天" if "明天" not in str(city) else f"{city} 天气"
        url = "https://www.bing.com/search"
        resp = requests.get(url, params={"q": query}, headers={"User-Agent": _USER_AGENT}, timeout=10)
        resp.raise_for_status()
        html = resp.text

        # 尝试提取必应天气卡片（wtr_weather 模块）
        weather_card = re.search(
            r'<div[^>]*class="[^"]*wtr_currTemp[^"]*"[^>]*>(.*?)</div>.*?'
            r'<div[^>]*class="[^"]*wtr_currDesc[^"]*"[^>]*>(.*?)</div>',
            html, re.DOTALL
        )
        if weather_card:
            temp_text = _strip_html(weather_card.group(1)).strip()
            desc_text = _strip_html(weather_card.group(2)).strip()
            forecast_items = re.findall(
                r'<span[^>]*class="[^"]*wtr_day[^"]*"[^>]*>(.*?)</span>.*?'
                r'<div[^>]*class="[^"]*wtr_condiTemp[^"]*"[^>]*>(.*?)</div>',
                html, re.DOTALL
            )
            search_weather = {
                "city": city,
                "source": "Bing天气卡片",
                "temp": temp_text,
                "desc": desc_text,
                "forecast_raw": [
                    {"day": _strip_html(d).strip(), "temp": _strip_html(t).strip()}
                    for d, t in forecast_items[:3]
                ] if forecast_items else [],
            }
            return json.dumps({"success": True, "weather": search_weather}, ensure_ascii=False)

        # 无天气卡片时返回搜索摘要
        blocks = re.split(r'<li class="b_algo"', html)[1:6]
        results = []
        for block in blocks:
            title_m = re.search(r'<h2[^>]*>.*?<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
            snippet_m = re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.DOTALL)
            if title_m:
                results.append({
                    "title": _strip_html(title_m.group(2)).strip(),
                    "snippet": _strip_html(snippet_m.group(1))[:200] if snippet_m else "",
                })
        return json.dumps({
            "success": True,
            "weather": {"city": city, "source": "Bing搜索摘要", "search_results": results}
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": f"天气查询失败(wttr.in超时且搜索回退失败): {str(e)}"}, ensure_ascii=False)
