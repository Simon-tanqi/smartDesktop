"""追踪 11:27-11:29 的调音量任务完整轨迹"""
import json

target_start = "2026-07-12 11:27:00"
target_end = "2026-07-12 11:29:00"
out_lines = []
with open(r'C:\Users\username\AppData\Roaming\MyAgent\logs\agent_2026-07-12.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        try:
            d = json.loads(line)
            msg = d.get('message', '')
            ts = d.get('timestamp', '')[:19]
            if not (target_start <= ts <= target_end):
                continue
            out_lines.append(f"{ts} [{d.get('level','')}] {msg[:400]}")
        except Exception as e:
            pass

with open(r'D:\PyCharm Project\MyAgent\logs\trace_volume_2026-07-12.txt', 'w', encoding='utf-8') as f:
    f.write("\n".join(out_lines))
print(f"已写出 {len(out_lines)} 行到 trace_volume_2026-07-12.txt")
