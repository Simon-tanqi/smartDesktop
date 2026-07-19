import json
import hashlib
import difflib
import tempfile
import os
import re
import time
import threading
from tools import TOOL_REGISTRY
from llm_client import ai_concat, get_api_key
from config.settings import DATA_DIR
from logger import get_logger

logger = get_logger()

_CACHED_ACTIONS_PATH = os.path.join(DATA_DIR, "cached_actions.json")
_CACHED_ACTIONS_LOCK = threading.Lock()
_CACHED_ACTIONS_MAX_ENTRIES = 1000
_CACHED_ACTIONS_MIN_SUCCESS = 6

# 禁止参与高频固化的工具：任何会修改系统状态、或只是"任务中间步骤"的工具都不能直接固化命中。
# 这些工具要么会真正破坏系统（卸载/删除/修改），要么只是查询/半步动作（get_uninstall_string 只查不卸）。
# 用户报问题："帮我把软件Qorder卸载一下" → 命中了 get_uninstall_string（success_count=9），
# 执行到"找到软件"就停了，卸载动作没发生。
_UNSAFE_CACHE_TOOLS = {
    # 真正会破坏系统的工具
    "execute_command",         # 任意 shell 命令
    "modify_registry",         # 改注册表
    "web_download",            # 下载文件
    "gui_keys",                # 键盘输入
    "gui_type",                # 文本输入
    "gui_workflow",            # GUI 自动化工作流
    "gui_activate",            # 激活窗口
    "gui_move",                # 移动/调整窗口
    "verify_uninstall",        # 卸载验证（仅在卸载后用）
    "delete_file",             # 文件删除
    "delete_directory",        # 目录删除
    "batch_ppt_process",       # PPT 批量处理
    "excel_batch_process",     # Excel 批量
    "excel_format",            # Excel 格式修改
    "excel_pivot",             # 数据透视
    "excel_chart_advanced",    # 图表
    "create_presentation",     # 创建 PPT
    "export_presentation_text",
    "extract_presentation_info",
    "resolve_folder",          # 文件操作辅助
    "scan_directory",          # 扫描（一般只读但被滥用为前置）
    # 卸载任务链的"中间步骤"，只查到不执行
    "get_uninstall_string",    # ★ 问题根源：只查到软件不卸载
    "search_programs",         # 类似：只列出不卸载
    "find_installed",          # 类似
    "list_programs",
    "list_installed",
}

os.makedirs(DATA_DIR, exist_ok=True)


def _is_unsafe_for_cache(tool_name: str) -> bool:
    """判断工具名是否绝对不能参与高频固化"""
    if not tool_name:
        return True
    if tool_name in _UNSAFE_CACHE_TOOLS:
        return True
    # 子串兜底：名字里含 uninstall/delete/remove/kill/install/modify 的工具也拒
    lowered = tool_name.lower()
    for keyword in ("uninstall", "delete", "remove", "install_", "modify_", "kill_", "format"):
        if keyword in lowered:
            return True
    return False


_TASK_VERBS = ["帮我", "请执行", "执行", "删除", "打开", "创建", "修改", "搜索", "下载",
               "运行", "安装", "卸载", "配置", "修复", "排查", "分析", "统计",
               "生成", "批量", "替换", "重构", "迁移", "转换", "提取", "同步"]

_TASK_NOUNS = ["文件", "程序", "软件", "代码", "数据", "配置", "系统", "网络", "项目",
               "服务", "命令", "脚本", "目录", "进程", "端口", "数据库"]


_CHAT_MATCHES: dict[str, str] = {
    "谢谢": "不客气，随时为您服务。",
    "感谢": "不客气，随时为您服务。",
    "多谢": "不客气，随时为您服务。",
    "3q": "不客气，随时为您服务。",
    "thanks": "不客气，随时为您服务。",
    "你好": "您好，我在。请问有什么可以帮您？",
    "hi": "您好，我在。请问有什么可以帮您？",
    "hello": "您好，我在。请问有什么可以帮您？",
    "hey": "您好，我在。请问有什么可以帮您？",
    "在吗": "您好，我在。请问有什么可以帮您？",
    "早上好": "TIME_GREETING",
    "早安": "TIME_GREETING",
    "下午好": "TIME_GREETING",
    "午安": "TIME_GREETING",
    "晚上好": "TIME_GREETING",
    "晚安": "晚安，早点休息。",
    "早点休息": "晚安，早点休息。",
    "再见": "再见，随时找我。",
    "拜拜": "再见，随时找我。",
    "bye": "再见，随时找我。",
    "回头见": "再见，随时找我。",
    "辛苦了": "不辛苦，分内之事。",
    "你是谁": "我是 MyAgent，您的本地智能助手。",
    "你叫什么": "我是 MyAgent，您的本地智能助手。",
}


