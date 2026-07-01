"""网页下载工具：流式下载任意 URL 文件到本地"""
import os
import re
import json
import time
import uuid
from urllib.parse import urlparse, unquote
from pathlib import Path

import requests

from . import register_tool
from config.config_loader import get_safety_config

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


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
def web_download(url: str, output_dir: str = None, confirmed: bool = False, confirmation_token: str = ""):
    """
    从 URL 下载文件到本地。除可执行文件需要用户确认外，无文件类型限制。

    - url: 要下载的文件直链 URL
    - output_dir: 保存目录，默认下载到用户的 Downloads 文件夹
    - confirmed: 内部标志，LLM 勿传
    - confirmation_token: 内部标志，LLM 勿传

    可执行文件（.exe/.msi/.bat 等）下载前会弹出确认卡片让用户判断来源是否可信。
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

    # 提取文件名
    filename = _extract_filename(final_url, content_disposition)
    ext = os.path.splitext(filename)[1].lower()

    # 预估文件大小
    file_size = int(content_length_str) if content_length_str and content_length_str.isdigit() else None
    size_display = f"{file_size / 1024 / 1024:.1f} MB" if file_size else "未知大小"

    # 可执行文件：未确认时返回确认请求
    if ext in get_safety_config().risky_extensions:
        if not confirmed:
            token = uuid.uuid4().hex
            return json.dumps({
                "confirmation_required": True,
                "confirmation_token": token,
                "title": "下载可执行文件确认",
                "message": f"即将下载 {filename}（{size_display}）",
                "details": {
                    "文件名": filename,
                    "类型": f"{ext} 可执行文件",
                    "来源URL": final_url,
                    "大小": size_display,
                    "风险提示": "可执行文件可能包含恶意代码，请确认来源可信后再下载",
                },
                "url": url,
                "output_dir": output_dir,
            }, ensure_ascii=False)
        # confirmed=True 时继续下载，不做二次校验

    # 去重：如果文件已存在，加序号
    save_path = dl_dir / filename
    counter = 1
    stem, file_ext = os.path.splitext(filename)
    while save_path.exists():
        save_path = dl_dir / f"{stem} ({counter}){file_ext}"
        counter += 1

    # 流式下载
    try:
        start_time = time.time()
        downloaded = 0
        chunk_size = 8192

        with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=300, allow_redirects=True) as resp:
            resp.raise_for_status()

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
