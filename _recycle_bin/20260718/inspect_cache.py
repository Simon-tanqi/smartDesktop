"""检查固化缓存当前状态"""
import json
import os

path = r"C:\Users\username\AppData\Roaming\MyAgent\cached_actions.json"
if not os.path.exists(path):
    print("缓存文件不存在")
    raise SystemExit

with open(path, encoding="utf-8") as f:
    data = json.load(f)

records = data.get("固化记录", {})
print(f"总记录数: {len(records)}")
legacy = sum(1 for v in records.values() if v.get("version", 1) < 2)
wf = sum(1 for v in records.values() if v.get("version", 1) >= 2)
print(f"legacy (v1): {legacy}, workflow (v2): {wf}")
print()
print("--- 包含危险工具的 legacy 记录 ---")
for k, v in records.items():
    if v.get("version", 1) < 2:
        t = v.get("固化参数", {}).get("tool", "")
        if t in ("get_uninstall_string", "execute_command", "modify_registry", "gui_keys", "gui_type", "web_download", "delete_file", "delete_directory"):
            print(f"  {k}: tool={t!r}, count={v.get('success_count', 0)}")