def _has_task_verb(msg: str) -> bool:
    msg_lower = msg.lower()
    for verb in _TASK_VERBS:
        if verb in msg_lower:
            for noun in _TASK_NOUNS:
                if noun in msg_lower:
                    return True
            if len(msg.strip()) > 6:
                return True
            if verb in ["帮我", "请执行"]:
                return True
    return False


def check_chat_layer(user_message: str, context: dict = None) -> str | None:
    msg_lower = user_message.strip().lower()
    
    if msg_lower in _CHAT_MATCHES:
        reply = _CHAT_MATCHES[msg_lower]
        
        if reply == "TIME_GREETING":
            reply = _get_time_enhanced_greeting()
        
        if msg_lower in ['你好', 'hello', 'hi', 'hey'] and context:
            return _enhance_greeting_with_context(reply, context)
        
        return reply
    
    if len(user_message.strip()) <= 8 and not _has_task_verb(msg_lower):
        for keyword, reply in _CHAT_MATCHES.items():
            if len(keyword) >= 2 and keyword.lower() in msg_lower:
                if reply == "TIME_GREETING":
                    reply = _get_time_enhanced_greeting()
                return reply

    return None


def _enhance_greeting_with_context(base_reply: str, context: dict) -> str:
    from memory import get_chat_messages
    
    try:
        session_id = context.get('session_id', '')
        if session_id:
            messages = get_chat_messages(session_id)
            user_name = _extract_user_name(messages)
            if user_name:
                hour = time.localtime().tm_hour
                if 5 <= hour < 12:
                    time_word = "早上好"
                elif 12 <= hour < 18:
                    time_word = "下午好"
                else:
                    time_word = "晚上好"
                return f"{time_word}，{user_name}！有什么可以帮您？"
    except Exception as e:
        logger.warning(f"上下文问候增强失败: {e}")
    
    return base_reply


def _extract_user_name(messages: list) -> str | None:
    for msg in reversed(messages[-10:]):
        content = msg.get('content', '').lower()
        if '我叫' in content:
            match = re.search(r'我叫(.+?)(?:[:：，,\.\!！\s]|$)', content)
            if match:
                name = match.group(1).strip()
                if name and len(name) <= 10:
                    return name
    return None


def _get_time_enhanced_greeting() -> str:
    hour = time.localtime().tm_hour
    
    if 5 <= hour < 9:
        return "早上好！新的一天开始了，有什么需要处理的吗？"
    elif 9 <= hour < 12:
        return "上午好！新的一天，有什么需要处理的吗？"
    elif 12 <= hour < 14:
        return "中午好！有什么需要处理的吗？"
    elif 14 <= hour < 18:
        return "下午好！有什么需要处理的吗？"
    else:
        return "晚上好！有什么需要处理的吗？"


# ── 第2层：高频固化层 ──

# 工作流记录当前版本号
_WORKFLOW_VERSION = 2
# 工作流最少成功次数才能命中（高一点更稳）
_WORKFLOW_MIN_SUCCESS = 3
# 失败次数超过该阈值的固化记录被淘汰
_WORKFLOW_MAX_FAIL = 2


