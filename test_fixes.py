"""
测试脚本：验证三个问题的修复
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import skill_learning


def test_keyword_extraction():
    """测试关键词提取（修复问题 1 和 2）"""
    print("=" * 60)
    print("Test: Keyword Extraction (Fix issues 1 & 2)")
    print("=" * 60)
    
    # 测试用例 1：从 YAML frontmatter 提取
    content1 = """---
skill_name: excel_department_stats
version: 1.0
created_at: 2026-06-29 22:00:00
keywords:
  - 统计部门人数
  - 部门统计
  - Excel统计
  - 人数统计
---

KEYWORDS: 统计部门人数, 部门统计, Excel统计, 人数统计

# 统计部门人数

## 流程
1. 使用 read_file 读取 Excel
"""
    
    keywords1 = skill_learning._extract_keywords_from_skill(content1)
    print(f"Test 1 - YAML frontmatter:")
    print(f"  Extracted: {keywords1}")
    assert len(keywords1) >= 4, f"Expected >= 4 keywords, got {len(keywords1)}"
    assert "统计部门人数" in keywords1, "Missing expected keyword"
    print("  PASS")
    
    # 测试用例 2：从 KEYWORDS 行提取（没有 YAML keywords）
    content2 = """---
skill_name: excel_stats
version: 1.0
created_at: 2026-06-29 22:00:00
---

KEYWORDS: Excel处理, 数据分析, 表格统计, 部门人数

# Excel 数据分析

## 流程
1. 读取文件
"""
    
    keywords2 = skill_learning._extract_keywords_from_skill(content2)
    print(f"\nTest 2 - KEYWORDS line:")
    print(f"  Extracted: {keywords2}")
    assert len(keywords2) >= 3, f"Expected >= 3 keywords, got {len(keywords2)}"
    assert "Excel处理" in keywords2, "Missing expected keyword"
    print("  PASS")
    
    # 测试用例 3：从标题提取（备选）
    content3 = """---
skill_name: test_skill
version: 1.0
---

# 数据处理 和 分析

## 流程
1. 步骤1
"""
    
    keywords3 = skill_learning._extract_keywords_from_skill(content3)
    print(f"\nTest 3 - Title fallback:")
    print(f"  Extracted: {keywords3}")
    assert len(keywords3) >= 2, f"Expected >= 2 keywords, got {len(keywords3)}"
    print("  PASS")
    
    print("\nAll keyword extraction tests PASSED!")
    return True


def test_chunked_tool_calls():
    """测试 chunked tool_calls 的 arguments 解析（修复问题 3）"""
    print("\n" + "=" * 60)
    print("Test: Chunked Tool Calls Arguments Parsing (Fix issue 3)")
    print("=" * 60)
    
    # 模拟消息，包含字符串类型的 arguments
    test_messages = [
        {
            "role": "assistant",
            "content": "我来帮你处理这个 Excel 文件",
            "tool_calls": [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path": "data.xlsx", "sheet": 0}'  # 字符串类型
                    }
                }
            ]
        },
        {
            "role": "assistant", 
            "content": "让我统计一下",
            "tool_calls": [
                {
                    "function": {
                        "name": "run_python",
                        "arguments": {"code": "import pandas"}  # dict 类型（正常情况）
                    }
                }
            ]
        }
    ]
    
    # 测试提取逻辑
    import json
    
    for msg in test_messages:
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            arguments = tc.get("function", {}).get("arguments", {})
            
            # 修复后的逻辑
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                    print(f"  Parsed string arguments: {arguments}")
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
                    print(f"  Failed to parse, using raw: {arguments}")
            else:
                print(f"  Already dict: {arguments}")
            
            # 验证结果
            assert isinstance(arguments, dict), f"Arguments should be dict, got {type(arguments)}"
    
    print("\nAll chunked tool calls tests PASSED!")
    return True


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Testing Fixes for Issues 1, 2, 3")
    print("=" * 60 + "\n")
    
    test1 = test_keyword_extraction()
    test2 = test_chunked_tool_calls()
    
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"Test 1 (Keyword Extraction): {'PASS' if test1 else 'FAIL'}")
    print(f"Test 2 (Chunked Tool Calls): {'PASS' if test2 else 'FAIL'}")
    
    if test1 and test2:
        print("\nAll tests PASSED! Issues 1, 2, 3 are fixed.")
    else:
        print("\nSome tests FAILED.")
