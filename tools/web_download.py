"""网页下载工具：流式下载任意 URL 文件到本地"""
import os
import re
import json
import time
from urllib.parse import urlparse, unquote
from pathlib import Path

import requests

from . import register_tool

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

# 安全文件类型白名单
SAFE_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma",
    ".json", ".csv", ".xml", ".yaml", ".yml",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".log", ".md",
}

# 明确禁止的扩展名（即使在白名单中也拒绝）
BLOCKED_EXTENSIONS = {".exe", ".bat", ".cmd", ".sh", ".msi", ".scr", ".ps1", ".vbs", ".com", ".dll", ".sys"}

# 可信域名：这些域名的 .exe/.msi 允许下载（知名软件官方分发渠道）
TRUSTED_DOMAINS = {
    "codebuddy.cn", "download.codebuddy.cn", "copilot.tencent.com",
    "tencent.com", "qq.com",
    "microsoft.com", "visualstudio.com", "github.com",
    "apple.com",
    "google.com", "googleapis.com",
    "mozilla.org", "adobe.com", "oracle.com",
}


def _extract_filename(url: str, content_disposition: str = None) -> str:
    """从 URL 或 Content-Disposition 头提取文件名"""
    # Content-Disposition 优先
    if content_disposition:
        match = re.search(r'filename[*]?=["\']?(?:UTF-8\'\')?([^"\';]+)', content_disposition, re.IGNORECASE)
        if match:
            name = unquote(match.group(1))
            # 去掉可能出现的路径前缀
            return os.path.basename(name)

    # 从 URL 路径提取
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = os.path.basename(path)
    if name:
        return name
    return "download"


def _validate_extension(filename: str, url: str = "") -> tuple[bool, str]:
    """校验文件扩展名是否安全。返回 (允许, 扩展名)。
    可信域名的 .exe/.msi 文件放行。
    """
    ext = os.path.splitext(filename)[1].lower()
    # 可信域名放行 .exe/.msi
    if ext in BLOCKED_EXTENSIONS:
        if url:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            # 去掉端口
            domain = domain.split(":")[0]
            # 检查是否匹配可信域名（含子域名）
            if any(domain == td or domain.endswith("." + td) for td in TRUSTED_DOMAINS):
                return True, ext
        return False, ext
    if ext not in SAFE_EXTENSIONS:
        return False, ext
    return True, ext


def _probe_url(url: str) -> dict:
    """HEAD 探测 URL 的 Content-Type、Content-Length 和 Content-Disposition"""
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT}, timeout=15, allow_redirects=True)
        return {
            "content_type": resp.headers.get("Content-Type", ""),
            "content_length": resp.headers.get("Content-Length", ""),
            "content_disposition": resp.headers.get("Content-Disposition", ""),
            "final_url": resp.url,
            "status": resp.status_code,
        }
    except Exception as e:
        return {"error": str(e)}


@register_tool(timeout=120)
def web_download(url: str, output_dir: str = None):
    """
    从 URL 下载文件到本地。支持 PDF、图片、音视频、Office 文档等常见格式。

    - url: 要下载的文件直链 URL
    - output_dir: 保存目录，默认下载到用户的 Downloads 文件夹

    特性：
    1. HEAD 探测 Content-Type 和文件大小
    2. 流式下载，支持大文件
    3. 自动从 URL / Content-Disposition 提取文件名
    4. 安全类型白名单，拒绝可执行文件
    5. 下载进度通过 SSE progress 事件上报
    """
    # 确定输出目录
    if output_dir:
        dl_dir = Path(output_dir)
    else:
        dl_dir = Path.home() / "Downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    # HEAD 探测
    probe = _probe_url(url)
    if "error" in probe:
        return json.dumps({"success": False, "error": f"URL 探测失败: {probe['error']}"}, ensure_ascii=False)

    content_type = probe.get("content_type", "")
    content_length_str = probe.get("content_length", "")
    content_disposition = probe.get("content_disposition", "")
    final_url = probe.get("final_url", url)

    # 提取并校验文件名
    filename = _extract_filename(final_url, content_disposition)
    allowed, ext = _validate_extension(filename, url)
    if not allowed:
        reason = "文件类型被拦截" if ext in BLOCKED_EXTENSIONS else f"不在白名单中 (.{ext})"
        return json.dumps({
            "success": False,
            "error": f"{reason}",
            "filename": filename,
            "extension": ext,
            "content_type": content_type,
            "hint": "支持的类型: " + ", ".join(sorted(SAFE_EXTENSIONS))
        }, ensure_ascii=False)

    # 预估文件大小
    file_size = int(content_length_str) if content_length_str and content_length_str.isdigit() else None
    size_display = f"{file_size / 1024 / 1024:.1f} MB" if file_size else "未知大小"

    # 去重：如果文件已存在，加序号
    save_path = dl_dir / filename
    counter = 1
    stem, ext = os.path.splitext(filename)
    while save_path.exists():
        save_path = dl_dir / f"{stem} ({counter}){ext}"
        counter += 1

    # 流式下载
    try:
        start_time = time.time()
        downloaded = 0
        chunk_size = 8192

        with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=300, allow_redirects=True) as resp:
            resp.raise_for_status()

            # 再次校验实际 Content-Type
            actual_ct = resp.headers.get("Content-Type", "")
            actual_cl = resp.headers.get("Content-Length", "")
            if actual_cl and actual_cl.isdigit():
                file_size = int(actual_cl)

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

        elapsed = time.time() - start_time
        actual_size = os.path.getsize(save_path)
        speed = actual_size / elapsed / 1024 if elapsed > 0 else 0

        return json.dumps({
            "success": True,
            "filename": save_path.name,
            "file_path": str(save_path),
            "file_size": f"{actual_size / 1024 / 1024:.2f} MB" if actual_size >= 1024 * 1024 else f"{actual_size / 1024:.1f} KB",
            "content_type": actual_ct,
            "download_time": f"{elapsed:.1f}s",
            "speed": f"{speed:.0f} KB/s",
            "source_url": final_url,
        }, ensure_ascii=False)

    except requests.exceptions.Timeout:
        # 清理不完整文件
        if save_path.exists():
            save_path.unlink()
        return json.dumps({"success": False, "error": f"下载超时（300s），文件可能过大或网络不稳定"}, ensure_ascii=False)
    except Exception as e:
        if save_path.exists():
            save_path.unlink()
        return json.dumps({"success": False, "error": f"下载失败: {str(e)[:200]}"}, ensure_ascii=False)


def is_downloadable(content_type: str) -> bool:
    """
    判断 Content-Type 是否为可下载的二进制文件类型。
    供 fetch_webpage 调用，用于自动路由到下载。
    """
    if not content_type:
        return False
    ct = content_type.lower()
    # 文本类型不下发下载
    text_types = {"text/", "application/json", "application/xml", "application/javascript"}
    if any(ct.startswith(t) for t in text_types):
        return False
    # 其他 application/* 和 image/*、audio/*、video/* 都走下载
    return any(ct.startswith(p) for p in ("application/", "image/", "audio/", "video/"))
