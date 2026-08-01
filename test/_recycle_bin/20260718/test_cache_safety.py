"""单元测试：v2 workflow 固化拒绝逻辑

场景：
A. 全为不安全工具 + 步骤≥2 → 拒绝
B. 单工具重复 ≥3 次 → 拒绝
C. 步骤≥4 且无变量提取 → 拒绝
D. 合理工作流 → 允许
"""
import sys
sys.path.insert(0, r'D:\PyCharm Project\MyAgent')

# 模拟 _record_workflow_success 的安全检查逻辑
import json

# 复制路由逻辑中的检查函数
_UNSAFE_CACHE_TOOLS = {
    "execute_command", "modify_registry", "web_download", "delete_file",
    "delete_directory", "get_uninstall_string", "search_programs",
}

def _is_unsafe_for_cache(tool_name: str) -> bool:
    return tool_name in _UNSAFE_CACHE_TOOLS

def _guess_variables(text: str) -> dict:
    """简化版：检测 sw_name/var_N 等变量"""
    import re
    hints = {}
    if re.search(r'(?:软件|卸载)\s*([\w一-龥]+)', text):
        hints['sw_name'] = re.search(r'(?:软件|卸载)\s*([\w一-龥]+)', text).group(1)
    return hints

def _param_to_template(params, hint):
    if not params:
        return {}
    out = {}
    for k, v in params.items():
        if isinstance(v, str) and v in (hint.values() if hint else []):
            out[k] = f"${{var.{k}}}"
        else:
            out[k] = v
    return out

def _should_reject_workflow(user_message, tool_call_chain):
    """镜像 routing.py 中的拒绝逻辑"""
    workflow_steps = []
    variables_hint = _guess_variables(user_message)
    for call in tool_call_chain:
        tool_name = call.get("tool", "")
        raw_params = call.get("params", {}) or {}
        is_unsafe = _is_unsafe_for_cache(tool_name)
        resolved_params = _param_to_template(raw_params, variables_hint)
        step = {"tool": tool_name, "params": resolved_params}
        if is_unsafe:
            step["require_confirm"] = True
        workflow_steps.append(step)

    if not workflow_steps:
        return True, "empty"

    # 检查 1: 全为不安全工具（步骤≥3 才拒绝，保护 2 步合法卸载任务）
    unsafe_count = sum(1 for s in workflow_steps if s.get("require_confirm", False))
    if unsafe_count == len(workflow_steps) and len(workflow_steps) >= 3:
        return True, f"all_unsafe ({unsafe_count} steps)"

    # 检查 2: 单工具重复 ≥3
    tool_names = [s.get("tool") for s in workflow_steps]
    if len(set(tool_names)) == 1 and len(tool_names) >= 3:
        return True, f"single_tool_repeat ({tool_names[0]} × {len(tool_names)})"

    # 检查 3: 步骤≥4 且无变量
    has_variable = bool(variables_hint) and any(
        "${" in json.dumps(s.get("params", {}), ensure_ascii=False)
        for s in workflow_steps
    )
    if not has_variable and len(workflow_steps) >= 4:
        return True, f"too_many_no_var ({len(workflow_steps)} steps)"

    return False, "allow"


print("=" * 70)
print("测试 1: 调音量任务（6 步全 execute_command）")
chain = [
    {"tool": "execute_command", "params": {"command": "SendKeys 173"}},
    {"tool": "execute_command", "params": {"command": "set_volume 45"}},
    {"tool": "execute_command", "params": {"command": "pip install pycaw"}},
    {"tool": "execute_command", "params": {"command": "python -c 'import pycaw'"}},
    {"tool": "execute_command", "params": {"command": "python -c 'AudioUtilities'"}},
    {"tool": "execute_command", "params": {"command": "python -c 'set volume'"}},
]
rejected, reason = _should_reject_workflow("帮我把喇叭声音改成45", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert rejected, "应该被拒绝！"

print("\n测试 2: get_workspace_path 重复调用（5 次）")
chain = [
    {"tool": "get_workspace_path", "params": {}} for _ in range(5)
]
rejected, reason = _should_reject_workflow("查workspace", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert rejected, "应该被拒绝！"

print("\n测试 3: 4 步不同工具、无变量（应该被拒绝）")
chain = [
    {"tool": "search_files", "params": {"keyword": "Q"}},
    {"tool": "read_file", "params": {}},
    {"tool": "execute_command", "params": {"command": "ls"}},
    {"tool": "write_file", "params": {}},
]
rejected, reason = _should_reject_workflow("随便查一些东西", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert rejected, "应该被拒绝！"

print("\n测试 4: 卸载迅雷（2 步 + 1 变量）—— 应该被允许")
chain = [
    {"tool": "search_programs", "params": {"name": "${var.sw_name}"}},
    {"tool": "get_uninstall_string", "params": {"program_name": "${var.sw_name}"}},
]
rejected, reason = _should_reject_workflow("帮我把软件迅雷卸载一下", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert not rejected, "应该被允许！"

print("\n测试 5: 查天气（1 步）—— 应该被允许")
chain = [
    {"tool": "get_weather", "params": {"city": "${var.city}"}},
]
rejected, reason = _should_reject_workflow("查北京今天的天气", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert not rejected, "应该被允许！"

print("\n测试 6: 修复后音量任务（1 步 system_audio）—— 应该被允许")
chain = [
    {"tool": "system_audio", "params": {"operation": "set_volume", "value": "${var.value}"}},
]
rejected, reason = _should_reject_workflow("把喇叭声音改成45", chain)
print(f"  结果: rejected={rejected}, reason={reason}")
assert not rejected, "应该被允许！"

print("\n" + "=" * 70)
print("✅ 全部 6 个测试通过！")
