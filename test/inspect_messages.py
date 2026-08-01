# -*- coding: utf-8 -*-
"""展示 LLM 日志中的完整 messages 字段"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import json
import os

LOG_DIR = r"C:\Users\username\AppData\Roaming\MyAgent\logs\llm_calls"
log_file = os.path.join(LOG_DIR, "2026080121_2_新会话55.json")

with open(log_file, encoding="utf-8") as f:
    data = json.load(f)

for turn_key, turn_data in sorted(data.get("turns", {}).items(), key=lambda x: int(x[0])):
    print(f"\n{'='*60}")
    print(f"  Turn {turn_key}  |  user_task: {turn_data.get('user_task', '')[:60]}")
    print(f"{'='*60}")

    for ct_key, ct_data in sorted(turn_data.get("child_turns", {}).items(), key=lambda x: int(x[0])):
        msgs = ct_data.get("messages", [])
        if msgs is None:
            print("  (messages=None)")
            continue

        print(f"\n  子轮次 {ct_key}  |  共 {len(msgs)} 条消息\n")
        for i, msg in enumerate(msgs):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            print(f"  --- 消息 [{i}] role={role} ---")
            if isinstance(content, str):
                print(f"  [content] 长度={len(content)}")
                # 打印前500字符
                preview = content[:500]
                if len(content) > 500:
                    preview += f"\n  ... [省略 {len(content)-500} 字符]"
                print(preview)
            elif isinstance(content, list):
                print(f"  [content] 类型=list，{len(content)} 个block")
                for j, block in enumerate(content[:3]):
                    if isinstance(block, dict):
                        print(f"    block[{j}]: type={block.get('type')}, keys={list(block.keys())}")
            elif tool_calls:
                print(f"  [tool_calls]: {json.dumps(tool_calls, ensure_ascii=False)[:300]}")
            else:
                print(f"  [content] 类型={type(content).__name__}, 值={str(content)[:100]}")

            print()
