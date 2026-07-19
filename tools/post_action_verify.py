"""
操作后验证模块：为工具执行提供通用验证机制

设计原则：
1. 声明式配置：每个工具通过配置定义如何验证
2. 自动触发：根据命令内容自动匹配验证器
3. 结果透明：验证结果附加到工具返回中
"""

import json
import re
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum
import shlex


class VerifyResult(Enum):
    """验证结果状态"""
    SUCCESS = "success"      # 验证通过，操作成功
    FAILED = "failed"        # 验证失败，操作未达成目标
    UNCERTAIN = "uncertain"  # 无法验证，需要人工确认
    SKIPPED = "skipped"      # 跳过验证（非目标操作）


@dataclass
class VerifyContext:
    """验证上下文"""
    original_command: str      # 原始命令
    tool_output: str           # 工具原始输出
    tool_success: bool         # 工具是否返回 success=True
    extracted_params: Dict[str, Any]  # 从命令提取的参数


@dataclass
class VerifyReport:
    """验证报告"""
    result: VerifyResult
    message: str
    details: Optional[Dict] = None


# 验证器注册表: {操作类型: 验证函数}
_VERIFIERS: Dict[str, Callable[[VerifyContext], VerifyReport]] = {}


def register_verifier(operation_type: str):
    """装饰器：注册操作后验证器"""
    def decorator(func: Callable[[VerifyContext], VerifyReport]):
        _VERIFIERS[operation_type] = func
        return func
    return decorator


