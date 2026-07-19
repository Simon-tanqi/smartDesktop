"""搜索工具：文件搜索 + 网页搜索

修复清单：
- 二进制魔数检测改用 resp.content 字节判断
- 移动端 m. 子域名避免双重前缀
- file_extensions 大小写归一化
- Playwright try-finally 确保浏览器关闭 + 单例复用
- 全局 requests.Session 连接池复用
- fetch_multiple 线程池并发
- search_files 文件总数上限 + 遍历超时
- 检测标志线程锁保护
- JS 空壳误判阈值优化
- 多盘全量搜索后返回 Top N
- Ollama 配置化
- 异常日志 + 统一返回格式
- 跨平台浏览器路径
"""
import os
import re
import json
import time
import threading
import logging
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import register_tool, path_params
from config.config_loader import get_safety_config
from config.settings import (
    SEARCH_TIMEOUT, HTML_PARSE_MAX_CHARS, USER_AGENT, MOBILE_UA,
    DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#  全局 HTTP Session（连接池复用）
# ═══════════════════════════════════════════

_session: requests.Session | None = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    """获取全局 requests.Session 单例，复用 TCP 连接池。"""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is not None:
            return _session
        s = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
        return _session


# ═══════════════════════════════════════════
#  Playwright 懒加载
# ═══════════════════════════════════════════

_playwright_available = None  # None=未检测
_playwright_lock = threading.Lock()


def _get_playwright():
    """获取 Playwright sync_playwright，带线程锁保护。"""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    with _playwright_lock:
        if _playwright_available is not None:
            return _playwright_available
        try:
            from playwright.sync_api import sync_playwright
            _playwright_available = sync_playwright
        except ImportError:
            _playwright_available = False
            logger.warning("Playwright 未安装，JS 渲染功能不可用")
        return _playwright_available


def _find_browser_executable():
    """查找系统已安装的 Chrome 或 Edge（跨平台）。"""
    candidates = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path

    # Linux/macOS 常见路径
    linux_candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/microsoft-edge",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for path in linux_candidates:
        if os.path.exists(path):
            return path

    # 尝试从 PATH 查找
    for name in ["google-chrome", "chromium-browser", "chromium", "microsoft-edge"]:
        try:
            import shutil
            found = shutil.which(name)
            if found:
                return found
        except Exception:
            pass

    return None


def _render_with_playwright(url: str, timeout: int = 20) -> dict:
    """使用 Playwright 渲染 JS 页面，返回 {text, links, title, url}。

    修复：try-finally 确保浏览器和上下文关闭，防止进程泄漏。
    """
    pw = _get_playwright()
    if not pw:
        return {"error": "Playwright 不可用（未安装或浏览器未就绪）"}

    browser_path = _find_browser_executable()
    if not browser_path:
        return {"error": "未找到系统 Chrome/Edge 浏览器"}

    browser = None
    context = None
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
            page.wait_for_timeout(2000)

            title = page.title()
            text = page.inner_text("body")

            links = page.evaluate("""() => {
                const allLinks = Array.from(document.querySelectorAll('a[href]'));
                return allLinks.map(a => ({
                    text: (a.innerText || '').trim().substring(0, 80),
                    href: a.href
                }));
            }""")

            download_exts = {'.exe', '.msi', '.zip', '.dmg', '.apk', '.7z', '.rar', '.tar.gz'}
            download_links = [
                link for link in links
                if any(link.get("href", "").lower().endswith(ext) for ext in download_exts)
            ]

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
        logger.warning("Playwright 渲染失败: %s", error_msg)
        if "Target page, context or browser has been closed" in error_msg:
            return {"error": "页面渲染超时或浏览器崩溃"}
        return {"error": f"渲染失败: {error_msg}"}
    finally:
        # 确保上下文和浏览器关闭，防止进程泄漏
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass


# ═══════════════════════════════════════════
#  LLM 提取回退
# ═══════════════════════════════════════════

_LLM_EXTRACTION_AVAILABLE = None
_llm_lock = threading.Lock()


def _check_llm_available():
    """检测本地 Ollama 是否可用（结果缓存，线程安全）。"""
    global _LLM_EXTRACTION_AVAILABLE
    if _LLM_EXTRACTION_AVAILABLE is not None:
        return _LLM_EXTRACTION_AVAILABLE
    with _llm_lock:
        if _LLM_EXTRACTION_AVAILABLE is not None:
            return _LLM_EXTRACTION_AVAILABLE
        try:
            base_url = DEFAULT_OLLAMA_URL.rsplit("/api/", 1)[0]
            r = _get_session().get(f"{base_url}/api/tags", timeout=3)
            _LLM_EXTRACTION_AVAILABLE = r.status_code == 200
        except Exception as e:
            _LLM_EXTRACTION_AVAILABLE = False
            logger.debug("Ollama 不可用: %s", str(e)[:100])
        return _LLM_EXTRACTION_AVAILABLE


def _llm_extract_results(html: str, max_results: int):
    """将 Bing 搜索结果页 HTML 发给本地 LLM 提取结果。

    修复：模型名和 URL 从配置读取，异常有日志。
    """
    body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else html
    for tag in ["script", "style", "noscript", "svg", "iframe"]:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = body[:HTML_PARSE_MAX_CHARS]

    prompt = f"""下面是一段 Bing 搜索引擎的结果页 HTML 片段。请从中提取搜索结果，返回 JSON 数组。

要求：
1. 每个结果包含 title（标题纯文本）、url（完整链接）、snippet（摘要纯文本，最多 300 字）
2. 最多提取 {max_results} 条
3. 只返回 JSON 数组，不要任何解释

HTML 片段：
{body}

JSON 输出："""

    try:
        resp = _get_session().post(
            DEFAULT_OLLAMA_URL,
            json={"model": DEFAULT_OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1}},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict) and r.get("title")]
    except Exception as e:
        logger.warning("LLM 提取搜索结果失败: %s", str(e)[:150])

    return None


