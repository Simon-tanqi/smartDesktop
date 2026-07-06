# 操作后验证机制 (Post-Action Verification)

## 概述

为工具执行添加自动验证层，确保操作真正达成预期目标，而不是仅报告"执行成功"。

## 核心问题

以"卸载迅雷"为例：
- **之前**: WMI 查询返回空结果 → 命令成功 → AI 报告"已卸载"
- **实际**: 迅雷仍然存在（开始菜单、注册表等）
- **之后**: 命令成功 + **验证失败** → AI 报告"命令执行但验证未通过"

## 架构设计

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   工具执行       │────▶│  操作后验证       │────▶│  带验证的响应    │
│ (execute_command)│     │ (post_action_verify)│    │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
        │                        │                        │
        ▼                        ▼                        ▼
   执行 PowerShell          检测操作类型              success + verification
   返回 output              执行验证命令              或 warning
```

## 验证器类型

### 1. 卸载验证 (`uninstall`)

**触发条件**: 命令包含 `Uninstall()`、`卸载`、`uninstall` 等关键词

**验证逻辑**:
1. WMI (Win32_Product) - MSI 安装的程序
2. Appx (Get-AppxPackage) - UWP/Store 应用
3. 开始菜单快捷方式
4. 注册表卸载项

**输出示例**:
```json
{
  "verified": false,
  "verify_result": "failed",
  "verify_message": "验证失败：程序 '迅雷' 仍然存在，卸载未成功",
  "details": {
    "program": "迅雷",
    "found_in": "StartMenu:迅雷.lnk; Registry:迅雷X"
  }
}
```

### 2. 注册表验证 (`registry_add`)

**触发条件**: 命令包含 `reg add`

**验证逻辑**:
1. 执行 `reg query` 检查键是否存在
2. 如果知道期望值，验证值是否匹配

**输出示例**:
```json
{
  "verified": true,
  "verify_result": "success",
  "verify_message": "验证通过：注册表值已设置为 'HH:mm:ss'",
  "details": {
    "path": "HKCU\\Control Panel\\International",
    "value_name": "sShortTime",
    "value_data": "HH:mm:ss"
  }
}
```

### 3. 音量验证 (`audio_volume`)

**触发条件**: 命令包含 `volume`、`音量`、`SetVolume` 等

**验证逻辑**:
1. 设置音量后读回实际值
2. 允许 ±2% 误差

## 工具集成

### execute_command

```python
# 执行命令
result = safe_exec.run_powershell(command, ...)

# 执行后验证
verify_report = post_action_verify.verify_tool_result(
    tool_name="execute_command",
    command=command,
    tool_output=result.get("output", ""),
    tool_success=True
)

# 如果验证失败，添加警告
if verify_report and verify_report.get("verify_result") == "failed":
    response["warning"] = f"⚠️ {verify_report.get('verify_message')}"
```

### modify_registry

```python
# 执行 reg 命令
result = safe_exec.run_cmd(cmd_list, ...)

# 如果是 add 操作，执行验证
if operation_tag == "add":
    verify_report = post_action_verify.verify_tool_result(...)
    if verify_report:
        response["verification"] = verify_report
```

## 扩展新验证器

在 `post_action_verify.py` 中添加：

```python
@register_verifier("new_operation")
def verify_new_operation(ctx: VerifyContext) -> VerifyReport:
    """
    验证新操作类型
    """
    # 1. 从命令提取参数
    param = extract_param(ctx.original_command)
    
    # 2. 执行验证命令
    verify_cmd = f"..."
    result = safe_exec.run_powershell(verify_cmd)
    
    # 3. 返回验证报告
    if success:
        return VerifyReport(
            result=VerifyResult.SUCCESS,
            message="验证通过：...",
            details={...}
        )
    else:
        return VerifyReport(
            result=VerifyResult.FAILED,
            message="验证失败：...",
            details={...}
        )
```

在 `_VERIFICATION_PATTERNS` 中添加匹配规则：

```python
_VERIFICATION_PATTERNS = [
    ...
    (r"new_keyword|pattern", "new_operation", 70),
]
```

## 验证结果状态

| 状态 | 含义 | 处理建议 |
|------|------|---------|
| `success` | 验证通过，操作达成目标 | 正常返回 |
| `failed` | 验证失败，操作未达成目标 | 添加 warning，提示用户 |
| `uncertain` | 无法验证（如无法提取参数） | 正常返回，但不保证结果 |
| `skipped` | 非目标操作，跳过验证 | 正常返回 |

## 对 LLM 的影响

验证结果会作为 `verification` 字段附加到工具返回中：

```json
{
  "success": true,
  "output": "...",
  "verification": {
    "verified": false,
    "verify_result": "failed",
    "verify_message": "..."
  },
  "warning": "⚠️ 命令执行成功，但验证未通过。"
}
```

LLM 在后续决策中可以看到验证结果，避免错误报告。

## 注意事项

1. **验证有成本**: 每个验证器会执行额外的 PowerShell 命令，增加延迟
2. **验证可能失败**: 验证命令本身可能因权限、环境问题失败，此时标记为 `uncertain`
3. **不是 100% 准确**: 验证器尽量全面，但可能有遗漏（如绿色软件不写入注册表）
4. **优先保证正确性**: 宁可报告"不确定"，也不要错误报告"成功"
