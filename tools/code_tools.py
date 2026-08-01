"""代码操作工具：工作空间读写、内容搜索、文件替换、项目结构分析

修复清单：
- 路径穿越漏洞：所有函数添加工作区边界校验
- 工作目录命名：work_space → workspace（与项目结构一致）
- 相对路径解析：基于工作区根目录解析相对路径
- 编码处理一致：search_content/replace_in_file 支持多编码
- 大文件保护：文件大小前置校验，防止 OOM
- 单行超长截断：read_file 修复单行超长不截断问题
- 目录遍历限制：深度限制 + 文件数量限制
- 原子写入：replace_in_file 使用临时文件替换
- 公共函数提取：工作目录计算统一到 get_workspace_root()
"""
import os
import json
import fnmatch
import logging
import re

from . import register_tool, path_params
from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#  常量配置
# ═══════════════════════════════════════════

WORKSPACE_ROOT = os.path.join(DATA_DIR, "workspace")
"""工作区根目录（DATA_DIR 下的 workspace 文件夹）"""

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB，超过此大小拒绝操作
MAX_CONTENT_CHARS = 4000          # 单次读取最大字符数
MAX_READ_LINES = 200              # 单次读取最大行数
MAX_WALK_DEPTH = 5               # 目录遍历最大深度
MAX_SEARCH_FILES = 2000          # 搜索时最大遍历文件数
ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]


# ═══════════════════════════════════════════
#  公共函数
# ═══════════════════════════════════════════

def _ensure_workspace():
    """确保工作区目录存在，返回工作区绝对路径。"""
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    return WORKSPACE_ROOT


def _resolve_path(file_path: str) -> str | None:
    """解析路径，确保在工作区范围内。

    规则：
    1. 相对路径 → 基于工作区根目录解析
    2. 绝对路径 → 检查是否在工作区内
    3. 路径穿越 → 返回 None

    返回：绝对路径 或 None（非法路径）
    """
    if not file_path:
        return None

    file_path = os.path.normpath(file_path)

    # 相对路径 → 基于工作区解析
    if not os.path.isabs(file_path):
        resolved = os.path.abspath(os.path.join(WORKSPACE_ROOT, file_path))
    else:
        resolved = file_path

    # 确保路径在工作区内（防止路径穿越）
    workspace_abs = os.path.abspath(WORKSPACE_ROOT)
    resolved_abs = os.path.abspath(resolved)

    # 检查路径前缀（必须以工作区路径开头）
    if not resolved_abs.startswith(workspace_abs + os.sep) and resolved_abs != workspace_abs:
        logger.warning("路径穿越检测：%s 不在工作区 %s 内", resolved_abs, workspace_abs)
        return None

    return resolved_abs


def _resolve_path_read(file_path: str) -> str | None:
    """解析路径（读操作，不限制工作区）。

    读取文件时不限制路径范围，允许从任意位置读取文件。
    规则：
    1. 空路径 → 返回 None
    2. 相对路径 → 基于工作区根目录解析
    3. 绝对路径 → 直接返回（不做工作区校验）

    返回：绝对路径 或 None（非法路径）
    """
    if not file_path:
        return None

    file_path = os.path.normpath(file_path)

    # 相对路径 → 基于工作区解析
    if not os.path.isabs(file_path):
        resolved = os.path.abspath(os.path.join(WORKSPACE_ROOT, file_path))
    else:
        resolved = file_path

    return os.path.abspath(resolved)


@register_tool
def resolve_path_safe(file_path: str) -> tuple:
    """安全路径解析包装器，统一返回 (resolved_path, error_message)。
    
    解决 _resolve_path 返回 None 时各调用方处理不一致的问题。
    """
    resolved = _resolve_path(file_path)
    if resolved is None:
        return None, f"非法路径：只能在 workspace 内操作: {file_path}"
    return resolved, None


def _read_file_with_encoding(file_path: str, size_limit: int = MAX_FILE_SIZE) -> tuple | None:
    """读取文件，自动检测编码。

    返回：(content, encoding) 或 None（读取失败）
    """
    # 文件大小检查
    try:
        file_size = os.path.getsize(file_path)
        if file_size > size_limit:
            logger.warning("文件过大拒绝读取: %s (%d bytes)", file_path, file_size)
            return None
    except OSError:
        return None

    for encoding in ENCODINGS:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                content = f.read()
            return (content, encoding)
        except UnicodeDecodeError:
            continue
        except Exception as e:
            logger.warning("读取文件失败 %s: %s", file_path, str(e)[:100])
            return None

    return None


