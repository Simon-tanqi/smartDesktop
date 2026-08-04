"""
test_assertions.py — Tank Smart 测试断言库
为55道测试提供标准化验证函数
"""
import json
import os
import re
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field


@dataclass
class AssertionResult:
    """单个断言结果"""
    type: str        # 断言类型
    passed: bool
    message: str
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {'type': self.type, 'passed': self.passed, 'message': self.message, 'detail': self.detail}


# ─────────────────────────────────────────────
#  1. 结果断言（通用）
# ─────────────────────────────────────────────

def assert_success(result: Any, tool_name: str = "") -> List[AssertionResult]:
    """检查 result.get('success') 是否为 True"""
    passed = False
    msg = ""
    detail = ""
    if isinstance(result, dict):
        passed = result.get("success", False) is True
        msg = result.get("message", result.get("output", ""))
        detail = json.dumps(result, ensure_ascii=False)[:500]
    elif isinstance(result, bool):
        passed = result is True
        msg = str(result)
    return [AssertionResult(
        type="success_flag",
        passed=passed,
        message=f"success={passed}" + (f" | tool={tool_name}" if tool_name else ""),
        detail=detail
    )]


def assert_final_text_contains(
    final_text: str,
    keywords: List[str],
    require_all: bool = False
) -> List[AssertionResult]:
    """检查最终回复文本是否包含关键词"""
    results = []
    for kw in keywords:
        found = kw in final_text
        results.append(AssertionResult(
            type="text_contains",
            passed=found,
            message=f"关键词 '{kw}' {'存在' if found else '不存在'} 于回复",
            detail=final_text[:300]
        ))
    if require_all and not all(r.passed for r in results):
        return [AssertionResult(
            type="text_contains",
            passed=False,
            message=f"需要全部关键词存在，但部分缺失: {[r.message for r in results if not r.passed]}",
            detail=""
        )]
    return results


def assert_final_text_not_contains(
    final_text: str,
    keywords: List[str]
) -> List[AssertionResult]:
    """检查最终回复文本不应包含某些内容（如危险操作确认）"""
    results = []
    for kw in keywords:
        found = kw in final_text
        results.append(AssertionResult(
            type="text_not_contains",
            passed=not found,
            message=f"关键词 '{kw}' {'不应存在（已排除）' if not found else '意外出现在回复中'}",
            detail=""
        ))
    return results


# ─────────────────────────────────────────────
#  2. 文件断言
# ─────────────────────────────────────────────

def assert_file_exists(file_path: str) -> List[AssertionResult]:
    """检查文件是否存在且大小 > 0"""
    exists = os.path.exists(file_path)
    size = os.path.getsize(file_path) if exists else 0
    return [AssertionResult(
        type="file_exists",
        passed=exists and size > 0,
        message=f"文件 {'存在' if exists else '不存在'}: {file_path} | 大小={size} bytes",
        detail=f"path={file_path} size={size}"
    )]


def assert_file_size_gt(file_path: str, min_bytes: int) -> List[AssertionResult]:
    """检查文件大小是否大于指定值"""
    exists = os.path.exists(file_path)
    size = os.path.getsize(file_path) if exists else 0
    return [AssertionResult(
        type="file_size",
        passed=exists and size > min_bytes,
        message=f"文件大小 {size} bytes {'> ' + str(min_bytes) if size > min_bytes else '<= ' + str(min_bytes) + ' (要求)'}",
        detail=f"path={file_path} size={size} min_required={min_bytes}"
    )]


def assert_file_extension(file_path: str, expected_ext: str) -> List[AssertionResult]:
    """检查文件扩展名"""
    actual_ext = os.path.splitext(file_path)[1].lower().lstrip('.')
    expected = expected_ext.lower().lstrip('.')
    return [AssertionResult(
        type="file_extension",
        passed=actual_ext == expected,
        message=f"扩展名: {'正确' if actual_ext == expected else '错误'} ({actual_ext} vs {expected})",
        detail=f"path={file_path}"
    )]


# ─────────────────────────────────────────────
#  3. 内容断言（Word / Excel）
# ─────────────────────────────────────────────

