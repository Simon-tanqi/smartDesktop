# -*- coding: utf-8 -*-
"""test_cases_parser.py - Tank Smart 55道功能测试用例解析器
从 docs/test_cases.md 提取测试用例，构建可执行测试列表。
"""
import re
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TestCase:
    """测试用例数据模型"""
    id: str                  # T-01 ~ T-55
    category: str            # 类别名称
    title: str               # 测试标题
    user_input: str          # 用户输入（直接传给 Agent）
    expected_behavior: str   # 预期行为（原始文本）
    verification: str        # 验证方式（原始文本）
    expected_result: str     # 标准化结果状态: PASS / MANUAL_REVIEW / FAIL
    prerequisites: Optional[str] = None


def _norm_result(s: str) -> str:
    """将预期结果状态标准化为 PASS / MANUAL_REVIEW / FAIL"""
    s = (s or "").strip()
    if "✅" in s or "成功" in s or "PASS" in s.upper():
        return "PASS"
    if "⚠️" in s or "确认" in s or "MANUAL" in s.upper() or "需用户" in s:
        return "MANUAL_REVIEW"
    if "❌" in s or "失败" in s or "拦截" in s or "FAIL" in s.upper():
        return "FAIL"
    return "PASS"


def parse_test_cases(md_path: str) -> List[TestCase]:
    """解析 test_cases.md，提取全部 55 道测试用例。"""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    headers = list(re.finditer(r"###\s+T-(\d+)", content))
    cases: List[TestCase] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(content)
        block = content[start:end]

        idm = re.search(r"\*\*测试编号\*\*[：:]\s*T-(\d+)", block)
        catm = re.search(r"\*\*测试类别\*\*[：:]\s*(.+)", block)
        titlem = re.search(r"\*\*测试标题\*\*[：:]\s*(.+)", block)
        prereqm = re.search(r"\*\*前置条件\*\*[：:]\s*(.+)", block)
        inputm = re.search(r"\*\*用户输入\*\*[：:]\s*(.+?)(?=\n\*\*预期行为|\Z)", block, re.DOTALL)
        behm = re.search(r"\*\*预期行为\*\*[：:]\s*(.+?)(?=\n\*\*验证方式|\Z)", block, re.DOTALL)
        verm = re.search(r"\*\*验证方式\*\*[：:]\s*(.+?)(?=\n\*\*预期结果状态|\Z)", block, re.DOTALL)
        resm = re.search(r"\*\*预期结果状态\*\*[：:]\s*(.+)", block)

        if not (idm and catm and titlem and inputm):
            continue

        tid = "T-" + idm.group(1).zfill(2)
        cases.append(TestCase(
            id=tid,
            category=catm.group(1).strip(),
            title=titlem.group(1).strip(),
            user_input=inputm.group(1).strip(),
            expected_behavior=behm.group(1).strip() if behm else "",
            verification=verm.group(1).strip() if verm else "",
            expected_result=_norm_result(resm.group(1) if resm else ""),
            prerequisites=prereqm.group(1).strip() if prereqm else None,
        ))
    return cases


CATEGORY_ORDER = [
    "系统配置更改",
    "软件安装卸载",
    "网页浏览",
    "Word文档处理",
    "PPT演示文稿",
    "Excel表格处理",
    "代码编写执行",
]


def group_by_category(cases: List[TestCase]) -> dict:
    """按类别分组测试用例"""
    groups = {}
    for c in cases:
        groups.setdefault(c.category, []).append(c)
    return groups


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    md = os.path.join(base, "docs", "test_cases.md")
    cases = parse_test_cases(md)
    print("解析到 %d 道测试用例" % len(cases))
    for c in cases:
        print("  %s | %s | %s | 预期=%s" % (c.id, c.category, c.title, c.expected_result))