def _extract_program_name(command: str) -> Optional[str]:
    """从命令中提取程序/应用名称（支持多种格式）"""
    patterns = [
        # 卸载相关：从 -like '*名称*' 中提取
        r"Win32_Product.*Name\s+-like\s+['\"]\*(.+?)\*['\"]",
        r"Get-AppxPackage.*DisplayName\s+-like\s+['\"]\*(.+?)\*['\"]",
        r"Get-ItemProperty.*DisplayName\s+-like\s+['\"]\*(.+?)\*['\"]",
        # winget/msiexec 卸载
        r"winget\s+uninstall\s+(?:--id\s+)?(.+?)(?:\s|$)",
        r"msiexec\s+/x\s+\{?\s*(.+?)\s*\}?(?:\s|$)",
        # Start-Process 卸载程序：从 -FilePath "xxx/uninstall.exe" 中提取目录名
        r"Start-Process.*-FilePath\s+['\"]([^'\"]+?)(?:[Uu]ninstall|unins\d{3})\.(?:exe|bat)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            result = match.group(1).strip()
            # 从路径中提取程序名（取最后一级目录名）
            if "\\" in result or "/" in result:
                result = re.split(r"[\\/]", result)[-1]
            # 清理可能残留的引号和通配符
            result = result.strip("'\"*")
            if result and len(result) < 100:
                return result
    return None


def _extract_registry_path(command: str) -> Optional[str]:
    """从 reg 命令中提取注册表路径"""
    # 匹配 "HKCU\..." 或 'HKCU\...' 或裸路径
    match = re.search(r"(HKCU|HKLM|HKEY_[A-Z_]+)\\[^\"'\s]+|['\"](HKCU|HKLM|HKEY_[A-Z_]+)\\[^'\"]+['\"]", command, re.IGNORECASE)
    if match:
        path = match.group(0).strip('"\'')
        return path
    return None


def _extract_registry_value(command: str) -> tuple:
    """从 reg add 命令中提取路径、值名、值数据"""
    path = _extract_registry_path(command)
    value_name = None
    value_data = None
    
    # /v 值名
    v_match = re.search(r"/v\s+['\"]?([^'\"\s]+)['\"]?", command)
    if v_match:
        value_name = v_match.group(1)
    
    # /d 值数据
    d_match = re.search(r"/d\s+['\"]?([^'\"]+)['\"]?(?:\s|$|/)", command)
    if d_match:
        value_data = d_match.group(1).strip('"\'')
    
    return path, value_name, value_data


# ═══════════════════════════════════════════════════════════════
# 具体验证器实现
# ═══════════════════════════════════════════════════════════════

@register_verifier("uninstall")
def verify_uninstall(ctx: VerifyContext) -> VerifyReport:
    """
    验证卸载操作：检查程序是否仍然存在（优化版 - 修复转义与匹配逻辑）
    """
    program = _extract_program_name(ctx.original_command)
    if not program:
        return VerifyReport(
            result=VerifyResult.UNCERTAIN,
            message="无法从命令中提取程序名称，跳过卸载验证",
            details={"command": ctx.original_command}
        )

    # 核心优化1：只转义单引号（PowerShell 单引号字符串的唯一硬性要求），
    # 不要删除/篡改 $ 或其他字符，交由 [regex]::Escape 处理匹配阶段。
    program_escaped = program.replace("'", "''")

    # 构建验证命令：使用 -match 配合 [regex]::Escape，彻底避免通配符冲突
    verify_cmd = f"""
$prog = '{program_escaped}'
$escapedProg = [regex]::Escape($prog)
$found = @()

# 1. 检查 WMI (MSI 安装)
try {{
    $wmi = Get-WmiObject -Class Win32_Product -ErrorAction SilentlyContinue | 
           Where-Object {{ $_.Name -match $escapedProg }}
    if ($wmi) {{ $found += "WMI:" + $wmi.Name }}
}} catch {{}}

# 2. 检查 Appx (UWP/Store)
try {{
    $appx = Get-AppxPackage -ErrorAction SilentlyContinue | 
            Where-Object {{ $_.Name -match $escapedProg -or $_.DisplayName -match $escapedProg }}
    if ($appx) {{ $found += "Appx:" + $appx.DisplayName }}
}} catch {{}}

# 3. 检查开始菜单 (注意：这里用 .Name 属性)
try {{
    $startMenu = Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" -Recurse -ErrorAction SilentlyContinue | 
                 Where-Object {{ $_.Name -match $escapedProg }}
    if ($startMenu) {{ $found += "StartMenu:" + $startMenu.Name }}
}} catch {{}}

# 4. 检查注册表卸载项（匹配 DisplayName 和 Publisher）
try {{
    $regPaths = @(
        "HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
        "HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
        "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
        "HKCU:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*"
    )
    foreach ($rp in $regPaths) {{
        $reg = Get-ItemProperty $rp -ErrorAction SilentlyContinue | 
               Where-Object {{ $_.DisplayName -match $escapedProg }}
        if ($reg) {{ $found += "Registry:" + $reg.DisplayName }}
    }}
}} catch {{}}

if ($found.Count -eq 0) {{
    Write-Output "VERIFIED: Program '$prog' not found - uninstall successful"
}} else {{
    Write-Output "STILL_EXISTS: Found in: $($found -join '; ')"
}}
"""
    
    # 执行验证（注意：这里你仍需确保 safe_exec 已正确导入）
    try:
        from . import safe_exec
    except ImportError:
        safe_exec = None  # 或抛出一个明确的错误提示
    result = safe_exec.run_powershell(verify_cmd, timeout=30)
    
    if not result.get("success"):
        return VerifyReport(
            result=VerifyResult.UNCERTAIN,
            message=f"验证执行失败: {result.get('message', '未知错误')}",
            details={"program": program}
        )
    
    output = result.get("output", "")
    
    if "VERIFIED:" in output:
        return VerifyReport(
            result=VerifyResult.SUCCESS,
            message=f"✓ 验证通过：程序 '{program}' 已成功卸载",
            details={"program": program, "check_methods": ["WMI", "Appx", "StartMenu", "Registry"]}
        )
    elif "STILL_EXISTS:" in output:
        return VerifyReport(
            result=VerifyResult.FAILED,
            message=f"✗ 验证失败：程序 '{program}' 仍然存在",
            details={"program": program, "found_in": output.replace("STILL_EXISTS: ", "").strip()}
        )
    else:
        return VerifyReport(
            result=VerifyResult.UNCERTAIN,
            message=f"? 验证结果不明确",
            details={"program": program, "raw_output": output[:200]}
        )


@register_verifier("registry_add")
def verify_registry_add(ctx: VerifyContext) -> VerifyReport:
    """
    验证注册表添加/修改操作
    """
    path, value_name, expected_data = _extract_registry_value(ctx.original_command)
    
    if not path:
        return VerifyReport(
            result=VerifyResult.UNCERTAIN,
            message="无法提取注册表路径，跳过验证"
        )
    
    # 构建验证命令
    if value_name:
        verify_cmd = f'reg query "{path}" /v "{value_name}" 2>&1'
    else:
        verify_cmd = f'reg query "{path}" 2>&1'  # 仅查询路径下所有值（验证路径存在）
    
    try:
        from . import safe_exec
    except ImportError:
        safe_exec = None  # 或抛出一个明确的错误提示
    result = safe_exec.run_cmd(shlex.split(verify_cmd), timeout=10)
    
    if not result.get("success"):
        return VerifyReport(
            result=VerifyResult.FAILED,
            message=f"✗ 验证失败：无法查询注册表项 '{path}'",
            details={"path": path, "error": result.get("message")}
        )
    
    output = result.get("output", "")
    
    # 检查值是否存在
    if value_name and value_name not in output:
        return VerifyReport(
            result=VerifyResult.FAILED,
            message=f"✗ 验证失败：值 '{value_name}' 不存在于 '{path}'",
            details={"path": path, "value_name": value_name}
        )
    
    # 如果知道期望值，检查是否匹配
    if expected_data and expected_data in output:
        return VerifyReport(
            result=VerifyResult.SUCCESS,
            message=f"✓ 验证通过：注册表值已设置为 '{expected_data}'",
            details={"path": path, "value_name": value_name, "value_data": expected_data}
        )
    elif expected_data:
        # 提取实际值进行比较
        actual_match = re.search(rf"{re.escape(value_name)}\s+\w+\s+(.+)", output)
        actual_value = actual_match.group(1).strip() if actual_match else "未知"
        return VerifyReport(
            result=VerifyResult.FAILED if expected_data != actual_value else VerifyResult.SUCCESS,
            message=f"{'✓' if expected_data == actual_value else '✗'} 验证{'通过' if expected_data == actual_value else '失败'}：期望值 '{expected_data}'，实际值 '{actual_value}'",
            details={"path": path, "expected": expected_data, "actual": actual_value}
        )
    
    return VerifyReport(
        result=VerifyResult.SUCCESS,
        message=f"✓ 验证通过：注册表项 '{path}' 存在",
        details={"path": path, "output": output[:500]}
    )
    
    try:
        from . import safe_exec
    except ImportError:
        safe_exec = None  # 或抛出一个明确的错误提示
    result = safe_exec.run_cmd(shlex.split(verify_cmd), timeout=10)
    
    if not result.get("success"):
        return VerifyReport(
            result=VerifyResult.UNCERTAIN,
            message="无法验证音量设置"
        )
    
    output = result.get("output", "")
    
    if "CURRENT_VOLUME:" in output:
        current_vol = output.split("CURRENT_VOLUME:")[1].strip()
        if expected_volume:
            # 音量值可能有微小差异，允许 ±2 的误差
            try:
                diff = abs(int(current_vol) - int(expected_volume))
                if diff <= 2:
                    return VerifyReport(
                        result=VerifyResult.SUCCESS,
                        message=f"✓ 验证通过：音量已设置为 {current_vol}% (目标: {expected_volume}%)",
                        details={"current": current_vol, "expected": expected_volume}
                    )
                else:
                    return VerifyReport(
                        result=VerifyResult.FAILED,
                        message=f"✗ 验证失败：当前音量 {current_vol}%，但目标为 {expected_volume}%",
                        details={"current": current_vol, "expected": expected_volume, "diff": diff}
                    )
            except ValueError:
                pass
        
        return VerifyReport(
            result=VerifyResult.SUCCESS,
            message=f"✓ 当前音量：{current_vol}%",
            details={"current": current_vol}
        )
    
    return VerifyReport(
        result=VerifyResult.UNCERTAIN,
        message="无法获取当前音量值"
    )


# ═══════════════════════════════════════════════════════════════
# 核心验证调度器
# ═══════════════════════════════════════════════════════════════

# 命令模式匹配规则: [(模式, 操作类型, 优先级), ...]
_VERIFICATION_PATTERNS: List[tuple] = [
    # 卸载相关（最高优先级）- 只匹配实际执行卸载的命令，不匹配查询命令
    # 排除 Get-ItemProperty, Get-WmiObject, Get-AppxPackage, Select-Object 等查询命令
    (r"winget\s+uninstall", "uninstall", 100),
    (r"msiexec\s+/x", "uninstall", 100),
    (r"Start-Process.*[Uu]ninstall", "uninstall", 100),
    (r"Remove-AppxPackage", "uninstall", 100),
    (r"sc\s+delete", "uninstall", 95),
    (r"\.Uninstall\(\)", "uninstall", 95),
    
    # 注册表添加/修改
    (r"reg\s+add", "registry_add", 90),
    
    # 音量设置
    (r"volume|音量|SetVolume|nircmd.*\d+\s*\d+", "audio_volume", 80),
    
    # 可以扩展更多...
]


def detect_operation_type(command: str) -> Optional[str]:
    """
    根据命令内容检测操作类型
    返回最匹配的操作类型，如果没有匹配则返回 None
    """
    command_lower = command.lower()
    matches = []
    
    for pattern, op_type, priority in _VERIFICATION_PATTERNS:
        if re.search(pattern, command_lower):
            matches.append((op_type, priority))
    
    if not matches:
        return None
    
    # 按优先级排序，返回最高优先级的
    matches.sort(key=lambda x: -x[1])
    return matches[0][0]


def verify_tool_result(
    tool_name: str,
    command: str,
    tool_output: str,
    tool_success: bool,
    force_verify: bool = False
) -> Optional[Dict]:
    """
    对工具执行结果进行后验证
    
    Args:
        tool_name: 工具名称 (如 "execute_command")
        command: 执行的命令
        tool_output: 工具的原始输出
        tool_success: 工具是否报告成功
        force_verify: 强制验证（即使 tool_success=False 也验证）
    
    Returns:
        验证报告字典，如果没有匹配验证器则返回 None
    """
    # 如果工具已经明确失败，且不是强制验证，跳过
    if not tool_success and not force_verify:
        return None
    
    # 检测操作类型
    op_type = detect_operation_type(command)
    if not op_type:
        return None  # 没有匹配的验证器
    
    # 获取验证器
    verifier = _VERIFIERS.get(op_type)
    if not verifier:
        return None
    
    # 构建上下文并执行验证
    ctx = VerifyContext(
        original_command=command,
        tool_output=tool_output,
        tool_success=tool_success,
        extracted_params={}
    )
    
    try:
        report = verifier(ctx)
    except Exception as e:
        return {
            "verified": False,
            "verify_error": str(e),
            "operation_type": op_type,
            "message": f"验证器执行异常: {str(e)[:100]}"
        }
    
    # 构建返回结果
    return {
        "verified": report.result == VerifyResult.SUCCESS,
        "verify_result": report.result.value,
        "verify_message": report.message,
        "operation_type": op_type,
        "details": report.details
    }


def format_verify_result(verify_report: Dict) -> str:
    """
    将验证报告格式化为人类可读的字符串
    """
    if not verify_report:
        return ""
    
    lines = ["\n【操作验证】"]
    lines.append(verify_report.get("verify_message", "验证完成"))
    
    if verify_report.get("details"):
        details = verify_report["details"]
        if isinstance(details, dict):
            for key, value in details.items():
                if isinstance(value, str) and len(value) < 100:
                    lines.append(f"  • {key}: {value}")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 便捷函数：用于工具包装
# ═══════════════════════════════════════════════════════════════

def with_verification(tool_func: Callable) -> Callable:
    """
    装饰器：为工具函数自动添加验证逻辑
    
    使用示例:
    @register_tool
    @with_verification
    def execute_command(command: str) -> str:
        ...
    """
    def wrapper(*args, **kwargs):
        # 执行原始工具
        result = tool_func(*args, **kwargs)
        
        # 尝试提取命令参数（假设第一个参数是 command）
        command = kwargs.get("command") or (args[0] if args else "")
        
        # 解析工具结果
        try:
            result_obj = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            return result
        
        tool_success = result_obj.get("success", True)
        tool_output = result_obj.get("output", "")
        
        # 执行验证
        verify_report = verify_tool_result(
            tool_name=tool_func.__name__,
            command=command,
            tool_output=tool_output,
            tool_success=tool_success
        )
        
        if verify_report:
            # 将验证结果附加到返回中
            result_obj["verification"] = verify_report
            
            # 如果验证失败但工具报告成功，添加警告
            if verify_report.get("verify_result") == "failed" and tool_success:
                result_obj["warning"] = (
                    "⚠️ 命令执行成功，但验证未通过。"
                    f"{verify_report.get('verify_message', '')}"
                )
        
        return json.dumps(result_obj, ensure_ascii=False) if isinstance(result, str) else result_obj
    
    return wrapper