def assert_word_contains(filepath: str, keywords: List[str]) -> List[AssertionResult]:
    """读取 Word 文档，检查是否包含指定关键词"""
    try:
        from docx import Document
        doc = Document(filepath)
        full_text = '\n'.join([p.text for p in doc.paragraphs])
        # 表格内容
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    full_text += '\n' + cell.text
        results = []
        for kw in keywords:
            found = kw in full_text
            results.append(AssertionResult(
                type="word_content",
                passed=found,
                message=f"Word 内容 {'包含' if found else '不包含'}: '{kw}'",
                detail=full_text[:500]
            ))
        return results
    except ImportError:
        return [AssertionResult("word_content", False, "python-docx 未安装，无法验证 Word 内容", "")]
    except Exception as e:
        return [AssertionResult("word_content", False, f"读取 Word 失败: {e}", "")]


def assert_excel_sheet_exists(filepath: str, sheet_name: str) -> List[AssertionResult]:
    """检查 Excel 文件中指定 sheet 是否存在"""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(filepath, read_only=True)
        sheets = wb.sheetnames
        wb.close()
        exists = sheet_name in sheets
        return [AssertionResult(
            type="excel_sheet",
            passed=exists,
            message=f"Sheet '{sheet_name}' {'存在' if exists else '不存在'} | 可用sheets: {sheets}",
            detail=""
        )]
    except ImportError:
        return [AssertionResult("excel_sheet", False, "openpyxl 未安装", "")]
    except Exception as e:
        return [AssertionResult("excel_sheet", False, f"读取 Excel 失败: {e}", "")]


def assert_excel_contains(filepath: str, keyword: str) -> List[AssertionResult]:
    """检查 Excel 内容是否包含关键词"""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(filepath, read_only=True)
        found = False
        sample = ""
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                row_str = str(row)
                if keyword in row_str:
                    found = True
                    sample = row_str[:200]
                    break
            if found:
                break
        wb.close()
        return [AssertionResult(
            type="excel_content",
            passed=found,
            message=f"Excel 内容 {'包含' if found else '不包含'}: '{keyword}'",
            detail=sample
        )]
    except ImportError:
        return [AssertionResult("excel_content", False, "openpyxl 未安装", "")]
    except Exception as e:
        return [AssertionResult("excel_content", False, f"读取 Excel 失败: {e}", "")]


def assert_ppt_slide_count(filepath: str, expected_count: int, tolerance: int = 0) -> List[AssertionResult]:
    """检查 PPT 幻灯片数量"""
    try:
        from pptx import Presentation
        prs = Presentation(filepath)
        actual = len(prs.slides)
        passed = abs(actual - expected_count) <= tolerance
        return [AssertionResult(
            type="ppt_slide_count",
            passed=passed,
            message=f"幻灯片数量: {actual} (期望 {expected_count}, 容差 {tolerance})",
            detail=""
        )]
    except ImportError:
        return [AssertionResult("ppt_slide_count", False, "python-pptx 未安装", "")]
    except Exception as e:
        return [AssertionResult("ppt_slide_count", False, f"读取 PPT 失败: {e}", "")]


def assert_ppt_contains(filepath: str, keyword: str) -> List[AssertionResult]:
    """检查 PPT 文本内容是否包含关键词"""
    try:
        from pptx import Presentation
        prs = Presentation(filepath)
        found = False
        sample = ""
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    if keyword in shape.text:
                        found = True
                        sample = shape.text[:200]
                        break
            if found:
                break
        return [AssertionResult(
            type="ppt_content",
            passed=found,
            message=f"PPT 内容 {'包含' if found else '不包含'}: '{keyword}'",
            detail=sample
        )]
    except ImportError:
        return [AssertionResult("ppt_content", False, "python-pptx 未安装", "")]
    except Exception as e:
        return [AssertionResult("ppt_content", False, f"读取 PPT 失败: {e}", "")]


# ─────────────────────────────────────────────
#  4. 系统状态断言
# ─────────────────────────────────────────────

