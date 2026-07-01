"""搜索工具：文件搜索 + 网页搜索"""
import os
import re
import json
import asyncio

import requests

from . import register_tool, path_params
from config.config_loader import get_safety_config

# Playwright 懒加载 —— 仅在 render_js=True 时导入
_playwright_available = None  # None=未检测


def _get_playwright():
    """获取 Playwright 实例，优先使用系统 Chrome/Edge"""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    
    try:
        from playwright.sync_api import sync_playwright
        _playwright_available = sync_playwright
    except ImportError:
        _playwright_available = False
    return _playwright_available


def _find_browser_executable():
    """查找系统已安装的 Chrome 或 Edge"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _render_with_playwright(url: str, timeout: int = 20) -> dict:
    """使用 Playwright 渲染 JS 页面，返回 {text, links, title, url}"""
    pw = _get_playwright()
    if not pw:
        return {"error": "Playwright 不可用（未安装或浏览器未就绪）"}
    
    browser_path = _find_browser_executable()
    if not browser_path:
        return {"error": "未找到系统 Chrome/Edge 浏览器"}
    
    try:
        with pw() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=browser_path,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
            
            # 等待额外 2 秒以确保动态内容加载
            page.wait_for_timeout(2000)
            
            title = page.title()
            text = page.inner_text("body")
            
            # 提取所有链接
            links = page.evaluate("""() => {
                const allLinks = Array.from(document.querySelectorAll('a[href]'));
                return allLinks.map(a => ({
                    text: (a.innerText || '').trim().substring(0, 80),
                    href: a.href
                }));
            }""")
            
            # 筛选下载链接（.exe/.msi/.zip/.dmg/.apk）
            download_exts = {'.exe', '.msi', '.zip', '.dmg', '.apk', '.7z', '.rar', '.tar.gz'}
            download_links = [
                link for link in links
                if any(link.get("href", "").lower().endswith(ext) for ext in download_exts)
            ]
            
            browser.close()
            
            result = {
                "title": title or url,
                "url": page.url,
                "text": text[:5000],
                "links_count": len(links),
            }
            if download_links:
                result["download_links"] = download_links[:10]
            
            return result
    except Exception as e:
        error_msg = str(e)[:200]
        if "Target page, context or browser has been closed" in error_msg:
            return {"error": "页面渲染超时或浏览器崩溃"}
        return {"error": f"渲染失败: {error_msg}"}

# LLM 提取回退：仅在正则解析失败时按需导入
_LLM_EXTRACTION_AVAILABLE = None  # None=未检测, True/False=缓存结果


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
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in get_safety_config().search_skip_dirs]
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
_MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

# JS 渲染页面的典型空壳特征
_JS_SHELL_PATTERNS = [
    r'<div\s+id="root"></div>',
    r'<div\s+id="app"></div>',
    r'正在加载',
    r'loading',
    r'请启用\s*JavaScript',
    r'Please enable JavaScript',
    r'开启JavaScript',
]


def _strip_html(html: str) -> str:
    """去除 HTML 标签，提取纯文本"""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _check_llm_available():
    """检测本地 Ollama 是否可用（结果缓存）。"""
    global _LLM_EXTRACTION_AVAILABLE
    if _LLM_EXTRACTION_AVAILABLE is not None:
        return _LLM_EXTRACTION_AVAILABLE
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        _LLM_EXTRACTION_AVAILABLE = r.status_code == 200
    except Exception:
        _LLM_EXTRACTION_AVAILABLE = False
    return _LLM_EXTRACTION_AVAILABLE


def _llm_extract_results(html: str, max_results: int):
    """
    将 Bing 搜索结果页的 HTML 发给本地 LLM，让 LLM 自行判断结构并提取结果。
    只返回 body 区域的 clean text 给 LLM，避免 token 浪费。
    """
    # 提取 body 并去掉大块噪音标签
    body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else html
    for tag in ["script", "style", "noscript", "svg", "iframe"]:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)
    # 限制长度，避免超长 HTML 撑爆 token
    body = body[:15000]

    prompt = f"""下面是一段 Bing 搜索引擎的结果页 HTML 片段。请从中提取搜索结果，返回 JSON 数组。