def _msg_hash(user_message: str) -> str:
    normalized = user_message.strip().lower()
    normalized = re.sub(r'[^\w\s]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    if not normalized:
        normalized = user_message.strip()[:100]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _extract_variables(user_message: str, extractor: dict) -> dict:
    """从用户输入里提取变量。extractor 格式: {var_name: {"regex": "..."}}"""
    if not extractor:
        return {}
    variables = {}
    for var_name, spec in extractor.items():
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        if not pattern:
            continue
        try:
            m = re.search(pattern, user_message, re.IGNORECASE)
            if m:
                variables[var_name] = m.group(1) if m.groups() else m.group(0)
        except re.error as e:
            logger.warning(f"变量 {var_name!r} 正则编译失败: {e}")
    return variables


def _resolve_param_value(value, variables: dict, step_captures: dict) -> object:
    """递归解析参数值中的 ${var} 和 ${step.field} 引用"""
    if isinstance(value, str):
        if "${" not in value:
            return value
        # 替换所有 ${xxx} 引用
        def _replace(match):
            ref = match.group(1).strip()
            if "." in ref:
                # ${step_name.field} 形式
                head, *path = ref.split(".")
                if head in step_captures:
                    cur = step_captures[head]
                    for p in path:
                        if isinstance(cur, dict):
                            cur = cur.get(p)
                        elif isinstance(cur, list) and p.isdigit():
                            idx = int(p)
                            cur = cur[idx] if 0 <= idx < len(cur) else None
                        else:
                            return ""
                    return "" if cur is None else str(cur)
                return ""
            if ref in variables:
                return str(variables[ref])
            if ref in step_captures:
                v = step_captures[ref]
                return "" if v is None else str(v)
            return ""
        try:
            return re.sub(r'\$\{([^}]+)\}', _replace, value)
        except Exception:
            return value
    elif isinstance(value, dict):
        return {k: _resolve_param_value(v, variables, step_captures) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_param_value(v, variables, step_captures) for v in value]
    return value


def _load_cached_actions() -> dict:
    if not os.path.exists(_CACHED_ACTIONS_PATH):
        return {"固化记录": {}}
    try:
        with open(_CACHED_ACTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"加载固化缓存失败: {e}")
        backup = _CACHED_ACTIONS_PATH + ".corrupted"
        try:
            os.replace(_CACHED_ACTIONS_PATH, backup)
            logger.warning(f"已将损坏的缓存文件备份至 {backup}")
        except Exception:
            pass
        return {"固化记录": {}}


def _save_cached_actions(data: dict):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                      dir=DATA_DIR, delete=False, suffix=".tmp") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmpname = tf.name
    os.replace(tmpname, _CACHED_ACTIONS_PATH)


def _trim_cached_actions(data: dict) -> dict:
    records = data.get("固化记录", {})
    if len(records) <= _CACHED_ACTIONS_MAX_ENTRIES:
        return data
    
    sorted_records = sorted(records.items(), key=lambda x: x[1].get("success_count", 0), reverse=True)
    trimmed = dict(sorted_records[:_CACHED_ACTIONS_MAX_ENTRIES])
    data["固化记录"] = trimmed
    logger.info(f"固化缓存已裁剪至 {_CACHED_ACTIONS_MAX_ENTRIES} 条")
    return data


def check_frequent_cache(user_message: str) -> tuple[bool, str | None]:
    try:
        cache = _load_cached_actions()
        records = cache.get("固化记录", {})
        msg_hash_val = _msg_hash(user_message)

        for label, record in records.items():
            if record.get("query_hash") != msg_hash_val:
                continue
            version = record.get("version", 1)
            if version < _WORKFLOW_VERSION:
                # legacy 记录：限单工具且必须是缓存安全工具
                if record.get("success_count", 0) < _CACHED_ACTIONS_MIN_SUCCESS:
                    continue
                tool = record.get("固化参数", {}).get("tool", "")
                if _is_unsafe_for_cache(tool):
                    continue
                success, reply = _execute_cached_action(record, user_message)
                if success:
                    logger.info(f"[ROUTE L2 legacy] 命中: {user_message[:50]!r} -> {tool}")
                    return True, reply
            else:
                # v2 workflow 记录
                if record.get("success_count", 0) < _WORKFLOW_MIN_SUCCESS:
                    continue
                success, reply = _execute_workflow(record, user_message)
                if success:
                    logger.info(f"[ROUTE L2 wf] 命中: {user_message[:50]!r} scenario={record.get('scenario_id')}")
                    return True, reply
                else:
                    _increment_fail_count(label, record)

        return False, None
    except Exception as e:
        logger.error(f"高频固化检查失败: {e}")
        return False, None


