"""
search_tools.py - 搜索工具集（多源 + 相关性校验版）

改造原则：
- 公开函数签名不变（web_search / fetch_webpage / search_images / search_news / search_academic）
- 搜索后端从 HTML 爬取替换为 API 调用
- 新增相关性校验层
- fetch_webpage / get_webpage_content 保留原逻辑，小幅增强
"""

import json
import os
import re
import time
import hashlib
import logging
import threading
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, quote_plus

import requests

from . import register_tool

logger = logging.getLogger("tank.search")

# ═══════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════

SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT", "15"))
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# API Keys（从环境变量读取）
_BING_API_KEY = os.environ.get("BING_SEARCH_API_KEY", "")
_SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
_SEMANTIC_SCHOLAR_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")  # 可选


# ═══════════════════════════════════════════════════════
# 辅助函数（保留原有）
# ═══════════════════════════════════════════════════════

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                _session.headers.update({"User-Agent": _USER_AGENT})
    return _session


def _strip_html(text: str) -> str:
    """去除 HTML 标签"""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _check_llm_available() -> bool:
    """检查 LLM 是否可用（保留原逻辑）"""
    try:
        from llm_client import get_llm_client
        client = get_llm_client()
        return client is not None
    except Exception:
        return False


# ═══════════════════════════════════════════════════════
# 搜索缓存
# ═══════════════════════════════════════════════════════

class _SearchCache:
    """线程安全的搜索缓存"""

    def __init__(self, ttl: int = 300, max_size: int = 200):
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max_size = max_size

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            if key in self._cache:
                ts, value = self._cache[key]
                if time.time() - ts < self._ttl:
                    return value
                del self._cache[key]
        return None

    def set(self, key: str, value: str):
        with self._lock:
            if len(self._cache) >= self._max_size:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest]
            self._cache[key] = (time.time(), value)


_cache = _SearchCache()


def _cache_key(func_name: str, query: str) -> str:
    return hashlib.md5(f"{func_name}:{query.lower().strip()}".encode()).hexdigest()


# ═══════════════════════════════════════════════════════
# 相关性校验器
# ═══════════════════════════════════════════════════════

# 域名黑名单（基于上次日志中的垃圾结果）
_DOMAIN_BLACKLIST = {} #先注释一下
'''
    "baike.baidu.com",
    "data.stats.gov.cn",
    "data.gov.cn",
    "www.iciba.com",
    "dictionary.cambridge.org",
    "www.zdic.net",
    "zh.wikipedia.org",
    "en.wikipedia.org",
    "www.hydcd.com",      # 汉典
    "cidian.911cha.com",  # 词典
}'''

# 通用无意义标题关键词（搜索结果标题包含这些词大概率无关）
_JUNK_TITLE_PATTERNS = re.compile(
    r'(是什么意思|中文翻译|_百度百科|_词典|怎么读|读音|解释|含义)',
    re.IGNORECASE
)


def _tokenize(text: str) -> set:
    """简单分词：英文按空格，中文按 bigram"""
    text = re.sub(r'[^\w\u4e00-\u9fff]', ' ', text.lower())
    words = set(text.split())
    for segment in re.findall(r'[\u4e00-\u9fff]+', text):
        for i in range(len(segment) - 1):
            words.add(segment[i:i + 2])
    return words


def _chinese_char_overlap(query: str, text: str) -> float:
    """中文字符级重叠度：查询中的汉字有多少出现在文本中"""
    query_chars = set(re.findall(r'[\u4e00-\u9fff]', query.lower()))
    if not query_chars:
        return 0.0
    text_chars = set(re.findall(r'[\u4e00-\u9fff]', text.lower()))
    if not text_chars:
        return 0.0
    return len(query_chars & text_chars) / len(query_chars)


def _substring_hit_rate(query: str, text: str) -> float:
    """中文词组子串命中率：查询中 2-4 字词组在文本中出现的比例"""
    phrases = re.findall(r'[\u4e00-\u9fff]{2,4}', query.lower())
    if not phrases:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for p in phrases if p in text_lower)
    return hits / len(phrases)


