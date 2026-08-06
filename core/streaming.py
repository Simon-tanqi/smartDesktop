import json
import re
import time
import threading
import queue
from typing import Dict, Any, Optional, Callable, Iterator, Generator

from logger import get_logger


class SSEStreamer:
    def __init__(self):
        self._callbacks = {}
        self._on_final: Optional[Callable] = None
    
    def on_event(self, event_type: str, callback: Callable):
        self._callbacks[event_type] = callback
    
    def set_on_final(self, callback: Callable):
        """注册 emit_final 回调：每次 emit_final 时触发，用于日志轮次完成等。"""
        self._on_final = callback
    
    def emit(self, event_type: str, **kwargs) -> str:
        payload: Dict[str, Any] = {"type": event_type}
        payload.update(kwargs)
        try:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except (TypeError, ValueError):
            # KWArgs 里含不可序列化对象（如 PyTorch tensor），跳过该字段
            payload_safe = {"type": event_type}
            for k, v in kwargs.items():
                try:
                    json.dumps(v)
                    payload_safe[k] = v
                except (TypeError, ValueError):
                    pass
            return f"data: {json.dumps(payload_safe, ensure_ascii=False)}\n\n"
    
    def emit_step(self, text: str, status: str = "进行中") -> str:
        return self.emit("step", text=text, status=status)
    
    def emit_error(self, text: str) -> str:
        return self.emit("error", text=text)
    
    def emit_final(self, text: str, timestamp: Optional[str] = None) -> str:
        if self._on_final:
            try:
                self._on_final(text, timestamp)
            except Exception:
                pass
        return self.emit("final", text=text, timestamp=timestamp, complete=True)
    
    def emit_tool_call(self, tool_name: str, arguments: Dict[str, Any] = None) -> str:
        return self.emit("tool_call", tool_name=tool_name)
    
    def emit_tool_result(self, tool_name: str, result: Any, args: Dict[str, Any] = None) -> str:
        if isinstance(result, dict):
            status = "success" if result.get("success") else "failed"
            err_msg = result.get("message") or result.get("error") or result.get("output") or ""
            if not isinstance(err_msg, str):
                err_msg = str(err_msg)
            err_msg = err_msg[:150]
        else:
            status = "success"
            err_msg = ""
        payload = {"tool_name": tool_name, "status": status, "args": args or {}}
        if err_msg:
            payload["error"] = err_msg
        return self.emit("tool_result", **payload)
    
    def emit_reasoning(self, text: str) -> str:
        return self.emit("reasoning", text=text)
    
    def emit_confirm(self, confirmation_token: str, title: str = "操作需要确认", 
                      command: str = "", details: dict = None, tool_name: str = "",
                      challenge_phrase: str = "", expires_at: Optional[float] = None) -> str:
        # P3: 确认请求带 5 分钟过期时间戳，前端可据此倒计时自动回传拒绝
        if expires_at is None:
            expires_at = time.time() + 300
        return self.emit(
            "confirm",
            confirmation_token=confirmation_token,
            title=title,
            command=command,
            details=details or {},
            tool_name=tool_name,
            challenge_phrase=challenge_phrase,
            expires_at=expires_at,
        )
    
    def broadcast(self, event_type: str, **kwargs):
        if event_type in self._callbacks:
            try:
                self._callbacks[event_type](**kwargs)
            except Exception:
                pass


class StreamingFormatter:
    @staticmethod
    def format_llm_chunk(chunk: Dict[str, Any]) -> Optional[str]:
        if chunk.get("type") == "chunk":
            if chunk.get("content"):
                return chunk["content"]
            if chunk.get("reasoning"):
                return chunk["reasoning"]
        elif chunk.get("type") == "error":
            err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
            return f"错误: {err_msg}"
        return None
    
    @staticmethod
    def format_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
        if result.get("success"):
            output = result.get("output", "")
            if result.get("is_gui_operation"):
                return f"✅ {tool_name}: GUI程序已启动，需要用户手动操作完成。"
            if not isinstance(output, str):
                output = str(output)
            if len(output) > 200:
                output = output[:200] + "..."
            return f"✅ {tool_name}: {output}"
        else:
            message = result.get("message", "操作失败")
            if not isinstance(message, str):
                message = str(message)
            return f"❌ {tool_name}: {message}"