def _execute_workflow(record: dict, user_message: str) -> tuple[bool, str | None]:
    """执行 v2 工作流固化动作。返回 (成功, 回复文本)。"""
    try:
        extractor = record.get("variable_extractor", {})
        variables = _extract_variables(user_message, extractor)
        workflow = record.get("workflow", [])
        if not workflow:
            return False, None

        # 必需变量是否都提取到了
        for var_name in extractor.keys():
            if var_name not in variables:
                logger.info(f"工作流命中但变量提取不全 缺={var_name} 输入={user_message[:50]!r}")
                return False, None

        step_captures: dict = {}
        summary_lines: list[str] = []

        for idx, step in enumerate(workflow, 1):
            tool_name = step.get("tool", "")
            if not tool_name or tool_name not in TOOL_REGISTRY:
                logger.info(f"工作流 step{idx} 工具 {tool_name!r} 不存在或未注册")
                return False, None
            if _is_unsafe_for_cache(tool_name) and not step.get("require_confirm"):
                # 危险工具必须显式 require_confirm
                logger.warning(f"工作流 step{idx} 危险工具 {tool_name!r} 未标 require_confirm，拒绝执行")
                return False, None
            if step.get("require_confirm"):
                # 危险步骤：这里返回 False 走完整 agent，由 agent 自身发确认卡
                logger.info(f"工作流 step{idx} {tool_name!r} require_confirm，降级为完整agent")
                return False, None

            raw_params = step.get("params", {})
            resolved_params = _resolve_param_value(raw_params, variables, step_captures)
            tool_info = TOOL_REGISTRY[tool_name]
            tool_fn = tool_info["function"]

            # 过滤有效参数
            valid_params = {}
            tool_params_spec = tool_info.get("parameters", {})
            for param_name, param_spec in tool_params_spec.items():
                if param_name in resolved_params:
                    valid_params[param_name] = resolved_params[param_name]
                elif param_spec.get("required"):
                    logger.info(f"工作流 step{idx} 缺必需参数 {param_name!r}")
                    return False, None

            try:
                result = tool_fn(**valid_params)
            except Exception as e:
                logger.error(f"工作流 step{idx} {tool_name!r} 执行异常: {e}")
                return False, None

            # 解析 result 为结构
            result_parsed = None
            if isinstance(result, str):
                try:
                    result_parsed = json.loads(result)
                except json.JSONDecodeError:
                    result_parsed = result
            else:
                result_parsed = result

            # capture
            capture_name = step.get("capture")
            if capture_name:
                step_captures[capture_name] = result_parsed

            # 验证
            expect = step.get("expect")
            if expect:
                if not _verify_step_result(result_parsed, expect):
                    logger.info(f"工作流 step{idx} 验证未通过 expect={expect!r}")
                    return False, None

            # 拼 summary
            summary_lines.append(f"步骤{idx} {tool_name}: {_summarize_result(result_parsed)}")

        summary = "\n".join(summary_lines) or "任务完成"
        if len(summary) > 2000:
            summary = summary[:2000] + "\n\n... (内容过长已截断)"
        return True, summary
    except Exception as e:
        logger.error(f"执行工作流固化动作失败: {e}")
        return False, None


def _verify_step_result(result: any, expect: str) -> bool:
    """验证 step 执行结果。expect 支持: not_found, success, contains:<text>, 简单谓词。"""
    if result is None:
        return False
    if expect == "not_found":
        if isinstance(result, dict):
            if result.get("success") is False:
                return True
            if "found" in result and result["found"] is False:
                return True
            if "programs" in result and isinstance(result["programs"], list) and len(result["programs"]) == 0:
                return True
            text = json.dumps(result, ensure_ascii=False).lower()
            return "not found" in text or "未找到" in text or "不存在" in text
        text = str(result).lower()
        return "not found" in text or "未找到" in text
    if expect == "success":
        if isinstance(result, dict) and result.get("success") is False:
            return False
        return True
    if expect.startswith("contains:"):
        needle = expect[len("contains:"):]
        if isinstance(result, dict):
            return needle in json.dumps(result, ensure_ascii=False)
        return needle in str(result)
    return True


def _summarize_result(result: any) -> str:
    """把工具返回压缩成一行可读 summary"""
    if isinstance(result, dict):
        if "success" in result and isinstance(result["success"], bool):
            if result["success"] is False:
                return f"❌ {result.get('message') or result.get('error') or '失败'}"
            msg = result.get("message") or result.get("summary") or ""
            if msg:
                return "✅ " + str(msg)[:200]
        if "programs" in result and isinstance(result["programs"], list):
            return f"找到 {len(result['programs'])} 个匹配项"
        return json.dumps(result, ensure_ascii=False)[:200]
    s = str(result)
    return s[:200] + ("..." if len(s) > 200 else "")


