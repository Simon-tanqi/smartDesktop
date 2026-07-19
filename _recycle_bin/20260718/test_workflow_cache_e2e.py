"""
v2 workflow 固话端到端测试：
1. 清空缓存
2. 跑 3 次"查北京天气"（纯只读工具，应该走完整 agent 然后写入 v2 workflow）
3. 跑第 4 次，看是否命中 L2 固话（应该 < 5s 返回）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

# 强制无缓冲
sys.stdout.reconfigure(line_buffering=True)

CACHE_PATH = r"C:\Users\username\AppData\Roaming\MyAgent\cached_actions.json"
BASE_URL = "http://127.0.0.1:8765"

def call_chat(message: str, session_id: str) -> str:
    body = json.dumps({
        "message": message,
        "session_id": session_id,
        "model_choice": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "id": "deepseek-chat",
            "display": "DeepSeek Chat",
        },
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return f"[ERROR] {e}"
    elapsed = time.time() - t0
    # 提取 final 文本
    final_text = ""
    for line in data.split("\n"):
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
                if ev.get("type") == "final":
                    final_text = ev.get("text", "")
                    break
            except json.JSONDecodeError:
                pass
    return f"[{elapsed:.1f}s] final={final_text[:200]}"

def inspect_cache():
    if not os.path.exists(CACHE_PATH):
        return "缓存文件不存在"
    with open(CACHE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("固化记录", {})
    summary = []
    for k, v in recs.items():
        summary.append({
            "key": k,
            "version": v.get("version", 1),
            "kind": v.get("kind", "legacy"),
            "scenario": v.get("scenario_id", ""),
            "count": v.get("success_count", 0),
            "fail": v.get("fail_count", 0),
            "tools": [s.get("tool") for s in v.get("workflow", [])] or [v.get("固化参数", {}).get("tool", "")],
        })
    return summary

# 1. 清空缓存
print("=" * 60, flush=True)
print("Step 1: 清空缓存", flush=True)
print("=" * 60, flush=True)
data = {"固化记录": {}}
with open(CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("缓存已清空", flush=True)
print(flush=True)

# 2. 跑 3 次（写入 v2 workflow，但未达 3 次门槛前不命中）
for i in range(1, 4):
    print(f"--- 第 {i} 次执行 ---", flush=True)
    sid = f"test-wf-{int(time.time())}-{i}"
    print(call_chat("查北京今天的天气", sid), flush=True)
    time.sleep(2)
    print(f"  缓存状态: {inspect_cache()}", flush=True)
    print(flush=True)

# 3. 第 4 次 - 应该命中 L2 v2 workflow
print("=" * 60, flush=True)
print("Step 4: 跑第 4 次（应命中 L2 固话）", flush=True)
print("=" * 60, flush=True)
sid = f"test-wf-{int(time.time())}-hit"
print(call_chat("查北京今天的天气", sid), flush=True)
print(flush=True)
print("最终缓存:", flush=True)
for s in inspect_cache():
    print(f"  {s}", flush=True)