要求：
1. 每个结果包含 title（标题纯文本）、url（完整链接）、snippet（摘要纯文本，最多 300 字）
2. 最多提取 {max_results} 条
3. 只返回 JSON 数组，不要任何解释

HTML 片段：
{body}

JSON 输出："""

    try:
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "deepseek-r1:7b", "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1}},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        # 提取 JSON 数组
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict) and r.get("title")]
    except Exception:
        pass

    return None


@register_tool
def web_search(query: str, max_results: int = 10):
    """使用 Bing 搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。"""
    try:
        url = "https://www.bing.com/search"
        resp = requests.get(url, params={"q": query}, headers={"User-Agent": _USER_AGENT}, timeout=10)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return json.dumps({"success": False, "error": f"搜索请求失败: {str(e)}"}, ensure_ascii=False)

    results = []
    html_raw = html  # 保留原始 HTML 供 LLM 回退使用

    # 1. 正则解析 b_algo 结果块
    blocks = re.split(r'<li class="b_algo"', html)[1:]
    for block in blocks[:max_results]:
        title_m = re.search(r'<h2[^>]*>.*?<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_m = re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.DOTALL)

        if title_m:
            title = _strip_html(title_m.group(2)).strip()
            link = title_m.group(1)
            snippet = _strip_html(snippet_m.group(1))[:300] if snippet_m else ""
            results.append({"title": title, "url": link, "snippet": snippet})

    # 2. 正则解析失败 → 尝试 LLM 提取（让 LLM 自行判断 HTML 结构）
    if not results and _check_llm_available():
        llm_results = _llm_extract_results(html_raw, max_results)
        if llm_results:
            results = llm_results

    if not results:
        return json.dumps({"success": True, "results": [], "message": "未找到相关结果"}, ensure_ascii=False)

    return json.dumps({"success": True, "results": results}, ensure_ascii=False)


@register_tool
def fetch_webpage(url: str, max_length: int = 3000, render_js: bool = False):
    """抓取指定 URL 的网页正文（去除 HTML 标签）。适合在搜索后深入阅读某个页面。
    自动尝试桌面 UA → 移动端 UA → 移动端子域名(m.) 三级回退。
    若 URL 指向可下载文件（PDF/图片/音视频等），自动路由到 web_download 工具。
    
    render_js=True 时使用系统 Chrome/Edge 无头浏览器渲染 JS 页面，
    可正确处理 SPA/Vue/React 等动态站点，同时自动提取页面中的下载链接。"""
    
    # ── JS 渲染模式 ──
    if render_js:
        rendered = _render_with_playwright(url)
        if "error" in rendered:
            # 渲染失败时回退到普通抓取
            pass
        else:
            result = {
                "success": True,
                "url": rendered["url"],
                "title": rendered["title"],
                "text": rendered["text"][:max_length],
                "_rendered_by": "playwright",
            }
            if rendered.get("download_links"):
                dl_desc = []
                for dl in rendered["download_links"][:5]:
                    dl_desc.append(f"- {dl['text'] or '(无文字)'} → {dl['href']}")
                result["text"] += "\n\n--- 页面中找到的下载链接 ---\n" + "\n".join(dl_desc)
                result["download_urls"] = [d["href"] for d in rendered["download_links"][:5]]
            if len(rendered.get("text", "")) > max_length:
                result["text"] = result["text"][:max_length] + "\n… [内容截断]"
            return json.dumps(result, ensure_ascii=False)
    raw = None
    resp_url = url
    errors = []

    for attempt in range(3):
        try:
            if attempt == 0:
                ua = _USER_AGENT
                target_url = url
            elif attempt == 1:
                ua = _MOBILE_UA
                target_url = url
            else:
                # 尝试 m. 子域名
                ua = _MOBILE_UA
                target_url = re.sub(r'https?://(www\.)?', 'https://m.', url, count=1)
            resp = requests.get(target_url, headers={"User-Agent": ua}, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() != "utf-8":
                try:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                except Exception:
                    pass
            raw = resp.text
            resp_url = resp.url
            break
        except Exception as e:
            errors.append(f"[{attempt}] {str(e)[:80]}")
            continue

    if raw is None:
        return json.dumps({"success": False, "error": f"网页抓取失败: {'; '.join(errors)}"}, ensure_ascii=False)

    # ── Content-Type 检测：非文本类型自动路由到下载 ──
    content_type = ""
    # 从首次成功响应中获取 Content-Type（raw 不为空时已有 response 信息，用近似检测）
    # 通过解析 HTML <meta> 或直接匹配魔数判断
    if raw:
        # 若返回内容并非 HTML（如直接是二进制数据被误解析），检测文件魔术字
        if raw.startswith("%PDF") or raw.startswith("PK\x03\x04"):
            from .web_download import web_download
            return web_download(url)

    # 对常见文件扩展名做路由
    parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(resp_url)
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in get_safety_config().downloadable_exts):
        from .web_download import web_download
        result = web_download(url)
        data = json.loads(result)
        if data.get("success"):
            data["_routed_from"] = "fetch_webpage（自动检测到可下载文件）"
            return json.dumps(data, ensure_ascii=False)
        # 下载失败时继续尝试文本抓取

    body_m = re.search(r"<body[^>]*>(.*?)</body>", raw, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else raw

    for tag in ["nav", "footer", "header", "aside", "noscript", "iframe", "script", "style"]:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)

    text = _strip_html(body)

    # 检测 JS 空壳
    is_js_shell = False
    if len(text.strip()) < 200:
        for pattern in _JS_SHELL_PATTERNS:
            if re.search(pattern, raw, re.IGNORECASE):
                is_js_shell = True
                break

    if is_js_shell:
        text += ("\n\n⚠ [检测到 JS 渲染页面，正文可能不完整。建议搜索该文章的转载(知乎/CSDN/博客园)，"
                 "或使用移动端 URL 重试。]")

    if len(text) > max_length:
        text = text[:max_length] + f"\n… [内容截断，原文约 {len(text)} 字符]"

    title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
    title = _strip_html(title_m.group(1)) if title_m else resp_url

    return json.dumps({"success": True, "url": resp_url, "title": title, "text": text}, ensure_ascii=False)


@register_tool
def fetch_multiple(urls: list, max_length: int = 2000):
    """批量抓取多个 URL，返回合并结果。适合同时阅读多篇搜索结果。urls 为 URL 字符串列表，建议 3-5 个。"""
    results = []
    for url in urls[:5]:
        try:
            resp = requests.get(url, headers={"User-Agent": _MOBILE_UA}, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            raw = resp.text
            body_m = re.search(r"<body[^>]*>(.*?)</body>", raw, re.DOTALL | re.IGNORECASE)
            body = body_m.group(1) if body_m else raw
            for tag in ["nav", "footer", "header", "aside", "noscript", "iframe", "script", "style"]:
                body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)
            text = _strip_html(body)
            title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
            title = _strip_html(title_m.group(1)) if title_m else url
            if len(text) > max_length:
                text = text[:max_length] + "…[截断]"
            results.append(f"---\n来源: {title}\n{text[:max_length]}")
        except Exception as e:
            results.append(f"---\n来源: {url}\n[抓取失败: {str(e)[:100]}]")
    return json.dumps({"success": True, "combined": "\n\n".join(results)}, ensure_ascii=False)


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

        # 正则解析失败 → LLM 回退
        if not results and _check_llm_available():
            llm_results = _llm_extract_results(html, 5)
            if llm_results:
                results = [{"title": r["title"], "snippet": r["snippet"]} for r in llm_results]

        return json.dumps({
            "success": True,
            "weather": {"city": city, "source": "Bing搜索摘要", "search_results": results}
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": f"天气查询失败(wttr.in超时且搜索回退失败): {str(e)}"}, ensure_ascii=False)