def _increment_fail_count(label: str, record: dict):
    """固化记录失败计数 + 1，超过阈值则删除记录"""
    try:
        with _CACHED_ACTIONS_LOCK:
            cache = _load_cached_actions()
            records = cache.get("固化记录", {})
            if label not in records:
                return
            rec = records[label]
            rec["fail_count"] = rec.get("fail_count", 0) + 1
            rec["last_used"] = time.time()
            if rec["fail_count"] >= _WORKFLOW_MAX_FAIL:
                del records[label]
                logger.info(f"固化记录淘汰  label={label!r} scenario={rec.get('scenario_id')}")
            _save_cached_actions(cache)
    except Exception as e:
        logger.error(f"固化失败计数更新异常: {e}")


def _execute_cached_action(record: dict, user_message: str) -> tuple[bool, str | None]:
    try:
        tool_name = record.get("固化参数", {}).get("tool", "")
        params = record.get("固化参数", {}).get("params", {})
        template = record.get("固化回复模板", "{result_summary}")

        if not tool_name or tool_name not in TOOL_REGISTRY:
            return False, None

        tool_info = TOOL_REGISTRY[tool_name]
        tool_fn = tool_info["function"]
        
        tool_params = tool_info.get("parameters", {})
        valid_params = {}
        for param_name, param_spec in tool_params.items():
            if param_name in params:
                valid_params[param_name] = params[param_name]
            elif param_spec.get("required"):
                return False, None

        result = tool_fn(**valid_params)

        result_str = ""
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    if parsed.get("success") is False:
                        return False, None
                    result_str = json.dumps(parsed, ensure_ascii=False, indent=2)
                else:
                    result_str = str(result)
            except json.JSONDecodeError:
                result_str = result
        else:
            result_str = str(result)

        if len(result_str) > 2000:
            result_str = result_str[:2000] + "\n\n... (内容过长已截断)"

        reply = template.replace("{result_summary}", result_str)
        return True, reply
    except Exception as e:
        logger.error(f"执行固化动作失败: {e}")
        return False, None


def record_success(
    user_message: str,
    tool_name: str = "",
    params: dict | None = None,
    tool_call_chain: list | None = None,
    run_was_complete: bool = True,
):
    """
    记录一次任务成功。
    - tool_name/params: 兼容旧版单工具调用
    - tool_call_chain: v2 传入，格式 [{"tool": "...", "params": {...}, "result": ...}, ...]
    - run_was_complete: True=完整结束、False=中途被取消/出错
    """
    if not user_message:
        return
    if not run_was_complete:
        # 任务未完整完成 不计数（避免中间步骤重复累积）
        return

    # 优先使用 v2 链式记录
    if tool_call_chain and len(tool_call_chain) >= 1:
        _record_workflow_success(user_message, tool_call_chain)
        return

    # legacy fallback
    if _is_unsafe_for_cache(tool_name):
        logger.debug(f"高频固化跳过写入: 工具 {tool_name!r} 属于不安全列表  user_input={user_message[:50]!r}")
        return

    with _CACHED_ACTIONS_LOCK:
        cache = _load_cached_actions()
        records = cache.setdefault("固化记录", {})
        msg_hash_val = _msg_hash(user_message)

        existing_key = None
        for label, record in records.items():
            if record.get("query_hash") == msg_hash_val:
                existing_key = label
                break

        if existing_key:
            rec = records[existing_key]
            rec["success_count"] = rec.get("success_count", 0) + 1
            rec["固化参数"] = {"tool": tool_name, "params": params or {}}
            rec["last_used"] = time.time()
        else:
            records[msg_hash_val] = {
                "version": 1,
                "kind": "legacy",
                "query_hash": msg_hash_val,
                "success_count": 1,
                "固化参数": {"tool": tool_name, "params": params or {}},
                "固化回复模板": "{result_summary}",
                "last_used": time.time(),
            }

        cache = _trim_cached_actions(cache)
        _save_cached_actions(cache)