def format_sse_event(event: Dict[str, Any]) -> Optional[str]:
    """将内部事件字典转换为 SSE 格式字符串。
    
    根据 event_type 分派到不同的 SSE 格式化逻辑，支持 reply / step / final 等事件类型。
    """
    event_type = event.get("type")
    if event_type == "reply":
        # L0 闲聊层回复 → final_answer 格式，确保前端能正常显示
        payload = {
            "type": "final_answer",
            "content": event["content"],
            "layer": event.get("layer", "L0"),
        }
    elif event_type == "step":
        payload = {"type": "step", "text": event.get("text", ""), "status": event.get("status", "进行中")}
    elif event_type == "error":
        payload = {"type": "error", "text": event.get("text", event.get("message", "未知错误"))}
    elif event_type == "final":
        payload = {"type": "final", "text": event.get("text", ""), "timestamp": event.get("timestamp")}
    elif event_type == "plan_created":
        payload = {
            "type": "plan_created",
            "plan_id": event.get("plan_id", ""),
            "steps": event.get("steps", []),
            "goal": event.get("goal", ""),
        }
    elif event_type == "plan_step_start":
        payload = {
            "type": "step_start",
            "index": event.get("index", 0),
            "description": event.get("description", ""),
        }
    elif event_type == "plan_step_done":
        payload = {
            "type": "step_done",
            "index": event.get("index", 0),
            "result": event.get("result", ""),
        }
    elif event_type == "plan_completed":
        payload = {
            "type": "plan_completed",
            "summary": event.get("summary", ""),
        }
    elif event_type == "plan_replanned":
        payload = {
            "type": "plan_replanned",
            "reason": event.get("reason", ""),
            "new_steps": event.get("new_steps", []),
        }
    elif event_type == "upgrade_l2_to_l3":
        payload = {
            "type": "upgrade_l2_to_l3",
            "reason": event.get("reason", ""),
            "target": event.get("target", "L3"),
        }
    elif event_type == "mode":
        payload = {
            "type": "mode",
            "mode": event.get("mode", ""),
        }
    else:
        return None
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── SSE 心跳生产者-消费者模式 ──
# BUG-X-2: 使用 threading + queue 模式，15 秒心跳防止反向代理切断长连接

def stream_response(event_iter: Iterator[str], heartbeat_interval: float = 15.0) -> Generator[str, None, None]:
    """将事件迭代器包装为带 SSE 心跳的生成器。
    
    生产者线程从 event_iter 读取事件并推入队列；
    消费者以 heartbeat_interval 超时从队列取事件，超时时发送 SSE 心跳注释。
    None 哨兵表示生产者结束，消费者退出。
    
    Args:
        event_iter: SSE 事件字符串迭代器（每项为 "data: {...}\\n\\n"）
        heartbeat_interval: 心跳间隔秒数，默认 15 秒
    """
    event_queue: queue.Queue = queue.Queue(maxsize=100)

    def _producer():
        try:
            for event in event_iter:
                while True:
                    try:
                        event_queue.put(event, block=True, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            event_queue.put(None)  # 哨兵

    producer_thread = threading.Thread(target=_producer, daemon=True, name="sse-producer")
    producer_thread.start()

    try:
        while True:
            try:
                item = event_queue.get(timeout=heartbeat_interval)
            except queue.Empty:
                # 超时：发送 SSE 心跳注释，防止 Nginx/负载均衡器切断长连接
                yield ": heartbeat\n\n"
                continue

            if item is None:
                break
            yield item
    finally:
        # 确保生产者线程终止
        event_queue.put(None)


sse_streamer = SSEStreamer()
streaming_formatter = StreamingFormatter()


# ── 模块级工具函数（从 react_engine.py 迁入，纯函数无状态依赖） ──


def strip_code_blocks(content: str) -> str:
    content = re.sub(r'```reasoning\s*[\s\S]*?\s*```', '', content).strip()
    content = re.sub(r'```action\s*[\s\S]*?\s*```', '', content).strip()
    content = re.sub(r'```json\s*[\s\S]*?\s*```', '', content).strip()
    # 移除内联 JSON 对象：用 brace-counting 正确处理含 `}` 的值和多行对象
    for key in ["tool_name", "final_message", "need_capability",
                "current_intent", "decision_rationale", "active_hypothesis",
                "constraint_violations", "unresolved_questions", "plan_adjustment",
                "reasoning_trace"]:
        pattern = r'\{\s*"' + key + r'"\s*:\s*'
        for m in re.finditer(pattern, content):
            start = m.start()
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(content)):
                c = content[i]
                if escape:
                    escape = False
                    continue
                if c == '\\':
                    escape = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        content = content[:start] + content[i + 1:]
                        break
        content = content.strip()
    return content


def _truncate_result(content: str, max_length: int = 5000) -> str:
    if len(content) <= max_length:
        return content
    half = max_length // 2
    return content[:half] + f"\n[...截断：共 {len(content)} 字符...]\n" + content[-half:]


