# -*- coding: utf-8 -*-
"""测试 logger.py 的 messages 字段是否写入 LLM 日志"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import requests
import json
import time
import os

AGENT_URL = "http://localhost:8765"
LOG_DIR = "C:\\Users\\username\\AppData\\Roaming\\MyAgent\\logs\\llm_calls"

def test_simple_llm_log():
    # 1. 创建会话
    r = requests.post(f"{AGENT_URL}/api/sessions/new", timeout=10)
    data = r.json()
    session_id = data["session"]["id"]
    print(f"[+] 会话创建: {session_id}")

    # 2. 发一个简单请求
    task = "1+1等于几？用中文回答。"
    print(f"[+] 发送任务: {task}")
    r = requests.post(
        f"{AGENT_URL}/api/chat",
        json={"session_id": session_id, "message": task},
        stream=True, timeout=60
    )

    # 收集 SSE 响应
    final_content = ""
    for line in r.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if line.startswith("data: "):
            d = line[6:]
            if d.startswith("{"):
                try:
                    obj = json.loads(d)
                    if obj.get("type") == "final":
                        final_content = obj.get("content", "")
                        break
                except Exception:
                    pass

    print(f"[+] 收到回复: {final_content[:100]}")

    # 3. 等日志写入
    time.sleep(2)

    # 4. 查找日志文件
    session_name = data["session"].get("title", "untitled")
    safe_name = session_name.replace("<", "_").replace(">", "_").replace(":", "_")
    log_file = os.path.join(LOG_DIR, f"{session_id}_{safe_name}.json")

    if not os.path.exists(log_file):
        # 尝试其他可能的名称
        for f in os.listdir(LOG_DIR):
            if f.startswith(session_id):
                log_file = os.path.join(LOG_DIR, f)
                break

    print(f"[+] 检查日志文件: {log_file}")
    print(f"    文件存在: {os.path.exists(log_file)}")

    if os.path.exists(log_file):
        with open(log_file, encoding="utf-8") as f:
            log_data = json.load(f)

        turns = log_data.get("turns", {})
        print(f"    轮次数: {len(turns)}")

        for turn_key, turn_data in sorted(turns.items(), key=lambda x: int(x[0])):
            print(f"\n  === Turn {turn_key} ===")
            print(f"  user_task: {turn_data.get('user_task', '')[:80]}")
            child_turns = turn_data.get("child_turns", {})
            for ct_key, ct_data in sorted(child_turns.items(), key=lambda x: int(x[0])):
                msgs = ct_data.get("messages")
                if msgs is not None:
                    print(f"    child_turn {ct_key}: messages 字段存在 ✅")
                    print(f"      messages 条数: {len(msgs)}")
                    for i, msg in enumerate(msgs[:3]):  # 只打印前3条
                        role = msg.get("role", "?")
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            print(f"        [{i}] role={role}, content长度={len(content)}")
                            print(f"            content[:120]={repr(content[:120])}")
                        elif isinstance(content, list):
                            print(f"        [{i}] role={role}, content类型=list, 长度={len(content)}")
                        else:
                            print(f"        [{i}] role={role}, content类型={type(content).__name__}")
                else:
                    print(f"    child_turn {ct_key}: messages=None ❌")
    else:
        print("[-] 日志文件不存在")

    return session_id

if __name__ == "__main__":
    sid = test_simple_llm_log()
    print(f"\n[完成] 会话ID: {sid}")
