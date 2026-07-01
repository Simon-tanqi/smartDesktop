"""
测试脚本：验证三个新问题的修复
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import skill_learning


def test_fallback_skill_has_keywords():
    """测试 _generate_fallback_skill 包含 YAML keywords"""
    print("=" * 60)
    print("Test 1: _generate_fallback_skill has YAML keywords")
    print("=" * 60)
    
    task_info = {
        "task_description": "帮我处理这个Excel文件，统计每个部门的人数",
        "tools_used": ["read_file", "run_python"]
    }
    
    content = skill_learning._generate_fallback_skill(task_info)
    
    print("\nGenerated content:")
    print("-" * 40)
    print(content)
    print("-" * 40)
    
    # 验证 YAML frontmatter 包含 keywords
    assert "keywords:" in content, "Missing keywords field in YAML frontmatter"
    assert content.startswith("---"), "Should start with ---"
    
    # 验证包含 KEYWORDS 行
    assert "KEYWORDS:" in content, "Missing KEYWORDS line"
    
    # 验证可以提取关键词
    keywords = skill_learning._extract_keywords_from_skill(content)
    print(f"\nExtracted keywords: {keywords}")
    
    assert len(keywords) >= 3, f"Expected >= 3 keywords, got {len(keywords)}"
    
    print("\nPASS: _generate_fallback_skill contains YAML keywords")
    return True


def test_yaml_frontmatter_regex():
    """测试 YAML frontmatter 正则避免正文 --- 误匹配"""
    print("\n" + "=" * 60)
    print("Test 2: YAML frontmatter regex (avoid --- in body)")
    print("=" * 60)
    
    # 测试用例：正文包含 --- 分隔线
    content_with_dash_in_body = """---
skill_name: test_skill
version: 1.0
created_at: 2026-06-30
keywords:
  - 关键词1
  - 关键词2
---

KEYWORDS: 关键词1, 关键词2

# 测试技能

## 流程

下面是代码示例：
```
some code
---
another code
```

## 注意事项

---
这是分隔线
---

结束
"""
    
    keywords = skill_learning._extract_keywords_from_skill(content_with_dash_in_body)
    print(f"Extracted keywords: {keywords}")
    
    # 应该只提取到 2 个关键词，而不是更多（正文中的 --- 不应被误匹配）
    assert len(keywords) == 2, f"Expected 2 keywords, got {len(keywords)}"
    assert "关键词1" in keywords, "Missing expected keyword 1"
    assert "关键词2" in keywords, "Missing expected keyword 2"
    
    print("\nPASS: YAML frontmatter regex correctly ignores --- in body")
    return True


def test_build_context_messages_limit():
    """测试 build_context_messages 默认 limit 值"""
    print("\n" + "=" * 60)
    print("Test 3: build_context_messages default limit")
    print("=" * 60)
    
    import inspect
    from memory.session import build_context_messages
    
    sig = inspect.signature(build_context_messages)
    limit_param = sig.parameters.get('limit')
    
    if limit_param:
        default_limit = limit_param.default
        print(f"Default limit value: {default_limit}")
        
        # 验证默认值已修改为 150
        assert default_limit == 150, f"Expected limit=150, got limit={default_limit}"
        print("\nPASS: Default limit is now 150 (was 500)")
        return True
    else:
        print("FAIL: limit parameter not found")
        return False


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Testing Fixes for New Issues")
    print("=" * 60 + "\n")
    
    test1 = test_fallback_skill_has_keywords()
    test2 = test_yaml_frontmatter_regex()
    test3 = test_build_context_messages_limit()
    
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"Test 1 (_generate_fallback_skill): {'PASS' if test1 else 'FAIL'}")
    print(f"Test 2 (YAML regex): {'PASS' if test2 else 'FAIL'}")
    print(f"Test 3 (build_context_messages limit): {'PASS' if test3 else 'FAIL'}")
    
    if test1 and test2 and test3:
        print("\nAll tests PASSED!")
    else:
        print("\nSome tests FAILED.")