def _record_workflow_success(user_message: str, tool_call_chain: list):
    """将完整的 tool_call 链打包成 v2 workflow 固化记录"""
    # 安全检查：链中任何危险工具未带 require_confirm 不记
    workflow_steps = []
    variables_hint = _guess_variables(user_message)
    step_capture_idx = 0
    for call in tool_call_chain:
        tool_name = call.get("tool", "")
        if not tool_name or tool_name not in TOOL_REGISTRY:
            logger.debug(f"工作流记录跳过 未知工具 {tool_name!r}")
            return
        is_unsafe = _is_unsafe_for_cache(tool_name)
        raw_params = call.get("params", {}) or {}

        # 变量化参数里看起来是用户输入的字段
        resolved_params = _param_to_template(raw_params, variables_hint)
        step = {"step": len(workflow_steps) + 1, "tool": tool_name, "params": resolved_params}
        if is_unsafe:
            step["require_confirm"] = True
        workflow_steps.append(step)

    if not workflow_steps:
        return

    # 2026-07-12 修复：拒绝"全为不安全工具"或"无参数变量化"的低质量工作流被固化
    # 背景：用户问"把喇叭声音改成45"，LLM 选了 6 步全 execute_command 链（SendKeys/查pycaw/重复调用），
    # 原逻辑只设 require_confirm=True 就写盘，导致 success_count 累积到 2 后被命中为可重用模板，
    # 下次再问会弹出 6 个连续确认弹窗，重复执行错误的流程。
    # 调整：要求"全为不安全 + 步骤≥3"才拒绝（保护 2 步的合法卸载/安装任务）
    unsafe_count = sum(1 for s in workflow_steps if s.get("require_confirm", False))
    if unsafe_count == len(workflow_steps) and len(workflow_steps) >= 3:
        logger.info(
            f"工作流记录拒绝：全部为不安全工具 ({len(workflow_steps)} 步) "
            f"user_input={user_message[:50]!r}"
        )
        return

    # 拒绝"全是同一个工具重复调用"的工作流（典型的 LLM 死循环模式）
    tool_names = [s.get("tool") for s in workflow_steps]
    if len(set(tool_names)) == 1 and len(tool_names) >= 3:
        logger.info(
            f"工作流记录拒绝：单工具重复 ({tool_names[0]} × {len(tool_names)}) "
            f"user_input={user_message[:50]!r}"
        )
        return

    # 拒绝"无任何变量提取"且"步骤 ≥ 4"的工作流（用户目标难复现）
    has_variable = bool(variables_hint) and any(
        "${" in json.dumps(s.get("params", {}), ensure_ascii=False)
        for s in workflow_steps
    )
    if not has_variable and len(workflow_steps) >= 4:
        logger.info(
            f"工作流记录拒绝：步骤过多({len(workflow_steps)})且无变量提取 "
            f"user_input={user_message[:50]!r}"
        )
        return

    msg_hash_val = _msg_hash(user_message)
    scenario_id = f"auto_{tool_call_chain[0].get('tool', 'unknown')}_{int(time.time())}"

    with _CACHED_ACTIONS_LOCK:
        cache = _load_cached_actions()
        records = cache.setdefault("固化记录", {})

        existing_key = None
        for label, record in records.items():
            if record.get("query_hash") == msg_hash_val:
                existing_key = label
                break

        if existing_key:
            rec = records[existing_key]
            rec["success_count"] = rec.get("success_count", 0) + 1
            rec["last_used"] = time.time()
            rec["workflow"] = workflow_steps
        else:
            records[msg_hash_val] = {
                "version": _WORKFLOW_VERSION,
                "kind": "workflow",
                "scenario_id": scenario_id,
                "query_hash": msg_hash_val,
                "query_patterns": [user_message],
                "variable_extractor": variables_hint,
                "workflow": workflow_steps,
                "success_count": 1,
                "fail_count": 0,
                "last_used": time.time(),
            }

        cache = _trim_cached_actions(cache)
        _save_cached_actions(cache)
        logger.info(f"v2 workflow 固化记录写入/更新  hash={msg_hash_val} steps={len(workflow_steps)}")