def _compute_relevance(query: str, title: str, snippet: str) -> float:
    """混合相关性打分（中文优化版）。

    中文搜索使用字符重叠 + 子串匹配替代纯 bigram Jaccard，
    解决长查询 bigram 分母过大导致分数极低的问题。
    """
    query_chars = set(re.findall(r'[\u4e00-\u9fff]', query.lower()))

    if query_chars:
        # ── 中文查询路径：字符重叠 40% + 子串匹配 35% + bigram Jaccard 25% ──
        char_title = _chinese_char_overlap(query, title)
        char_snippet = _chinese_char_overlap(query, snippet)
        substr_title = _substring_hit_rate(query, title)
        substr_snippet = _substring_hit_rate(query, snippet)
        jac_title = _jaccard(_tokenize(query), _tokenize(title))
        jac_snippet = _jaccard(_tokenize(query), _tokenize(snippet)) if snippet.strip() else 0.0

        title_score = char_title * 0.40 + substr_title * 0.35 + jac_title * 0.25
        snippet_score = char_snippet * 0.40 + substr_snippet * 0.35 + jac_snippet * 0.25

        if snippet.strip():
            return title_score * 0.7 + snippet_score * 0.3
        else:
            return title_score  # 无 snippet 时只看标题
    else:
        # ── 纯英文/数字查询路径：bigram Jaccard 为主 ──
        qt = _tokenize(query)
        title_score = _jaccard(qt, _tokenize(title))
        snippet_score = _jaccard(qt, _tokenize(snippet)) if snippet.strip() else 0.0
        if snippet.strip():
            return title_score * 0.7 + snippet_score * 0.3
        else:
            return title_score


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_blacklisted(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        return any(bl in domain for bl in _DOMAIN_BLACKLIST)
    except Exception:
        return False

@register_tool
def validate_relevance(query: str, results: List[Dict], min_score: float = 0.06) -> List[Dict]:
    """
    对搜索结果进行相关性校验。
    返回通过校验的结果列表（已按分数降序排列）。
    min_score 默认 0.06，对中文搜索放宽以避免 bigram 分母膨胀导致的过度过滤。
    """
    if not results:
        return []

    scored = []

    for r in results:
        url = r.get("url", "")
        title = r.get("title", "")
        snippet = r.get("snippet", "")

        # 一票否决：域名黑名单
        if _is_blacklisted(url):
            continue

        # 一票否决：标题匹配垃圾模式
        if _JUNK_TITLE_PATTERNS.search(title):
            continue

        # 混合相关性打分（中文优化）
        score = _compute_relevance(query, title, snippet)

        if score >= min_score:
            r["relevance_score"] = round(score, 3)
            scored.append(r)

    scored.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    if not scored and results:
        logger.warning(
            f"相关性校验: {len(results)} 条结果全部未通过 "
            f"(query='{query[:40]}')"
        )

    return scored


# ═══════════════════════════════════════════════════════
# 搜索后端（多源）
# ═══════════════════════════════════════════════════════

def _search_bing_api(query: str, max_results: int, market: str = "zh-CN") -> List[Dict]:
    """Bing Web Search API v7"""
    if not _BING_API_KEY:
        raise RuntimeError("BING_SEARCH_API_KEY 未配置")

    resp = _get_session().get(
        "https://api.bing.microsoft.com/v7.0/search",
        headers={"Ocp-Apim-Subscription-Key": _BING_API_KEY},
        params={"q": query, "count": max_results, "mkt": market, "textFormat": "Raw"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    return [
        {
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", "")[:300],
        }
        for item in data.get("webPages", {}).get("value", [])
    ]


def _search_duckduckgo(query: str, max_results: int) -> List[Dict]:
    """DuckDuckGo（免费兜底，无需 API Key）"""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        raise RuntimeError("duckduckgo-search 未安装: pip install duckduckgo-search")

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results, region="cn-zh"):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", "")[:300],
            })
    return results


def _search_serp_api(query: str, max_results: int) -> List[Dict]:
    """SerpAPI（Google 搜索，付费）"""
    if not _SERP_API_KEY:
        raise RuntimeError("SERP_API_KEY 未配置")

    resp = _get_session().get(
        "https://serpapi.com/search",
        params={
            "q": query, "api_key": _SERP_API_KEY,
            "engine": "google", "num": max_results, "hl": "zh-cn",
        },
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", "")[:300],
        }
        for item in data.get("organic_results", [])
    ]