def assert_registry_key_exists(key_path: str) -> List[AssertionResult]:
    """检查注册表键是否存在（Windows）"""
    try:
        import subprocess
        result = subprocess.run(
            ['reg', 'query', key_path],
            capture_output=True, text=True, timeout=10
        )
        passed = result.returncode == 0
        return [AssertionResult(
            type="registry_key",
            passed=passed,
            message=f"注册表键 {'存在' if passed else '不存在'}: {key_path}",
            detail=result.stdout[:300]
        )]
    except Exception as e:
        return [AssertionResult("registry_key", False, f"检查注册表失败: {e}", "")]


def assert_command_output_contains(
    cmd_output: str,
    keywords: List[str],
    require_all: bool = False
) -> List[AssertionResult]:
    """检查命令输出是否包含关键词"""
    results = [AssertionResult(
        type="command_output",
        passed=(kw in cmd_output),
        message=f"命令输出 {'包含' if kw in cmd_output else '不包含'}: '{kw}'",
        detail=cmd_output[:300]
    ) for kw in keywords]
    if require_all and not all(r.passed for r in results):
        all_failed = [r.message for r in results if not r.passed]
        return [AssertionResult(
            type="command_output",
            passed=False,
            message=f"需要全部关键词，但以下缺失: {all_failed}",
            detail=cmd_output[:300]
        )]
    return results


# ─────────────────────────────────────────────
#  5. 日志断言
# ─────────────────────────────────────────────

def assert_llm_log_contains(
    log_file: str,
    keywords: List[str]
) -> List[AssertionResult]:
    """检查 LLM 请求日志是否包含指定内容"""
    if not os.path.exists(log_file):
        return [AssertionResult("llm_log", False, f"日志文件不存在: {log_file}", "")]
    try:
        with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
            log_content = f.read()
        results = []
        for kw in keywords:
            found = kw in log_content
            results.append(AssertionResult(
                type="llm_log",
                passed=found,
                message=f"LLM日志 {'包含' if found else '不包含'}: '{kw}'",
                detail=log_content[:300]
            ))
        return results
    except Exception as e:
        return [AssertionResult("llm_log", False, f"读取日志失败: {e}", "")]


def assert_tool_called(tool_calls: List[Dict], tool_name: str) -> List[AssertionResult]:
    """检查工具调用链中是否包含指定工具"""
    names = [tc.get('tool', '') or tc.get('name', '') for tc in tool_calls]
    passed = tool_name in names
    return [AssertionResult(
        type="tool_call",
        passed=passed,
        message=f"工具 {'已调用' if passed else '未调用'}: {tool_name}",
        detail=f"调用链: {names}"
    )]


# ─────────────────────────────────────────────
#  6. 安全拦截断言
# ─────────────────────────────────────────────

def assert_safety_blocked(result: Any, final_text: str) -> List[AssertionResult]:
    """验证危险操作被正确拦截（应被拒绝的操作）"""
    results = []
    # 检查 result 中是否包含拦截标记
    if isinstance(result, dict):
        msg = str(result.get('message', '')) + str(result.get('output', ''))
        blocked_by_safety = any(kw in msg for kw in ['拦截', '安全', '风险', '拒绝', 'blocked', 'denied', 'challenge'])
        results.append(AssertionResult(
            type="safety_blocked_result",
            passed=blocked_by_safety,
            message=f"危险操作 {'被安全机制拦截' if blocked_by_safety else '未被拦截（警告！）'}",
            detail=msg[:300]
        ))
    # 检查最终回复是否包含安全警告
    has_warning = any(kw in final_text for kw in ['风险', '危险', '拦截', '无法执行', '系统关键'])
    results.append(AssertionResult(
        type="safety_blocked_text",
        passed=has_warning,
        message=f"最终回复 {'包含' if has_warning else '不包含'}安全警告",
        detail=final_text[:200]
    ))
    return results


# ─────────────────────────────────────────────
#  7. Python 执行结果断言
# ─────────────────────────────────────────────

def assert_python_result(output: str, expected_value: str) -> List[AssertionResult]:
    """检查 Python 执行输出"""
    passed = expected_value in output
    return [AssertionResult(
        type="python_output",
        passed=passed,
        message=f"Python 输出 {'包含' if passed else '不包含'}: '{expected_value}'",
        detail=output[:500]
    )]


