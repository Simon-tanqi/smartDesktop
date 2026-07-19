"""
一次性脚本：清理 cached_actions.json 中所有含危险工具的 legacy 记录。
方案 A 改造后这些记录永远不会被命中，但占空间 + 易污染分析日志。
"""
import json
import os
import sys

# 复用 routing.py 的安全检查
sys.path.insert(0, r"D:\PyCharm Project\MyAgent")
from backend.routing import _is_unsafe_for_cache, _CACHED_ACTIONS_PATH

if not os.path.exists(_CACHED_ACTIONS_PATH):
    print("缓存文件不存在，跳过迁移")
    raise SystemExit(0)

with open(_CACHED_ACTIONS_PATH, encoding="utf-8") as f:
    data = json.load(f)

records = data.get("固化记录", {})
removed = []
kept = {}
for k, v in records.items():
    if v.get("version", 1) >= 2:
        kept[k] = v
        continue
    tool = v.get("固化参数", {}).get("tool", "")
    if _is_unsafe_for_cache(tool):
        removed.append((k, tool, v.get("success_count", 0)))
    else:
        kept[k] = v

data["固化记录"] = kept

# 原子写
import tempfile
with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(_CACHED_ACTIONS_PATH), delete=False, suffix=".tmp") as tf:
    json.dump(data, tf, ensure_ascii=False, indent=2)
    tmpname = tf.name
os.replace(tmpname, _CACHED_ACTIONS_PATH)

print(f"已清理 {len(removed)} 条 legacy 危险记录:")
for k, t, c in removed:
    print(f"  {k}: tool={t!r}, count={c}")
print(f"保留 {len(kept)} 条记录 (含 workflow v2 与安全 legacy)")
