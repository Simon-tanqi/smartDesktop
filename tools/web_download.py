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

# 惰性导入：避免 tools → core → tools 循环导入
_confirmation_manager = None

def _get_confirmation_manager():
    global _confirmation_manager
    if _confirmation_manager is None:
        from core.confirmation_manager import confirmation_manager as _cm
        _confirmation_manager = _cm
    return _confirmation_manager

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


def _is_internal_url(url: str) -> bool:
    """判断 URL 是否为内网地址（SSRF 防护）"""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return True

    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True

    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    except ValueError:
        pass

    if host.endswith(".local") or host.endswith(".internal"):
        return True

    return False


def _safe_redirect(resp, **kwargs):
    """安全重定向回调：禁止重定向到内网地址"""
    new_url = resp.headers.get("Location", "")
    if _is_internal_url(new_url):
        raise requests.exceptions.RequestException(f"禁止重定向到内网地址: {new_url}")
    return True


def _probe_url(url: str) -> dict:
    """HEAD 探测 URL 的 Content-Type、Content-Length 和 Content-Disposition"""
    try:
        resp = requests.head(url, headers={"User-Agent": USER_AGENT}, timeout=5, allow_redirects=True, hooks={"response": [_safe_redirect]})
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
def web_download(url: str, output_dir: str = None, **kwargs):
    """
    从 URL 下载文件到本地。除可执行文件需要用户确认外，无文件类型限制。

    - url: 要下载的文件直链 URL
    - output_dir: 保存目录，默认下载到用户的 Downloads 文件夹

    可执行文件（.exe/.msi/.bat 等）下载前会弹出确认卡片让用户判断来源是否可信。
    """
    # 内部确认标志：由 react_engine Path B 确认通过后带 token 重调注入，
    # 不出现在 LLM 可见的 JSON Schema 中，LLM 不应也无法传这些参数。
    confirmed = bool(kwargs.get("confirmed"))
    confirmation_token = kwargs.get("confirmation_token") or ""
    try:
        from config.settings import DATA_DIR
    except ImportError:
        DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
    workspace_dir = os.path.join(DATA_DIR, "workspace")

    # 确定输出目录并进行路径安全检查
    if output_dir:
        dl_path = Path(output_dir).resolve()
        if not str(dl_path).startswith(workspace_dir) and not str(dl_path).startswith(str(Path.home())):
            return json.dumps({
                "success": False,
                "error": f"输出目录不在允许的范围内，仅允许写入工作区或用户目录",
                "allowed_paths": [workspace_dir, str(Path.home())],
            }, ensure_ascii=False)
        dl_dir = dl_path
    else:
        dl_dir = Path.home() / "Downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    # HEAD 探测（可选，失败时降级）
    probe = _probe_url(url)
    if "error" in probe:
        content_type = ""
        content_length_str = ""
        content_disposition = ""
        final_url = url
    else:
        content_type = probe.get("content_type", "")
        content_length_str = probe.get("content_length", "")
        content_disposition = probe.get("content_disposition", "")
        final_url = probe.get("final_url", url)

    # 提取文件名（基于探测结果或 URL）
    filename = _extract_filename(final_url, content_disposition)
    ext = os.path.splitext(filename)[1].lower()

    # 预估文件大小
    file_size = int(content_length_str) if content_length_str and content_length_str.isdigit() else None
    size_display = f"{file_size / 1024 / 1024:.1f} MB" if file_size else "未知大小"

    # 可执行文件：需要确认（react_engine 层弹出确认卡片）
    if ext in get_safety_config().risky_extensions and not confirmed:
        if not confirmation_token:
            # 无确认令牌 → 返回 confirmation_required，触发 react_engine Path B 确认流程
            token = uuid.uuid4().hex
            _get_confirmation_manager().store(token, {
                "type": "download",
                "url": url,
                "output_dir": output_dir,
            })
            return json.dumps({
                "success": False,
                "error": "下载可执行文件需要预先授权",
                "confirmation_required": True,
                "confirmation_token": token,
                "message": f"确认下载可执行文件 {filename}（{size_display}）？来源: {url}",
                "details": {"filename": filename, "ext": ext, "url": url, "size": size_display},
            }, ensure_ascii=False)

        # 有确认令牌 → 校验（一次性 pop；引擎确认通过后预注册，主进程执行时可见）
        stored_info = _get_confirmation_manager().pop(confirmation_token)
        if not stored_info or stored_info.get("type") != "download":
            return json.dumps({
                "success": False,
                "error": "确认令牌无效或已过期"
            }, ensure_ascii=False)
        if time.time() > stored_info.get("_expiry", 0):
            return json.dumps({
                "success": False,
                "error": "确认令牌已过期"
            }, ensure_ascii=False)
        if stored_info.get("url") != url or (stored_info.get("output_dir") or None) != (output_dir or None):
            return json.dumps({
                "success": False,
                "error": "确认令牌与请求不匹配"
            }, ensure_ascii=False)
        confirmed = True

    # 去重：如果文件已存在，加序号（O_EXCL 原子创建占位文件防止竞态条件）
    save_path = dl_dir / filename
    counter = 1
    stem, file_ext = os.path.splitext(filename)
    max_retries = 100
    fd = None  # 占位文件 fd：创建后保持打开，后续流式写入复用同一 fd，消除 close/re-open 竞态窗口
    while counter <= max_retries:
        try:
            fd = os.open(str(save_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            save_path = dl_dir / f"{stem} ({counter}){file_ext}"
            counter += 1
        except (PermissionError, OSError) as e:
            return json.dumps({
                "success": False,
                "error": f"无法创建下载文件: {e}"
            }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": f"无法生成唯一文件名，已达最大重试次数 ({max_retries})"
        }, ensure_ascii=False)

    # 流式下载（带总时长限制）
    try:
        start_time = time.time()
        downloaded = 0
        chunk_size = 8192
        total_timeout = 120

        with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True, hooks={"response": [_safe_redirect]}) as resp:
            resp.raise_for_status()

            actual_ct = resp.headers.get("Content-Type", "")
            actual_cl = resp.headers.get("Content-Length", "")
            actual_cd = resp.headers.get("Content-Disposition", "")
            actual_final_url = resp.url

            if actual_cl and actual_cl.isdigit():
                file_size = int(actual_cl)

            actual_filename = _extract_filename(actual_final_url, actual_cd)
            actual_ext = os.path.splitext(actual_filename)[1].lower()

            if actual_ext in get_safety_config().risky_extensions and not confirmed:
                # 未开始写入：关闭占位 fd 并清理空占位文件
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
                if save_path.exists():
                    save_path.unlink()
                return json.dumps({
                    "success": False,
                    "error": "实际下载的文件为可执行文件，请重新确认后下载",
                    "actual_filename": actual_filename,
                    "actual_ext": actual_ext,
                }, ensure_ascii=False)

            # 复用 O_EXCL 创建的占位 fd 直接写入（os.fdopen 接管 fd，with 退出时关闭）
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                    elapsed = time.time() - start_time
                    if elapsed > total_timeout:
                        return json.dumps({
                            "success": False,
                            "error": f"下载超时（{total_timeout}s），文件可能过大或网络不稳定",
                            "downloaded": f"{downloaded / 1024 / 1024:.2f} MB",
                        }, ensure_ascii=False)

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
        # 关闭占位 fd（若尚未被 os.fdopen 接管），并清理不完整文件
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if save_path.exists():
            save_path.unlink()
        return json.dumps({"success": False, "error": f"下载超时（{total_timeout}s），文件可能过大或网络不稳定"}, ensure_ascii=False)
    except Exception as e:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if save_path.exists():
            save_path.unlink()
        return json.dumps({"success": False, "error": f"下载失败: {str(e)[:200]}"}, ensure_ascii=False)


@register_tool
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