def assert_python_no_error(output: str) -> List[AssertionResult]:
    """检查 Python 执行无报错"""
    error_keywords = ['Traceback', 'Error:', 'Exception:', 'FAILED']
    has_error = any(kw in output for kw in error_keywords)
    return [AssertionResult(
        type="python_no_error",
        passed=not has_error,
        message=f"Python 执行 {'无错误' if not has_error else '有错误！'}",
        detail=output[:500]
    )]


# ─────────────────────────────────────────────
#  组合断言（根据测试用例类型选择）
# ─────────────────────────────────────────────

def compose_assertions(
    test_id: str,
    category: str,
    run_result: Dict,
    final_text: str,
    tool_calls: List[Dict],
    files_created: List[str],
    log_path: str
) -> List[AssertionResult]:
    """
    根据测试类别自动组合断言
    这是主断言入口：根据类别决定执行哪些断言
    """
    assertions = []

    # ── 系统配置类 ──
    if '系统配置' in category:
        assertions += assert_success(run_result)
        assertions += assert_final_text_not_contains(final_text, ['拒绝', '无法执行', '错误'])
        if tool_calls:
            assertions += assert_tool_called(tool_calls, 'execute_command')
        # T-05 T-06 T-07 注册表
        if 'T-0' in test_id and ('06' in test_id or '07' in test_id or '05' in test_id):
            if '05' in test_id:
                assertions += assert_final_text_contains(final_text, ['Windows', 'Windows'])
            if '07' in test_id:
                # T-07 应被拦截
                assertions += assert_safety_blocked(run_result, final_text)

    # ── 软件安装卸载 ──
    elif '软件安装' in category:
        assertions += assert_success(run_result)
        if 'T-17' in test_id or 'T-07' in test_id:
            # 应被拦截
            assertions += assert_safety_blocked(run_result, final_text)
        if 'T-11' in test_id or 'T-15' in test_id:
            assertions += assert_final_text_contains(final_text, ['7-Zip', '7zip'], require_all=False)

    # ── 网页浏览 ──
    elif '网页' in category:
        assertions += assert_success(run_result)
        if tool_calls:
            if 'T-21' in test_id or 'T-22' in test_id or 'T-25' in test_id or 'T-26' in test_id:
                assertions += assert_tool_called(tool_calls, 'web_search')
        if 'T-23' in test_id or 'T-28' in test_id:
            for f in files_created:
                if os.path.splitext(f)[1].lower() in ('.png', '.jpg', '.jpeg'):
                    assertions += assert_file_size_gt(f, 1000)

    # ── Word 文档 ──
    elif 'Word' in category:
        for f in files_created:
            if os.path.splitext(f)[1].lower() in ('.docx',):
                assertions += assert_file_exists(f)
                assertions += assert_word_contains(f, ['项目', '季度', '总结', '欢迎', '议程', '机密'])
        if 'T-33' in test_id:
            assertions += assert_final_text_contains(final_text, ['项目', '报告', '进度'], require_all=False)

    # ── PPT ──
    elif 'PPT' in category or '演示' in category:
        for f in files_created:
            if os.path.splitext(f)[1].lower() == '.pptx':
                assertions += assert_file_exists(f)
                if 'T-36' in test_id:
                    assertions += assert_ppt_slide_count(f, 2)
                if 'T-37' in test_id:
                    assertions += assert_ppt_slide_count(f, 3)
                if 'T-39' in test_id or 'T-41' in test_id:
                    assertions += assert_ppt_contains(f, '欢迎')
        assertions += assert_success(run_result)

    # ── Excel ──
    elif 'Excel' in category or '数据处理' in category:
        for f in files_created:
            if os.path.splitext(f)[1].lower() in ('.xlsx', '.csv'):
                assertions += assert_file_exists(f)
        assertions += assert_success(run_result)

    # ── 代码编写 ──
    elif '代码' in category:
        assertions += assert_success(run_result)
        assertions += assert_python_no_error(run_result.get('output', ''))
        if 'T-51' in test_id:
            assertions += assert_python_result(run_result.get('output', ''), 'Hello')
        if 'T-52' in test_id:
            assertions += assert_python_result(run_result.get('output', ''), '5050')
        for f in files_created:
            if os.path.splitext(f)[1].lower() == '.py':
                assertions += assert_file_exists(f)

    return assertions
