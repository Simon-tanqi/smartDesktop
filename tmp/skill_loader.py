# skill_loader.py
import os
import json

def load_skill_prompt(intent_tag: str) -> str:
    """
    按需加载：根据意图标签，动态读取对应的 SKILL.md 文件。
    这就是“渐进式披露”，初始化时只读元数据，命中时才加载完整提示词<websource>source_group_web_6</websource>。
    """
    skill_path = f"./skills/{intent_tag}/SKILL.md"
    if os.path.exists(skill_path):
        with open(skill_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "你是一个通用助手。"