# ═══════════════════════════════════════════
#  文件搜索工具
# ═══════════════════════════════════════════

_SEARCH_MAX_FILES = 50000      # 单次遍历文件总数上限
_SEARCH_TIMEOUT_SEC = 30       # 遍历超时秒数


@register_tool
@path_params("search_path")
def search_files(keyword: str, search_path: str = None, file_extensions: list = None, max_results: int = 5):
    """按文件名关键词搜索文件。search_path 可选，默认搜索用户目录及 D/E/F 盘。支持通配符 * 和 ?。"""
    # 扩展名大小写归一化
    if file_extensions:
        file_extensions = [ext.lower() for ext in file_extensions]

    if not search_path:
        candidates = [
            os.path.expanduser("~"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "D:\\", "E:\\", "F:\\"
        ]
    else:
        candidates = [search_path]

    results = []
    files_scanned = 0
    start_time = time.time()

    use_wildcard = '*' in keyword or '?' in keyword

    for base in candidates:
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base, topdown=True, onerror=lambda e: logger.debug("遍历错误: %s", e)):
            # 深度限制
            depth = root[len(base):].count(os.sep)
            if depth > 3:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in get_safety_config().search_skip_dirs]

            for f in files:
                files_scanned += 1
                # 文件总数上限
                if files_scanned > _SEARCH_MAX_FILES:
                    logger.info("文件搜索达到总数上限 %d，停止遍历", _SEARCH_MAX_FILES)
                    break
                # 超时检查
                if files_scanned % 5000 == 0 and time.time() - start_time > _SEARCH_TIMEOUT_SEC:
                    logger.info("文件搜索超时 %ds，返回部分结果", _SEARCH_TIMEOUT_SEC)
                    break

                if use_wildcard:
                    if not fnmatch.fnmatch(f.lower(), keyword.lower()):
                        continue
                else:
                    if keyword.lower() not in f.lower():
                        continue
                if file_extensions:
                    if os.path.splitext(f)[1].lower() not in file_extensions:
                        continue
                results.append(os.path.join(root, f))
            else:
                continue
            break
        else:
            continue
        break

    # 多盘全量搜索后返回 Top N（修复：原来找到 max_results 就直接 return）
    results = results[:max_results]
    return json.dumps(results, ensure_ascii=False)


