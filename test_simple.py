"""
Simple test script: Verify skill learning implementation
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import skill_learning


def test_intent_detection():
    """Test intent detection (fix issue 3.5)"""
    print("=" * 60)
    print("Test 1: Intent Detection (fix issue 3.5)")
    print("=" * 60)
    
    test_cases = [
        # (user input, expected, description)
        ("把这次处理的经验内化为新技能", True, "Exact trigger word"),
        ("保存为技能", True, "Exact trigger word"),
        ("帮我处理这个 Excel 文件", False, "Normal task"),
        ("帮我把这次的发票整理一下", False, "Contains '把这次' but not trigger (key fix)"),
        ("将这次的经验保存", True, "Prefix trigger (start)"),
        ("学习这次处理方法", True, "Exact trigger word"),
    ]
    
    passed = 0
    failed = 0
    
    for user_input, expected, description in test_cases:
        result = skill_learning.is_skill_learning_request(user_input)
        status = "PASS" if result == expected else "FAIL"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"{status} | {description}")
        print(f"      Input: '{user_input}'")
        print(f"      Expected: {expected}, Actual: {result}")
        print()
    
    print(f"Intent detection: {passed} passed, {failed} failed")
    print()
    
    return failed == 0


def test_skill_save_and_register():
    """Test skill save and registration (complete lifecycle)"""
    print("=" * 60)
    print("Test 2: Skill Save and Registration (complete lifecycle)")
    print("=" * 60)
    
    # 1. Test save Skill
    skill_name = "test_excel_stats"
    content = """---
skill_name: test_excel_stats
version: 1.0
created_at: 2026-06-29 23:00:00
---

# Test Skill: Excel Stats

## Process
1. Use read_file to read Excel
2. Use run_python to analyze
3. Return results
"""
    
    save_result = skill_learning.save_skill(skill_name, content, version="1.0")
    print(f"Save result: {save_result['success']}")
    print(f"Message: {save_result['message']}")
    print(f"Version: {save_result['version']}")
    print()
    
    if not save_result['success']:
        print("FAIL: Skill save failed")
        return False
    
    # 2. Test registration to agent_tools.yaml
    register_result = skill_learning.register_skill_to_yaml(
        skill_name,
        description="Test skill: Excel stats",
        keywords=["stats", "excel", "department"],
        version="1.0"
    )
    print(f"Register result: {register_result['success']}")
    print(f"Message: {register_result['message']}")
    print()
    
    if not register_result['success']:
        print("FAIL: Skill registration failed")
        return False
    
    # 3. Test loading from agent_tools.yaml
    config = skill_learning.load_agent_tools_config()
    if skill_name in config.get("domains", {}):
        print(f"PASS: Skill successfully registered to agent_tools.yaml")
        print(f"   Domain: {skill_name}")
        print(f"   Description: {config['domains'][skill_name]['description']}")
        print(f"   Version: {config['domains'][skill_name]['version']}")
        print()
        return True
    else:
        print("FAIL: Skill not registered to agent_tools.yaml")
        return False


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Skill Learning Feature - Simple Test")
    print("=" * 60 + "\n")
    
    # Run tests
    test1 = test_intent_detection()
    test2 = test_skill_save_and_register()
    
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"Test 1 (Intent Detection): {'PASS' if test1 else 'FAIL'}")
    print(f"Test 2 (Save + Register): {'PASS' if test2 else 'FAIL'}")
    print()
    
    if test1 and test2:
        print("All tests passed! Skill learning feature implemented.")
    else:
        print("Some tests failed. Please check implementation.")