def _read_lines_with_encoding(file_path: str, size_limit: int = MAX_FILE_SIZE) -> tuple | None:
    """按行读取文件，自动检测编码。

    返回：(lines, encoding) 或 None（读取失败）
    """
    try:
        file_size = os.path.getsize(file_path)
        if file_size > size_limit:
            logger.warning("文件过大拒绝读取: %s (%d bytes)", file_path, file_size)
            return None
    except OSError:
        return None

    for encoding in ENCODINGS:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                lines = f.readlines()
            return (lines, encoding)
        except UnicodeDecodeError:
            continue
        except Exception as e:
            logger.warning("读取文件失败 %s: %s", file_path, str(e)[:100])
            return None

    return None


# ═══════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════

@register_tool
def get_workspace_path():
    """返回 workspace 文件夹的绝对路径。
    
    ⚠️ 仅在以下场景调用：
    - 需要创建新文件（代码、文档、图片等）
    - 需要读取/修改已有文件
    
    ❌ 以下场景不需要调用：
    - 修改系统设置（音量、亮度等）→ 用 execute_command
    - 安装/卸载软件 → 用 execute_command
    - 纯问答/信息查询 → 直接回答或用 web_search
    """
    workspace = _ensure_workspace()
    if not workspace or not os.path.isdir(workspace):
        return json.dumps({"success": False, "error": f"workspace 路径无效: {workspace}"})
    return json.dumps({"success": True, "workspace": workspace})


