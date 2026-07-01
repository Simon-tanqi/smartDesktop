"""
测试 skill_learning 模块
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import skill_learning

# 测试意图识别
def test_intent_detection():
    test_cases = [
        ("把这次处理的经验内化为新技能", True),
        ("保存为技能", True),
        ("学习这次处理方法", True),
        ("帮我处理这个 Excel 文件", False),
        ("你好", False),
        ("learn from this session", True),
        ("save as skill", True),
    ]
    
    print("测试意图识别:")
    for message, expected in test_cases:
        result = skill_learning.is_skill_learning_request(message)
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] '{message}' -> {result} (expected: {expected})")

# 测试步骤提取
def test_step_extraction():
    test_messages = [
        {"role": "user", "content": "帮我处理这个 Excel 文件，统计每个部门的人数"},
        {"role": "assistant", "content": '{"tool_name": "read_file", "arguments": {"file_path": "data.xlsx"}}'},
        {"role": "user", "content": '工具 read_file 返回：文件内容...'},
        {"role": "assistant", "content": '{"tool_name": "run_python", "arguments": {"code": "import pandas as pd..."}}'},
        {"role": "user", "content": '工具 run_python 返回：处理结果...'},
        {"role": "assistant", "content": '{"final_message": "处理完成！统计结果已保存。"}'},
    ]
    
    print("\n测试步骤提取:")
    task_info = skill_learning.extract_task_steps_from_history(test_messages)
    print(f"  任务描述: {task_info['task_description']}")
    print(f"  使用工具: {task_info['tools_used']}")
    print(f"  步骤数量: {len(task_info['steps'])}")
    print(f"  摘要: {task_info['summary']}")

# 测试技能保存
def test_skill_save():
    print("\n测试技能保存:")
    
    # 测试正常保存
    result = skill_learning.save_skill(
        "test_skill",
        "# 测试技能\n\n这是一个测试技能。"
    )
    print(f"  保存结果: {result['success']}")
    print(f"  消息: {result['message']}")
    
    # 清理测试文件
    if result['success'] and os.path.exists(result['file_path']):
        os.remove(result['file_path'])
        print(f"  已清理测试文件: {result['file_path']}")

if __name__ == "__main__":
    test_intent_detection()
    test_step_extraction()
    test_skill_save()
    print("\n所有测试完成！")