def _build_context_memory_summary(layered_cache: dict) -> list:
    """从分层记忆缓存提取日志/追踪用的 context_memory 摘要列表。

    仅包含非空字段：长期记忆、近期结构化交互、历史摘要、规划信息。
    recent_structured 截断至合理长度，避免日志体积过大。
    """
    if not layered_cache:
        return []
    items = []
    long_term = layered_cache.get("long_term")
    if long_term:
        items.append({"summary": f"[长期记忆] {str(long_term)[:200]}"})
    recent_structured = layered_cache.get("recent_structured")
    if recent_structured:
        items.append({"summary": f"[近期交互] {str(recent_structured)[:300]}"})
    older_summary = layered_cache.get("older_summary")
    if older_summary:
        items.append({"summary": f"[历史摘要] {str(older_summary)[:200]}"})
    plan_info = layered_cache.get("plan_info")
    if plan_info:
        items.append({"summary": f"[规划信息] {str(plan_info)[:200]}"})
    return items


def _strip_reasoning_trace(raw: str) -> tuple[str, str | None]:
    """
    从 LLM 原始输出中剥离 reasoning_trace，返回(用户可见正文, trace JSON文本)。
    处理 <reasoning_trace>...</reasoning_trace> XML 标签格式（output.md）。
    未命中时原样返回 raw。
    """
    if not raw or not raw.strip():
        return raw.strip(), None
    trace_match = re.search(r'<reasoning_trace>\s*(.*?)\s*</reasoning_trace>', raw, re.DOTALL)
    if not trace_match:
        return raw.strip(), None
    visible = re.sub(r'<reasoning_trace>\s*(.*?)\s*</reasoning_trace>', '', raw, flags=re.DOTALL).strip()
    return visible, trace_match.group(1).strip()


def _safe_json_loads(data: str) -> Any:
    if not isinstance(data, str):
        return data

    try:
        from json_repair import loads as repair_loads
        try:
            return repair_loads(data)
        except Exception as repair_err:
            get_logger().warning(f"json_repair 解析失败: {repair_err}", trace_id=None)
    except ImportError:
        pass

    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        pass

    try:
        json_match = re.search(r'\{[\s\S]*\}', data)
        if json_match:
            json_str = json_match.group(0)
            try:
                from json_repair import loads as repair_loads
                return repair_loads(json_str)
            except Exception:
                pass
            try:
                return json.loads(json_str)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if data.strip().startswith('['):
            json_match = re.search(r'\[[\s\S]*\]', data)
            if json_match:
                json_str = json_match.group(0)
                try:
                    from json_repair import loads as repair_loads
                    return repair_loads(json_str)
                except Exception:
                    pass
                try:
                    return json.loads(json_str)
                except Exception:
                    pass
    except Exception:
        pass

    get_logger().warning(f"_safe_json_loads 无法解析数据: {data[:500]}", trace_id=None)
    return {"success": False, "message": f"JSON解析失败，原始数据前200字符: {data[:200]}", "raw_data": data}


# 模块级别的工作函数，避免 multiprocessing 在 Windows 上无法 pickle 局部函数
def _tool_worker(result_dict, func, args):
    """在子进程中执行工具函数的入口"""
    try:
        result_dict["result"] = func(**args)
    except Exception as e:
        result_dict["error"] = e


def parse_reasoning_trace(content: str) -> Optional[Dict[str, Any]]:
    try:
        from json_repair import loads as repair_loads
        has_json_repair = True
    except ImportError:
        repair_loads = None
        has_json_repair = False

    try:
        # 1. 提取 ```reasoning 代码块内容
        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', content)
        if reasoning_match:
            reasoning_content = reasoning_match.group(1).strip()
            # 1a. 尝试解析为 JSON
            try:
                if has_json_repair and repair_loads:
                    parsed = repair_loads(reasoning_content)
                else:
                    parsed = json.loads(reasoning_content)
                if isinstance(parsed, dict):
                    if "reasoning_trace" in parsed:
                        return parsed["reasoning_trace"]
                    # 如果顶层没有 reasoning_trace key，尝试直接使用
                    if all(k in parsed for k in ("current_intent", "decision_rationale")):
                        return parsed
            except Exception:
                pass
            # 1b. JSON 解析失败，作为纯文本 fallback
            return {
                "current_intent": reasoning_content[:200] + ("..." if len(reasoning_content) > 200 else ""),
                "decision_rationale": reasoning_content[:500] + ("..." if len(reasoning_content) > 500 else ""),
                "active_hypothesis": ""
            }

        # 2. 查找内联 JSON（无代码块包裹）
        json_match = re.search(r'\{\s*"reasoning_trace"\s*:\s*\{', content)
        if json_match:
            start = json_match.start()
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(content)):
                c = content[i]
                if escape:
                    escape = False
                    continue
                if c == '\\':
                    escape = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            if has_json_repair and repair_loads:
                                parsed = repair_loads(content[start:i + 1])
                            else:
                                parsed = json.loads(content[start:i + 1])
                            if isinstance(parsed, dict) and "reasoning_trace" in parsed:
                                return parsed["reasoning_trace"]
                        except Exception:
                            pass
                        break

        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════