# ═══════════════════════════════════════════
#  网页浏览工具
# ═══════════════════════════════════════════

_USER_AGENT = USER_AGENT
_MOBILE_UA = MOBILE_UA

# 二进制文件魔数（字节级）
_BINARY_MAGIC = {
    b"%PDF": "pdf",
    b"PK\x03\x04": "zip",
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF8": "gif",
    b"\x1f\x8b": "gzip",
    b"ID3": "mp3",
    b"ftyp": "mp4",
}

# JS 渲染页面的典型空壳特征（需同时匹配 2+ 个才判定）
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


def _is_binary_content(resp: requests.Response) -> str | None:
    """通过原始字节魔数检测二进制文件类型，返回类型名或 None。"""
    content = resp.content[:16]
    for magic, ftype in _BINARY_MAGIC.items():
        if content.startswith(magic):
            return ftype
    return None


def _build_mobile_url(url: str) -> str:
    """构建移动端 m. 子域名 URL，避免双重 m. 前缀。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # 已经是 m. 前缀，无需替换
    if host.startswith("m."):
        return url

    # 替换 www. → m.
    if host.startswith("www."):
        new_host = "m." + host[4:]
    else:
        new_host = "m." + host

    return url.replace(host, new_host, 1)


@register_tool
def web_search(query: str, max_results: int = 10):
    """使用 Bing 搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。"""
    try:
        url = "https://www.bing.com/search"
        resp = _get_session().get(
            url, params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=max(SEARCH_TIMEOUT // 3, 5),
        )
        if resp.status_code == 429:
            return json.dumps({
                "success": False,
                "error": "Bing 搜索触发限流 (429)，请等待 30 秒后重试或换用其他搜索引擎。"
            }, ensure_ascii=False)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning("搜索请求失败: %s", str(e)[:150])
        return json.dumps({"success": False, "error": f"搜索请求失败: {str(e)}"}, ensure_ascii=False)

    results = []
    html_raw = html

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

    # 2. 正则解析失败 → 尝试 LLM 提取
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
        if "error" not in rendered:
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
        # 渲染失败时回退到普通抓取
        logger.info("Playwright 渲染失败，回退普通抓取: %s", rendered.get("error", ""))

    raw = None
    resp_url = url
    errors = []
    last_resp = None

    for attempt in range(3):
        try:
            if attempt == 0:
                ua = _USER_AGENT
                target_url = url
            elif attempt == 1:
                ua = _MOBILE_UA
                target_url = url
            else:
                # 尝试 m. 子域名（修复双重 m. 前缀）
                ua = _MOBILE_UA
                target_url = _build_mobile_url(url)

            resp = _get_session().get(
                target_url, headers={"User-Agent": ua},
                timeout=SEARCH_TIMEOUT, allow_redirects=True,
            )
            resp.raise_for_status()

            # ── 二进制魔数检测：用原始字节判断 ──
            binary_type = _is_binary_content(resp)
            if binary_type:
                logger.info("检测到二进制文件 (%s)，路由到下载工具: %s", binary_type, url)
                from .web_download import web_download
                return web_download(url)

            if resp.encoding and resp.encoding.lower() != "utf-8":
                try:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                except Exception:
                    pass
            raw = resp.text
            resp_url = resp.url
            last_resp = resp
            break
        except Exception as e:
            errors.append(f"[{attempt}] {str(e)[:80]}")
            continue

    if raw is None:
        return json.dumps({"success": False, "error": f"网页抓取失败: {'; '.join(errors)}"}, ensure_ascii=False)

    # 对常见文件扩展名做路由
    parsed = urlparse(resp_url)
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in get_safety_config().downloadable_exts):
        from .web_download import web_download
        result = web_download(url)
        data = json.loads(result)
        if data.get("success"):
            data["_routed_from"] = "fetch_webpage（自动检测到可下载文件）"
            return json.dumps(data, ensure_ascii=False)
        # 下载失败时继续尝试文本抓取
        logger.info("自动下载失败，继续文本抓取: %s", data.get("error", ""))

    body_m = re.search(r"<body[^>]*>(.*?)</body>", raw, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else raw

    for tag in ["nav", "footer", "header", "aside", "noscript", "iframe", "script", "style"]:
        body = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", body, flags=re.DOTALL | re.IGNORECASE)

    text = _strip_html(body)

    # 检测 JS 空壳（优化：需同时匹配 2+ 个特征，降低误判率）
    is_js_shell = False
    if len(text.strip()) < 100:
        match_count = sum(1 for pattern in _JS_SHELL_PATTERNS if re.search(pattern, raw, re.IGNORECASE))
        if match_count >= 2:
            is_js_shell = True

    if is_js_shell:
        text += ("\n\n⚠ [检测到 JS 渲染页面，正文可能不完整。建议搜索该文章的转载(知乎/CSDN/博客园)，"
                 "或使用移动端 URL 重试。]")

    if len(text) > max_length:
        text = text[:max_length] + f"\n… [内容截断，原文约 {len(text)} 字符]"

    title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL | re.IGNORECASE)
    title = _strip_html(title_m.group(1)) if title_m else resp_url

    return json.dumps({"success": True, "url": resp_url, "title": title, "text": text}, ensure_ascii=False)


def _fetch_single_url(url: str, max_length: int) -> str:
    """抓取单个 URL 并返回格式化结果（供线程池调用）。"""
    try:
        resp = _get_session().get(
            url, headers={"User-Agent": _MOBILE_UA},
            timeout=SEARCH_TIMEOUT, allow_redirects=True,
        )
        resp.raise_for_status()

        # 二进制检测
        if _is_binary_content(resp):
            return f"---\n来源: {url}\n[检测到二进制文件，建议使用 web_download 工具下载]"

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
        return f"---\n来源: {title}\n{text[:max_length]}"
    except Exception as e:
        logger.debug("批量抓取失败 %s: %s", url, str(e)[:100])
        return f"---\n来源: {url}\n[抓取失败: {str(e)[:100]}]"


@register_tool
def fetch_multiple(urls: list, max_length: int = 2000):
    """批量抓取多个 URL，返回合并结果。适合同时阅读多篇搜索结果。urls 为 URL 字符串列表，建议 3-5 个。"""
    urls = urls[:5]
    results = [None] * len(urls)

    # 线程池并发抓取，控制并发数
    with ThreadPoolExecutor(max_workers=min(len(urls), 3)) as executor:
        future_to_idx = {
            executor.submit(_fetch_single_url, url, max_length): idx
            for idx, url in enumerate(urls)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = f"---\n来源: {urls[idx]}\n[抓取失败: {str(e)[:100]}]"

    return json.dumps({"success": True, "combined": "\n\n".join(r for r in results if r)}, ensure_ascii=False)


@register_tool
def get_weather(city: str = "Shenzhen"):
    """查询城市天气。优先使用 wttr.in API，失败时自动回退到必应搜索。city 支持中文名、拼音或英文。"""
    # 主路径：wttr.in
    try:
        url = f"https://wttr.in/{city}?format=j1&lang=zh"
        resp = _get_session().get(url, headers={"User-Agent": _USER_AGENT}, timeout=max(SEARCH_TIMEOUT // 3, 5))
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
    except Exception as e:
        logger.info("wttr.in 失败，回退到必应搜索: %s", str(e)[:100])

    # 回退路径：必应搜索
    try:
        query = f"{city} 天气 明天" if "明天" not in str(city) else f"{city} 天气"
        url = "https://www.bing.com/search"
        resp = _get_session().get(
            url, params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=max(SEARCH_TIMEOUT // 3, 5),
        )
        resp.raise_for_status()
        html = resp.text

        # 尝试提取必应天气卡片
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
        logger.warning("天气查询完全失败: %s", str(e)[:150])
        return json.dumps({"success": False, "error": f"天气查询失败(wttr.in超时且搜索回退失败): {str(e)}"}, ensure_ascii=False)