def _guess_variables(user_message: str) -> dict:
    """从用户输入里启发式猜测变量名，常见值用 ${var} 引用"""
    variables = {}
    # 软件名
    m = re.search(r"(?:软件|卸载)\s*([\w一-龥]+)", user_message)
    if m:
        variables["sw_name"] = {"regex": r"(?:软件|卸载)\s*([\w一-龥]+)"}
    # 路径
    if re.search(r"[A-Za-z]:\\", user_message) or "/" in user_message:
        variables["path"] = {"regex": r"([A-Za-z]:\\[^\s]+|/[\w/.-]+)"}
    # 端口
    m = re.search(r"端口\s*(\d+)", user_message)
    if m:
        variables["port"] = {"regex": r"端口\s*(\d+)"}
    return variables


def _param_to_template(params: dict, variables_hint: dict) -> dict:
    """把实际参数值里看起来是变量的值改写为 ${var} 引用"""
    if not params or not variables_hint:
        return params
    result = {}
    for k, v in params.items():
        if not isinstance(v, str):
            result[k] = v
            continue
        replaced = False
        for var_name in variables_hint.keys():
            # 启发式：如果值是字母数字组成的短词 并且 出现在 user_message 上下文里
            if var_name == "sw_name" and re.fullmatch(r"[\w一-龥]+", v) and 1 < len(v) < 30:
                result[k] = f"${{{var_name}}}"
                replaced = True
                break
            if var_name == "port" and re.fullmatch(r"\d+", v):
                result[k] = f"${{{var_name}}}"
                replaced = True
                break
        if not replaced:
            result[k] = v
    return result


# ── 第3层：复杂度判断 ──

_complexity_cache: dict = {}
_complexity_cache_lock = threading.Lock()
_COMPLEXITY_CACHE_MAX_SIZE = 500
_COMPLEXITY_CACHE_TTL = 300


def check_complexity(user_message: str, model_choice: dict, context: dict = None) -> str:
    domains = context.get('detected_domains', []) if context else []
    domain_str = "_".join(sorted(domains))
    cache_key = hashlib.md5(f"{user_message}_{model_choice.get('model', '')}_{domain_str}".encode()).hexdigest()
    
    with _complexity_cache_lock:
        if cache_key in _complexity_cache:
            cached_result = _complexity_cache[cache_key]
            if time.time() - cached_result['timestamp'] < _COMPLEXITY_CACHE_TTL:
                return cached_result['result']
            else:
                del _complexity_cache[cache_key]
        
        if len(_complexity_cache) > _COMPLEXITY_CACHE_MAX_SIZE:
            _cleanup_complexity_cache()
    
    msg = user_message.strip()
    
    complexity_score = 0.0
    
    COMPLEX_INDICATORS = {
        "分析": 0.4, "统计": 0.35, "对比": 0.3, "总结": 0.3, "报告": 0.4,
        "生成": 0.35, "批量": 0.3, "替换": 0.25, "重构": 0.4, "安装": 0.35,
        "卸载": 0.35, "配置": 0.3, "修复": 0.35, "诊断": 0.4, "排查": 0.35,
        "优化": 0.3, "迁移": 0.4, "转换": 0.25, "提取": 0.3, "同步": 0.3,
        "架构": 0.45, "设计": 0.4, "规划": 0.35, "策略": 0.35, "方案": 0.3
    }
    
    for indicator, score in COMPLEX_INDICATORS.items():
        if indicator in msg:
            complexity_score = max(complexity_score, score)
    
    if len(msg) > 50:
        complexity_score += 0.1
    elif len(msg) < 10:
        if complexity_score == 0:
            complexity_score = 0.05
    
    if msg.endswith(("?", "？")):
        ACTION_VERBS = ["帮我", "请", "搜索", "下载", "创建", "删除", "修改", "执行", "运行"]
        if any(verb in msg for verb in ACTION_VERBS):
            complexity_score += 0.05
        else:
            complexity_score = max(0, complexity_score - 0.05)
    
    TECH_TERMS = ["API", "数据库", "服务器", "框架", "算法", "架构", "部署", "容器", "微服务"]
    if any(term in msg for term in TECH_TERMS):
        complexity_score += 0.15
    
    MULTI_TASK_INDICATORS = ["然后", "接着", "同时", "另外", "还有", "并且", "最后"]
    if any(indicator in msg for indicator in MULTI_TASK_INDICATORS):
        complexity_score += 0.2
    
    TIME_SENSITIVE = ["紧急", "马上", "立刻", "尽快", "今天"]
    if any(word in msg for word in TIME_SENSITIVE):
        complexity_score += 0.1
    
    if context and 'detected_domains' in context:
        high_complexity_domains = ['system_high_risk', 'excel_analyze', 'code_dev', 'architect']
        detected_domains = context['detected_domains']
        if any(domain in high_complexity_domains for domain in detected_domains):
            complexity_score += 0.15
    
    complexity_score = min(complexity_score, 1.0)
    
    if complexity_score >= 0.7:
        result = "复杂"
    elif complexity_score <= 0.3:
        result = "简单"
    else:
        result = _llm_complexity_judgment(user_message, model_choice, complexity_score)
    
    with _complexity_cache_lock:
        _complexity_cache[cache_key] = {
            'result': result,
            'timestamp': time.time(),
            'score': complexity_score
        }
    
    return result