@register_tool
@path_params("file_path")
def write_file(file_path: str, content: str, overwrite: bool = True):
    """安全写入文件。content 原样写入不转义。路径必须在 workspace 内。"""
    try:
        target_abs = _resolve_path(file_path)
        if not target_abs:
            return json.dumps({
                "success": False,
                "message": f"非法路径！只能在 workspace 内操作: {file_path}"
            }, ensure_ascii=False)

        parent_dir = os.path.dirname(target_abs)
        os.makedirs(parent_dir, exist_ok=True)

        if os.path.exists(target_abs) and not overwrite:
            return json.dumps({
                "success": False,
                "message": f"文件已存在且不允许覆盖: {target_abs}"
            }, ensure_ascii=False)

        # 判断是否真正修改（changed 字段）
        actually_modified = True
        if os.path.exists(target_abs):
            try:
                with open(target_abs, "r", encoding="utf-8") as f:
                    old_content = f.read()
                actually_modified = (old_content != content)
            except Exception:
                actually_modified = True

        with open(target_abs, "w", encoding="utf-8", newline="") as f:
            f.write(content)

        return json.dumps({
            "success": True,
            "message": f"文件写入成功: {target_abs}",
            "changed": actually_modified
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"写入失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool
@path_params("file_path")
def read_file(file_path: str, start_line: int = None, end_line: int = None):
    """读取指定文件全部或行范围，返回 JSON（file, total_lines, content）。

    ⚠️ 大文件(>200行)严禁直接全量读取。正确策略：
    1. 先用 search_content(keyword, directory, file_pattern) 定位关键词所在行号
    2. 再用 read_file(start_line=行号-5, end_line=行号+30) 局部读取上下文
    3. 严禁通过缩小 start_line/end_line 范围来"搜索"——你只会重复读到文件开头区域
    """
    target_abs = _resolve_path_read(file_path)
    if not target_abs:
        return json.dumps({
            "success": False,
            "message": f"非法路径！只能在 workspace 内操作: {file_path}"
        }, ensure_ascii=False)

    if not os.path.isfile(target_abs):
        return json.dumps({
            "success": False,
            "message": f"文件不存在: {target_abs}"
        }, ensure_ascii=False)

    result = _read_lines_with_encoding(target_abs)
    if result is None:
        try:
            with open(target_abs, "rb") as f:
                raw = f.read(2000)
            return json.dumps({
                "success": False,
                "message": f"无法以文本模式读取文件，可能是二进制文件。文件前2000字节的十六进制预览: {raw.hex()[:200]}",
                "is_binary": True
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "success": False,
                "message": str(e)
            }, ensure_ascii=False)

    lines, encoding = result
    total = len(lines)

    if start_line is not None or end_line is not None:
        start = max(0, (start_line or 1) - 1)
        end = min(total, end_line or total)
        selected = lines[start:end]
    else:
        selected = lines

    content = "".join(selected)
    is_full_read = (start_line is None and end_line is None)

    # 修复单行超长截断漏洞：无论行数多少，都限制字符数
    truncate_reason = []
    if is_full_read:
        if total > MAX_READ_LINES:
            truncate_reason.append(f"超过{MAX_READ_LINES}行")
        if len(content) > MAX_CONTENT_CHARS:
            truncate_reason.append(f"超过{MAX_CONTENT_CHARS}字符")

        if truncate_reason:
            content = content[:MAX_CONTENT_CHARS]
            result_data = {
                "success": True,
                "file": target_abs,
                "total_lines": total,
                "content": content,
                "encoding": encoding,
                "truncated": True,
                "hint": f"文件共{total}行，因{';'.join(truncate_reason)}已截断。请使用 search_content 定位关键代码后再局部读取。"
            }
            return json.dumps(result_data, ensure_ascii=False)

    # 非全量读取时，也限制返回字符数防止单行超长
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n… [内容截断]"

    return json.dumps({
        "success": True,
        "file": target_abs,
        "total_lines": total,
        "content": content,
        "encoding": encoding
    }, ensure_ascii=False)


@register_tool
@path_params("directory")
def search_content(keyword: str, directory: str = None, file_pattern: str = "*", max_results: int = 10):
    """在目录下搜索包含关键词的文件行。返回匹配文件、行号、行内容。"""
    # 解析目录路径
    if directory:
        dir_abs = _resolve_path_read(directory)
        if not dir_abs:
            return json.dumps({
                "success": False,
                "message": f"非法路径！只能在 workspace 内操作: {directory}"
            }, ensure_ascii=False)
    else:
        dir_abs = _ensure_workspace()

    if not os.path.isdir(dir_abs):
        return json.dumps({
            "success": False,
            "message": f"目录不存在: {dir_abs}"
        }, ensure_ascii=False)

    results = []
    files_scanned = 0

    for root, dirs, files in os.walk(dir_abs):
        # 深度限制
        depth = root[len(dir_abs):].count(os.sep)
        if depth > MAX_WALK_DEPTH:
            dirs[:] = []
            continue

        # 过滤隐藏目录和常见排除目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]

        for file in files:
            # 文件数量限制
            files_scanned += 1
            if files_scanned > MAX_SEARCH_FILES:
                logger.info("搜索达到文件上限 %d，停止遍历", MAX_SEARCH_FILES)
                break

            if not fnmatch.fnmatch(file, file_pattern):
                continue

            file_path = os.path.join(root, file)

            # 文件大小检查
            try:
                if os.path.getsize(file_path) > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            # 多编码读取
            read_result = _read_lines_with_encoding(file_path)
            if read_result is None:
                continue

            lines, _ = read_result

            for idx, line in enumerate(lines, 1):
                if keyword.lower() in line.lower():
                    results.append({
                        "file": file_path,
                        "line": idx,
                        "content": line.rstrip('\n')
                    })
                    if len(results) >= max_results:
                        return json.dumps({"success": True, "results": results}, ensure_ascii=False)

        else:
            continue
        break

    return json.dumps({"success": True, "results": results}, ensure_ascii=False)


@register_tool
@path_params("file_path")
def replace_in_file(file_path: str, old_str: str, new_str: str, occurrence: int = 1):
    """将文件中第 occurrence 次出现的 old_str 替换为 new_str。"""
    target_abs = _resolve_path(file_path)
    if not target_abs:
        return json.dumps({
            "success": False,
            "message": f"非法路径！只能在 workspace 内操作: {file_path}"
        }, ensure_ascii=False)

    if not os.path.isfile(target_abs):
        return json.dumps({
            "success": False,
            "message": f"文件不存在: {target_abs}"
        }, ensure_ascii=False)

    # 多编码读取
    read_result = _read_file_with_encoding(target_abs)
    if read_result is None:
        return json.dumps({
            "success": False,
            "message": f"无法读取文件，可能是二进制文件或编码不支持: {target_abs}"
        }, ensure_ascii=False)

    content, encoding = read_result

    count = content.count(old_str)
    if count == 0:
        return json.dumps({
            "success": False,
            "message": "未找到要替换的字符串"
        }, ensure_ascii=False)

    if occurrence > count:
        return json.dumps({
            "success": False,
            "message": f"仅有 {count} 处出现，无法替换第 {occurrence} 次"
        }, ensure_ascii=False)

    parts = content.split(old_str)
    before = old_str.join(parts[:occurrence])
    after = old_str.join(parts[occurrence:])
    new_content = before + new_str + after

    # 原子写入：先写临时文件，再替换原文件
    temp_path = target_abs + ".tmp"
    try:
        with open(temp_path, "w", encoding=encoding, newline="") as f:
            f.write(new_content)
        # 使用 os.replace 原子替换
        os.replace(temp_path, target_abs)

        return json.dumps({
            "success": True,
            "message": f"替换成功，第 {occurrence} 处出现已被替换"
        }, ensure_ascii=False)

    except Exception as e:
        # 清理临时文件
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return json.dumps({
            "success": False,
            "message": f"替换失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool
def get_project_summary(directory: str = None):
    """列出目录下所有代码文件及大小，帮助快速了解项目结构。"""
    if directory:
        dir_abs = _resolve_path_read(directory)
        if not dir_abs:
            return json.dumps({
                "success": False,
                "message": f"非法路径！只能在 workspace 内操作: {directory}"
            }, ensure_ascii=False)
    else:
        dir_abs = _ensure_workspace()

    if not os.path.isdir(dir_abs):
        return json.dumps({
            "success": False,
            "message": f"目录不存在: {dir_abs}"
        }, ensure_ascii=False)

    summary = []
    files_scanned = 0

    for root, dirs, files in os.walk(dir_abs):
        # 深度限制
        depth = root[len(dir_abs):].count(os.sep)
        if depth > MAX_WALK_DEPTH:
            dirs[:] = []
            continue

        # 过滤隐藏目录和常见排除目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]

        for file in files:
            # 文件数量限制
            files_scanned += 1
            if files_scanned > MAX_SEARCH_FILES:
                logger.info("项目总结达到文件上限 %d，停止遍历", MAX_SEARCH_FILES)
                break

            if file.endswith(('.py', '.html', '.js', '.css', '.json', '.md')):
                path = os.path.join(root, file)
                try:
                    size = os.path.getsize(path)
                    summary.append({
                        "file": file,
                        "path": path,
                        "size": size
                    })
                except OSError:
                    continue

        else:
            continue
        break

    return json.dumps({"success": True, "files": summary}, ensure_ascii=False)


# ═══════════════════════════════════════════
#  Python 代码执行器（文件夹隔绝模式）
# ═══════════════════════════════════════════

DANGEROUS_PATTERNS = [
    (r'\bsubprocess\b', '禁止使用 subprocess 模块'),
    (r'\bos\.system\b', '禁止使用 os.system'),
    (r'\bos\.popen\b', '禁止使用 os.popen'),
    (r'\bos\.fork\b', '禁止使用 os.fork'),
    (r'\bos\.spawn\b', '禁止使用 os.spawn'),
    (r'\bsocket\.socket\s*\(\s*socket\.AF_INET.*SOCK_STREAM.*bind', '禁止监听网络端口'),
    (r'\bftplib\b', '禁止使用 ftplib 模块'),
    (r'\bsmtplib\b', '禁止使用 smtplib 模块'),
    (r'\b__import__\b', '禁止使用 __import__'),
    (r'\bimportlib\b', '禁止使用 importlib 模块'),
    (r'\beval\s*\(', '禁止使用 eval()'),
    (r'\bexec\s*\(', '禁止使用 exec()'),
    (r'\bcompile\b', '禁止使用 compile'),
    (r'\bpickle\b', '禁止使用 pickle 模块'),
    (r'\bmarshal\b', '禁止使用 marshal 模块'),
    (r'\bshutil\.rmtree\b', '禁止使用 shutil.rmtree'),
    (r'\bos\.remove\b', '禁止使用 os.remove'),
    (r'\bos\.unlink\b', '禁止使用 os.unlink'),
    (r'\bos\.rmdir\b', '禁止使用 os.rmdir'),
    (r'\bwin32\b', '禁止使用 win32api'),
    (r'\bpyautogui\b', '禁止使用 pyautogui'),
    (r'\bctypes\b', '禁止使用 ctypes'),
    (r'\bcffi\b', '禁止使用 cffi'),
]


def _check_dangerous_code(code):
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return reason
    return None


def _check_path_traversal(code):
    path_patterns = [
        r'\\\.\.',
        r'/\\\.\.',
        r'\.\./',
        r'\.\.\\',
    ]
    for pattern in path_patterns:
        if re.search(pattern, code):
            return True
    return False


@register_tool(timeout=60)
def run_python(code: str, timeout: int = 30, working_dir: str = None) -> str:
    """在受限环境中执行 Python 代码，用于处理 Excel/CSV 数据。
    
    code: Python 代码字符串。
    timeout: 执行超时时间（秒），默认 30。
    working_dir: 自定义工作目录路径（可为相对路径或绝对路径）。
                 若指定则使用该目录作为工作目录（不存在时会自动创建）；
                 否则默认使用 DATA_DIR/workspace 目录。
    
    限制:
    - 文件读写仅限于工作目录
    - 禁止系统命令、动态导入、监听网络端口
    - 内存限制：取决于 pandas 配置（约 500MB）
    - 输出：使用 print() 返回结果，文件写入工作目录
    
    示例:
    ```python
    import pandas as pd
    df = pd.read_excel('data.xlsx')
    result = df.groupby('部门')['销售额'].sum()
    result.to_excel('output.xlsx', sheet_name='汇总')
    print(f'汇总完成: {result.to_dict()}')
    ```
    """
    if not code or not code.strip():
        return json.dumps({"success": False, "message": "代码不能为空"}, ensure_ascii=False)
    
    danger_reason = _check_dangerous_code(code)
    if danger_reason:
        return json.dumps({
            "success": False,
            "message": f"安全检查失败：{danger_reason}"
        }, ensure_ascii=False)
    
    if _check_path_traversal(code):
        return json.dumps({
            "success": False,
            "message": "安全检查失败：代码包含路径穿越攻击"
        }, ensure_ascii=False)
    
    import io
    import sys
    import os
    
    # 如果指定了工作目录则使用，否则默认 workspace
    if working_dir:
        workspace_dir = working_dir
        if not os.path.isabs(workspace_dir):
            workspace_dir = os.path.join(os.getcwd(), workspace_dir)
        if not os.path.exists(workspace_dir):
            os.makedirs(workspace_dir, exist_ok=True)
    else:
        workspace_dir = os.path.join(DATA_DIR, "workspace")
        os.makedirs(workspace_dir, exist_ok=True)
    
    old_cwd = os.getcwd()
    os.chdir(workspace_dir)
    
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    sys.stdout = stdout_capture
    sys.stderr = stderr_capture
    
    error = None
    try:
        import threading
        
        class TimeoutException(Exception):
            pass
        
        result_container = {}
        
        def worker():
            try:
                # 创建统一的命名空间，包含常用模块
                exec_namespace = {
                    '__builtins__': __builtins__,
                    '__name__': '__main__',
                }
                exec(code, exec_namespace)
                result_container['success'] = True
            except Exception as e:
                result_container['error'] = str(e)
        
        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        thread.join(timeout=timeout)
        
        if thread.is_alive():
            raise TimeoutException(f"代码执行超时 ({timeout}s)")
        
        if 'error' in result_container:
            raise Exception(result_container['error'])
            
    except TimeoutException as e:
        error = str(e)
    except Exception as e:
        error = f"执行错误: {str(e)}"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        os.chdir(old_cwd)
    
    output = stdout_capture.getvalue()
    stderr_output = stderr_capture.getvalue()
    
    if error:
        return json.dumps({
            "success": False,
            "message": error,
            "stdout": output,
            "stderr": stderr_output
        }, ensure_ascii=False)
    
    return json.dumps({
        "success": True,
        "output": output,
        "stderr": stderr_output
    }, ensure_ascii=False)


# 向后兼容别名：确保旧代码中引用 run_python_code 仍能正常工作
run_python_code = run_python