@register_tool
def _search_baidu(query: str, max_results: int) -> List[Dict]:
    """百度搜索（免费，HTML 爬取兜底）"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 未安装: pip install beautifulsoup4")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    url = f"https://www.baidu.com/s?wd={quote_plus(query)}&rn={min(max_results, 20)}"
    resp = _get_session().get(url, headers=headers, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    # Baidu may return gbk encoding
    if resp.encoding and resp.encoding.lower() in ("gbk", "gb2312"):
        resp.encoding = "utf-8"
        html_text = resp.text.encode("gbk", errors="ignore").decode("gbk", errors="ignore")
    else:
        html_text = resp.text

    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    # 多套选择器兼容不同时期的百度页面结构
    for item in soup.select(
        ".result, .c-container, .result-op, "
        "div[class*='result'], div[class*='c-container']"
    ):
        # 标题提取：尝试多种选择器
        title_el = (
            item.select_one("h3 a") or
            item.select_one("h3.t a") or
            item.select_one("a[class*='title']") or
            item.select_one(".t a")
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        # 摘要提取：尝试多种选择器 + fallback 到任意文本
        snippet_el = (
            item.select_one(".c-abstract") or
            item.select_one(".c-span-last") or
            item.select_one("span[class*='content']") or
            item.select_one(".c-row") or
            item.select_one("div[class*='abstract']") or
            item.select_one("span[class*='desc']")
        )
        if snippet_el:
            snippet = snippet_el.get_text(strip=True)[:300]
        else:
            # fallback: 取容器内除标题外的可见文本
            all_text = item.get_text(separator=" ", strip=True)
            if title in all_text:
                all_text = all_text.replace(title, "", 1)
            snippet = all_text.strip()[:300]

        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


def _multi_source_search(query: str, max_results: int, market: str = "zh-CN") -> Tuple[List[Dict], str]:
    """
    多源搜索调度：按优先级尝试，返回 (results, provider_name)。
    优先级: Bing API > SerpAPI > DuckDuckGo > Baidu(HTML兜底)
    """
    providers = [
        ("bing_api", lambda: _search_bing_api(query, max_results, market)),
        ("serp_api", lambda: _search_serp_api(query, max_results)),
        ("duckduckgo", lambda: _search_duckduckgo(query, max_results)),
        ("baidu", lambda: _search_baidu(query, max_results)),
    ]

    last_error = ""
    for name, search_fn in providers:
        try:
            results = search_fn()
            if results:
                logger.info(f"搜索成功: provider={name}, query='{query[:40]}', count={len(results)}")
                return results, name
            logger.debug(f"搜索源 {name} 返回空结果，尝试下一个")
        except Exception as e:
            last_error = f"{name}: {str(e)[:100]}"
            logger.warning(f"搜索源 {name} 失败: {last_error}")
            continue

    logger.error(f"所有搜索源均失败: {last_error}")
    return [], ""


# ═══════════════════════════════════════════════════════
# 公开函数: search_files（按文件名递归搜索）
# ═══════════════════════════════════════════════════════
@register_tool
def search_files(keyword: str, search_path: str = None, file_extensions: list = None) -> str:
    """按文件名关键词递归搜索文件（深度3层，跳过系统目录，max_results=5）。keyword: 文件名关键词, search_path: 搜索起始路径（可选，默认用户目录及D/E/F盘）, file_extensions: 扩展名过滤如[".pdf"]（可选）"""
    import fnmatch

    # 默认搜索路径
    if not search_path:
        candidates = []
        # 用户目录
        user_dir = os.path.expanduser("~")
        if os.path.isdir(user_dir):
            candidates.append(user_dir)
        # D/E/F盘
        for drive in ["D:", "E:", "F:"]:
            dp = f"{drive}\\"
            if os.path.isdir(dp):
                candidates.append(dp)
        search_paths = candidates
    else:
        if not os.path.isdir(search_path):
            return json.dumps({
                "success": False,
                "message": f"搜索路径不存在: {search_path}"
            }, ensure_ascii=False)
        search_paths = [search_path]

    # 系统目录黑名单
    SKIP_DIRS = {
        "Windows", "System32", "SystemRoot", "Program Files", "Program Files (x86)",
        "AppData", "$Recycle.Bin", "Recovery", "Config.Msi", "Boot",
        "ProgramData", "Common Files", "Microsoft.NET", "assembly", "WinSxS",
        "node_modules", ".git", ".svn", "__pycache__", "venv", ".venv"
    }

    results = []
    MAX_DEPTH = 3
    MAX_RESULTS = 5

    for base in search_paths:
        if len(results) >= MAX_RESULTS:
            break
        base = os.path.normpath(base)
        try:
            for root, dirs, files in os.walk(base):
                # 深度限制
                depth = root[len(base):].count(os.sep) if root != base else 0
                if depth >= MAX_DEPTH:
                    dirs[:] = []
                    continue

                # 跳过系统目录
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

                # 跳过隐藏目录
                dirs[:] = [d for d in dirs if not d.startswith(".")]

                for fname in files:
                    if len(results) >= MAX_RESULTS:
                        break
                    # 扩展名过滤
                    if file_extensions:
                        ext = os.path.splitext(fname)[1].lower()
                        if ext not in [e.lower() for e in file_extensions]:
                            continue
                    # 关键词匹配（大小写不敏感）
                    if keyword.lower() in fname.lower():
                        full_path = os.path.join(root, fname)
                        try:
                            fsize = os.path.getsize(full_path)
                        except OSError:
                            fsize = 0
                        results.append({
                            "name": fname,
                            "path": full_path,
                            "size": fsize
                        })
        except (PermissionError, OSError):
            continue

    return json.dumps({
        "success": True,
        "results": results,
        "total": len(results),
        "max_results": MAX_RESULTS
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 公开函数 1: web_search
# ═══════════════════════════════════════════════════════
@register_tool
def web_search(query: str, max_results: int = 10) -> str:
    """使用多源搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。"""

    # 1. 缓存检查
    ck = _cache_key("web_search", query)
    cached = _cache.get(ck)
    if cached:
        logger.debug(f"搜索缓存命中: query='{query[:30]}'")
        return cached

    # 2. 多源搜索
    start = time.time()
    raw_results, provider = _multi_source_search(query, max_results)
    latency = (time.time() - start) * 1000

    if not raw_results and not provider:
        output = json.dumps({
            "success": False,
            "error": "所有搜索源均不可用",
            "suggestion": "请30秒后重试，或换用更具体的关键词，或基于已有知识生成并标注'未经验证'",
            "latency_ms": round(latency, 1),
        }, ensure_ascii=False)
        return output

    # 3. 相关性校验
    validated = validate_relevance(query, raw_results)

    # 4. 构建输出
    output_data = {
        "success": True,
        "query": query,
        "provider": provider,
        "latency_ms": round(latency, 1),
        "raw_count": len(raw_results),
        "filtered_count": len(validated),
        "relevance_passed": len(validated) > 0,
        "results": [
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"][:200],
                "relevance_score": r.get("relevance_score", 0),
            }
            for r in validated[:max_results]
        ],
    }

    if not validated:
        output_data["warning"] = (
            "搜索结果与查询相关性极低，已全部过滤。"
            "建议：1) 更换更具体的关键词 2) 使用 fetch_webpage 直接抓取目标网站"
        )

    output = json.dumps(output_data, ensure_ascii=False)

    # 5. 写入缓存
    _cache.set(ck, output)
    return output


# ═══════════════════════════════════════════════════════
# 公开函数 2: fetch_webpage（保留原逻辑 + 小幅增强）
# ═══════════════════════════════════════════════════════
@register_tool
def fetch_webpage(url: str, max_length: int = 5000) -> str:
    """抓取指定网页的正文内容。适合深入阅读某个搜索结果。"""
    try:
        # 增加重试（1次）
        for attempt in range(2):
            try:
                resp = _get_session().get(url, timeout=SEARCH_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    logger.warning(f"fetch_webpage 超时重试: {url[:60]}")
                    time.sleep(1)
                    continue
                raise
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"网页抓取失败: {str(e)[:150]}",
            "url": url,
        }, ensure_ascii=False)

    # 提取正文（保留原有正则逻辑）
    html = resp.text
    # 移除 script/style
    html = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # 提取 <p> 和 <article> 内容
    paragraphs = re.findall(r'<(?:p|article|div class="content")[^>]*>(.*?)</(?:p|article|div)>', html, re.DOTALL)
    text = '\n'.join(_strip_html(p) for p in paragraphs if len(_strip_html(p)) > 20)

    if not text:
        # fallback: 去标签后取全文
        text = _strip_html(html)

    text = text[:max_length]

    return json.dumps({
        "success": True,
        "url": url,
        "content": text,
        "content_length": len(text),
        "truncated": len(text) >= max_length,
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 公开函数 3: search_images（替换后端）
# ═══════════════════════════════════════════════════════

@register_tool
def search_images(query: str, max_results: int = 10) -> str:
    """搜索图片，返回图片标题、链接和缩略图。"""

    ck = _cache_key("search_images", query)
    cached = _cache.get(ck)
    if cached:
        return cached

    results = []
    provider = ""

    # 优先: Bing Image Search API
    if _BING_API_KEY:
        try:
            resp = _get_session().get(
                "https://api.bing.microsoft.com/v7.0/images/search",
                headers={"Ocp-Apim-Subscription-Key": _BING_API_KEY},
                params={"q": query, "count": max_results, "mkt": "zh-CN"},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                results.append({
                    "title": item.get("name", ""),
                    "url": item.get("contentUrl", ""),
                    "thumbnail": item.get("thumbnailUrl", ""),
                    "source": item.get("hostPageDisplayUrl", ""),
                })
            provider = "bing_api"
        except Exception as e:
            logger.warning(f"Bing Image API 失败: {str(e)[:100]}")

    # 兜底: DuckDuckGo Images
    if not results:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                for r in ddgs.images(query, max_results=max_results, region="cn-zh"):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("image", ""),
                        "thumbnail": r.get("thumbnail", ""),
                        "source": r.get("source", ""),
                    })
            provider = "duckduckgo"
        except Exception as e:
            logger.warning(f"DuckDuckGo Images 失败: {str(e)[:100]}")

    output = json.dumps({
        "success": bool(results),
        "provider": provider,
        "results": results[:max_results],
        "count": len(results),
    }, ensure_ascii=False)

    _cache.set(ck, output)
    return output


# ═══════════════════════════════════════════════════════
# 公开函数 4: search_news（替换后端）
# ═══════════════════════════════════════════════════════

@register_tool
def search_news(query: str, max_results: int = 10) -> str:
    """搜索新闻，返回新闻标题、来源、链接和摘要。适合获取时事热点。"""

    ck = _cache_key("search_news", query)
    cached = _cache.get(ck)
    if cached:
        return cached

    results = []
    provider = ""

    # 优先: Bing News Search API
    if _BING_API_KEY:
        try:
            resp = _get_session().get(
                "https://api.bing.microsoft.com/v7.0/news/search",
                headers={"Ocp-Apim-Subscription-Key": _BING_API_KEY},
                params={"q": query, "count": max_results, "mkt": "zh-CN", "freshness": "Month"},
                timeout=SEARCH_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                results.append({
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("description", "")[:200],
                    "source": item.get("provider", [{}])[0].get("name", ""),
                    "published": item.get("datePublished", ""),
                })
            provider = "bing_api"
        except Exception as e:
            logger.warning(f"Bing News API 失败: {str(e)[:100]}")

    # 兜底: DuckDuckGo News
    if not results:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                for r in ddgs.news(query, max_results=max_results, region="cn-zh"):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("body", "")[:200],
                        "source": r.get("source", ""),
                        "published": r.get("date", ""),
                    })
            provider = "duckduckgo"
        except Exception as e:
            logger.warning(f"DuckDuckGo News 失败: {str(e)[:100]}")

    # 相关性校验（新闻同样需要）
    if results:
        results = validate_relevance(query, results, min_score=0.10)

    output = json.dumps({
        "success": bool(results),
        "provider": provider,
        "results": results[:max_results],
        "count": len(results),
    }, ensure_ascii=False)

    _cache.set(ck, output)
    return output


# ═══════════════════════════════════════════════════════
# 公开函数 5: search_academic（替换后端 → Semantic Scholar）
# ═══════════════════════════════════════════════════════
@register_tool
def search_academic(query: str, max_results: int = 10) -> str:
    """搜索学术论文，返回论文标题、作者、摘要和链接。"""

    ck = _cache_key("search_academic", query)
    cached = _cache.get(ck)
    if cached:
        return cached

    results = []
    provider = ""

    # 优先: Semantic Scholar API（免费，无需 Key 也可用，有 Key 限额更高）
    try:
        headers = {}
        if _SEMANTIC_SCHOLAR_KEY:
            headers["x-api-key"] = _SEMANTIC_SCHOLAR_KEY

        resp = _get_session().get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params={
                "query": query,
                "limit": max_results,
                "fields": "title,authors,abstract,url,year,citationCount,venue",
            },
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("data", []):
            authors = ", ".join(a.get("name", "") for a in item.get("authors", [])[:3])
            if len(item.get("authors", [])) > 3:
                authors += " et al."
            results.append({
                "title": item.get("title", ""),
                "authors": authors,
                "abstract": (item.get("abstract") or "")[:300],
                "url": item.get("url", ""),
                "year": item.get("year"),
                "citations": item.get("citationCount", 0),
                "venue": item.get("venue", ""),
            })
        provider = "semantic_scholar"
    except Exception as e:
        logger.warning(f"Semantic Scholar 失败: {str(e)[:100]}")

    # 兜底: DuckDuckGo + site:scholar.google.com
    if not results:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                for r in ddgs.text(f"{query} site:scholar.google.com OR site:arxiv.org",
                                   max_results=max_results):
                    results.append({
                        "title": r.get("title", ""),
                        "authors": "",
                        "abstract": r.get("body", "")[:300],
                        "url": r.get("href", ""),
                        "year": None,
                        "citations": 0,
                        "venue": "",
                    })
            provider = "duckduckgo"
        except Exception as e:
            logger.warning(f"DuckDuckGo Academic 失败: {str(e)[:100]}")

    output = json.dumps({
        "success": bool(results),
        "provider": provider,
        "results": results[:max_results],
        "count": len(results),
    }, ensure_ascii=False)

    _cache.set(ck, output)
    return output


# ═══════════════════════════════════════════════════════
# 公开函数 6: get_webpage_content（合并到 fetch_webpage）
# ═══════════════════════════════════════════════════════

@register_tool
def get_webpage_content(url: str) -> str:
    """获取网页正文内容（fetch_webpage 的别名，保持向后兼容）。"""
    return fetch_webpage(url, max_length=8000)


# ═══════════════════════════════════════════════════════
# 工具注册表（供 Agent 框架使用）
# ═══════════════════════════════════════════════════════

SEARCH_TOOLS = {
    "web_search": {
        "func": web_search,
        "description": "使用多源搜索引擎搜索网页，返回标题、链接和摘要。适合获取最新新闻、实时信息。",
        "parameters": {
            "query": {"type": "string", "description": "搜索关键词", "required": True},
            "max_results": {"type": "integer", "description": "最大返回条数", "default": 10},
        },
    },
    "fetch_webpage": {
        "func": fetch_webpage,
        "description": "抓取指定网页的正文内容。适合深入阅读某个搜索结果。",
        "parameters": {
            "url": {"type": "string", "description": "目标网页URL", "required": True},
            "max_length": {"type": "integer", "description": "最大内容长度", "default": 5000},
        },
    },
    "search_images": {
        "func": search_images,
        "description": "搜索图片，返回图片标题、链接和缩略图。",
        "parameters": {
            "query": {"type": "string", "description": "图片搜索关键词", "required": True},
            "max_results": {"type": "integer", "description": "最大返回条数", "default": 10},
        },
    },
    "search_news": {
        "func": search_news,
        "description": "搜索新闻，返回新闻标题、来源、链接和摘要。适合获取时事热点。",
        "parameters": {
            "query": {"type": "string", "description": "新闻搜索关键词", "required": True},
            "max_results": {"type": "integer", "description": "最大返回条数", "default": 10},
        },
    },
    "search_academic": {
        "func": search_academic,
        "description": "搜索学术论文（Semantic Scholar），返回论文标题、作者、摘要和引用数。",
        "parameters": {
            "query": {"type": "string", "description": "学术搜索关键词", "required": True},
            "max_results": {"type": "integer", "description": "最大返回条数", "default": 10},
        },
    },
    "get_webpage_content": {
        "func": get_webpage_content,
        "description": "获取网页正文内容（fetch_webpage 的别名）。",
        "parameters": {
            "url": {"type": "string", "description": "目标网页URL", "required": True},
        },
    },
}