def _llm_complexity_judgment(user_message: str, model_choice: dict, base_score: float) -> str:
    # 非 Ollama 模型且未配置 API Key 时跳过 LLM 调用，直接使用规则判断
    provider = model_choice.get("provider", "")
    if provider != "ollama":
        model_id = model_choice.get("id")
        api_key = get_api_key(model_id)
        if not api_key:
            logger.info(f"API Key 未配置，跳过 LLM 复杂度判断，使用规则结果: {'复杂' if base_score > 0.3 else '简单'}")
            return "复杂" if base_score > 0.3 else "简单"

    judge_prompt = (
        f'请判断以下用户请求的复杂度。当前基础评分为：{base_score:.1f}（0-1，越高越复杂）\n\n'
        f'用户请求：{user_message}\n\n'
        f'请在考虑基础评分的情况下，综合考虑以下因素：\n'
        f'1. 是否需要多步骤协作完成\n'
        f'2. 是否需要搜索外部信息\n'
        f'3. 是否涉及系统配置修改\n'
        f'4. 是否有技术复杂性\n'
        f'5. 是否需要多个工具配合\n\n'
        f'回复格式：仅回复"复杂"或"简单"'
    )

    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = ai_concat(
            messages=messages,
            model_name=model_choice.get("model"),
            provider=provider,
            trace_id=model_choice.get("model", "complexity"),
            model_id=model_choice.get("id"),
        )

        content = ""
        if result is None:
            content = ""
        elif isinstance(result, str):
            content = result.strip()
        elif isinstance(result, dict):
            content = result.get("message", {}).get("content", "")
            if not content:
                content = result.get("content", "")
            if not content:
                content = result.get("response", "")
            content = content.strip() if isinstance(content, str) else str(content)
        else:
            content = str(result).strip()

        content_lower = content.lower()

        has_complex = "复杂" in content_lower
        has_simple = "简单" in content_lower

        if has_complex and not has_simple:
            return "复杂"
        elif has_simple and not has_complex:
            return "简单"
        elif has_complex and has_simple:
            return "复杂" if base_score > 0.5 else "简单"
        else:
            return "复杂"

    except Exception as e:
        logger.error(f"LLM复杂度判断失败: {e}")
        return "复杂" if base_score > 0.3 else "简单"


def _cleanup_complexity_cache():
    with _complexity_cache_lock:
        current_time = time.time()
        expired_keys = [
            key for key, data in _complexity_cache.items()
            if current_time - data['timestamp'] > _COMPLEXITY_CACHE_TTL
        ]
        for key in expired_keys:
            del _complexity_cache[key]


# ============================================================
# 统一路由入口
# ============================================================

def route(user_message: str, model_choice: dict, context: dict = None) -> dict:
    try:
        chat_reply = check_chat_layer(user_message, context)
        if chat_reply is not None:
            return {"layer": "chat", "reply": chat_reply, "complexity": None}
        
        cache_hit, cache_reply = check_frequent_cache(user_message)
        if cache_hit:
            return {"layer": "cache", "reply": cache_reply, "complexity": None}
        
        complexity = check_complexity(user_message, model_choice, context)
        return {"layer": "complexity", "reply": None, "complexity": complexity}
    
    except Exception as e:
        logger.error(f"路由处理异常: {e}", exc_info=True)
        return {"layer": "complexity", "reply": None, "complexity": "复杂"}