# 从 ReActEngine 迁移的 staticmethod（无状态依赖）
# ═══════════════════════════════════════════════

def tool_call_signature(tool_name: str, tool_args: dict) -> str:
    """生成工具调用的唯一签名（名称 + 参数）"""
    import hashlib
    payload = json.dumps(
        {"name": tool_name, "args": tool_args},
        sort_keys=True, ensure_ascii=False
    )
    return hashlib.md5(payload.encode()).hexdigest()


def is_file_not_found(text: str) -> bool:
    """判断错误文本是否属于'文件不存在'类错误"""
    if not text:
        return False
    patterns = ["文件不存在", "No such file", "File not found",
                "找不到文件", "路径不存在", "The system cannot find",
                "does not exist", "No such directory",
                "Not a directory", "不是一个目录"]
    for p in patterns:
        if p.lower() in text.lower():
            return True
    return False


def classify_tool_failure(tool_result: dict) -> str:
    """分类工具失败类型"""
    error = tool_result.get("error", "")
    message = tool_result.get("message", "")
    code = tool_result.get("error_code", "")
    # 状态为 empty 的结果（如 web_search 过滤后无结果）→ 空结果
    if tool_result.get("status") == "empty":
        return "empty_result"
    if code in ("AUTH_FAILED", "PERMISSION_DENIED", "TOOL_NOT_FOUND"):
        return "fatal"
    if code in ("TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR"):
        return "retryable"
    # "文件不存在"类错误不计入循环计数
    if is_file_not_found(error) or is_file_not_found(message):
        return "file_not_found"
    return "unknown"


def validate_tool_dependency(tool_name: str) -> Optional[str]:
    """验证工具依赖是否可用，返回错误提示或 None"""
    from utils.dependency_manager import feature_availability
    feature_map = {
        "system_audio": "system_audio",
        "execute_command": "execute_command",
        "run_python": "run_python",
        "read_file": "file_operations",
        "write_file": "file_operations",
        "web_search": "web_browse",
        "web_download": "web_browse",
        "create_word_document": "office_documents",
        "create_presentation": "office_documents",
        "gui_click": "gui_automation",
        "gui_screenshot": "gui_automation",
        "modify_registry": "registry",
    }
    alternative_map = {
        "modify_registry": "可以尝试使用 execute_command 配合 reg.exe 命令来操作注册表",
        "system_audio": "可以尝试使用 execute_command 调用系统命令来调整音频",
        "gui_click": "可以尝试使用 execute_command 配合其他工具来完成自动化操作",
        "gui_screenshot": "可以尝试使用 execute_command 调用系统截图工具",
    }
    feature_name = feature_map.get(tool_name)
    if feature_name and not feature_availability.check_feature(feature_name):
        alternative = alternative_map.get(tool_name, "")
        base_msg = f"工具 '{tool_name}' 需要的依赖未安装，请检查依赖配置。"
        if alternative:
            return f"{base_msg} {alternative}"
        return base_msg
    return None


def strip_temp_system_messages(messages):
    """移除历史临时 system 消息（[格式纠错]/[进展控制器]），防止动态反馈跨轮累积；
    按内容标记精准识别，不影响固定 system prompt / 记忆注入 / 跨Turn历史 / 压缩摘要。"""
    return [m for m in messages if not (
        m.get("role") == "system"
        and isinstance(m.get("content"), str)
        and ("[格式纠错]" in m["content"] or "[进展控制器]" in m["content"])
    )]


def inline_compress(summaries, max_chars=800):
    """轻量压缩思考摘要：当摘要总字符数超限时，逐对合并相邻摘要。"""
    if not summaries:
        return summaries
    while len(summaries) > 1:
        total_chars = sum(
            len(str(s.get("current_intent", ""))) + len(str(s.get("decision_rationale", "")))
            for s in summaries
        )
        if total_chars <= max_chars:
            break
        s1, s2 = summaries[0], summaries[1]
        merged = {
            "current_intent": f"{str(s1.get('current_intent', ''))[:150]} | {str(s2.get('current_intent', ''))[:150]}",
            "decision_rationale": f"{str(s1.get('decision_rationale', ''))[:200]}；{str(s2.get('decision_rationale', ''))[:200]}",
            "active_hypothesis": s2.get("active_hypothesis", "") or s1.get("active_hypothesis", ""),
        }
        summaries = [merged] + summaries[2:]
    return summaries