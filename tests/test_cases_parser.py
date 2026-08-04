"""
test_cases_parser.py — Tank Smart 55道功能测试用例解析器
从 test_cases.md 提取测试用例，构建可执行测试列表
"""
import re
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TestCase:
    """单个测试用例结构"""
    id: str                  # T-01 ~ T-55
    category: str            # 类别名称
    title: str               # 测试标题
    user_input: str          # 用户输入（直接传给 Agent）
    expected_behavior: str    # 预期行为（原始文本，用于日志）
    verification: str        # 验证方式（原始文本）
    expected_result: str     # 预期结果状态（PASS / MANUAL_REVIEW / FAIL）
    prerequisites: Optional[str] = None


def _normalize_result(s: str) -> str:
    s = s.strip()
    if '拦截' in s or '❌' in s or 'FAIL' in s.upper() and ('拦截' in s or '高危' in s):
        return 'FAIL'
    if '需用户确认' in s or '⚠️' in s or 'MANUAL' in s.upper() or '用户确认' in s:
        return 'MANUAL_REVIEW'
    if '成功' in s or '✅' in s or 'PASS' in s.upper():
        return 'PASS'
    return 'PASS'


def _extract_field(block: str, field_name: str) -> Optional[str]:
    """从测试块中提取指定字段值"""
    # 字段格式：**字段名**：值 或 **字段名**：\n值
    pattern = rf'\*\*{re.escape(field_name)}\*\*[：:]\s*'
    m = re.search(pattern, block)
    if not m:
        return None
    start = m.end()
    # 下一个 **字段名** 之前的内容
    next_header = re.search(r'\n\*\*[^\n]+\*\*[：:]', block[start:])
    if next_header:
        end = start + next_header.start()
    else:
        end = len(block)
    value = block[start:end].strip()
    # 去掉开头的换行和列表符号
    value = re.sub(r'^[\n\r]+', '', value)
    return value if value else None


def parse_test_cases(md_path: str) -> List[TestCase]:
    """解析 test_cases.md，提取全部 55 道测试用例"""
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 按 ### T-XX 分割块
    # 每个块从 "### T-XX" 到下一个 "### T-" 之前
    section_headers = list(re.finditer(r'### T-(\d{2})', content))
    test_cases = []

    for i, match in enumerate(section_headers):
        start = match.start()
        end = section_headers[i + 1].start() if i + 1 < len(section_headers) else len(content)
        block = content[start:end]

        # 提取测试编号
        num = match.group(1)
        test_id = f"T-{num}"

        # 提取各字段
        category = _extract_field(block, '测试类别') or '未知类别'
        title = _extract_field(block, '测试标题') or '无标题'
        prerequisites = _extract_field(block, '前置条件')
        user_input = _extract_field(block, '用户输入') or ''
        expected_behavior = _extract_field(block, '预期行为') or ''
        verification = _extract_field(block, '验证方式') or ''
        expected_result_raw = _extract_field(block, '预期结果状态') or '✅ 成功'
        expected_result = _normalize_result(expected_result_raw)

        tc = TestCase(
            id=test_id,
            category=category,
            title=title,
            user_input=user_input,
            expected_behavior=expected_behavior,
            verification=verification,
            expected_result=expected_result,
            prerequisites=prerequisites,
        )
        test_cases.append(tc)

    return test_cases


def group_by_category(test_cases: List[TestCase]) -> dict:
    """按类别分组"""
    groups = {}
    for tc in test_cases:
        groups.setdefault(tc.category, []).append(tc)
    return groups


if __name__ == '__main__':
    import sys as _sys
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _sys.path.insert(0, proj)
    from tests.test_cases_parser import parse_test_cases
    cases = parse_test_cases(os.path.join(proj, 'tests', 'test_cases', 'test_cases.md'))
    print(f"解析到 {len(cases)} 道测试用例")
    for tc in cases:
        print(f"  {tc.id} | {tc.category[:14]:14s} | {tc.title[:22]}")
