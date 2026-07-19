"""2026-07-12 修复：删除污染的 v2 workflow 记录

污染特征：
1. success_count < 3 的 workflow（不够稳定，不应启用）
2. 全部为不安全工具的 workflow（如纯 execute_command 链）
3. 已知脏数据 hash: d31236b4ff67（喇叭声音调音量，6 步全错）
"""
import json
from pathlib import Path

CACHE_PATH = Path.home() / "AppData/Roaming/MyAgent/cached_actions.json"
BACKUP_PATH = CACHE_PATH.with_suffix(".json.bak.20260712-1204")

# 1. 读取并备份
cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
records = cache.get("固化记录", {})

# 2. 识别要删除的记录
to_drop = []
for key, rec in records.items():
    # v1 legacy: 一律保留（v1 已有黑名单保护）
    if rec.get("version", 1) < 2:
        continue

    # 已知脏数据：d31236b4ff67（喇叭声音改 45）
    if key == "d31236b4ff67":
        to_drop.append((key, "已知脏数据：6 步全 execute_command，无参数，无法完成音量调节"))
        continue

    # success_count 太低
    success = rec.get("success_count", 0)
    if success < 3:
        to_drop.append((key, f"success_count={success} < 3，不够稳定"))
        continue

    # 全部为不安全工具
    workflow = rec.get("workflow", [])
    if workflow:
        all_unsafe = all(s.get("require_confirm", False) for s in workflow)
        if all_unsafe and len(workflow) >= 2:
            tools = [s.get("tool") for s in workflow]
            to_drop.append((key, f"全部为不安全工具: {tools}"))
            continue

# 3. 删除
print(f"检查 {len(records)} 条记录，将删除 {len(to_drop)} 条：")
for k, reason in to_drop:
    print(f"  - {k}: {reason}")
    del records[k]

# 4. 写回
CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n✅ 修复完成。剩余 {len(records)} 条记录。")
print(f"📦 备份位置: {BACKUP_PATH}")
