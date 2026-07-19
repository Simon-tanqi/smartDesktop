import json
import time
import datetime
import re
import os
import threading
import multiprocessing
from multiprocessing import Manager
from typing import Dict, Any, List, Optional, Callable, Generator
from utils.dependency_manager import feature_availability

from llm_client import (
    ai_concat, parse_ai_json,
    get_available_models, _ollama_call_stream, _deepseek_call_stream,
    get_api_key, get_base_url,
)
from tools import (
    build_dynamic_system_prompt, build_tools_param,
    match_domains, match_persona, get_session_domains,
    add_domain_to_session,
    load_domain_skills, ask_user_clarification,
)
from memory import (
    get_relevant_memory, log_session_turn,
    append_session_message, log_trace, build_context_messages,
    get_full_session_messages,
    estimate_messages_tokens, get_last_turn, update_last_turn,
)
from logger import get_logger
from utils.error_mapper import translate_error

from .agent_state import (
    AgentTurnState, AgentExecutionState, global_state_manager,
    checkpoint_manager, TaskCheckpoint
)
from .streaming import SSEStreamer, StreamingFormatter

# BM25 上下文路由：纯统计任务切换检测，无需 LLM / 向量模型 / 关键词枚举
from memory.context_router import ContextSwitchDetector


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
        import re
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
        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', content)
        if reasoning_match:
            reasoning_content = reasoning_match.group(1).strip()
            try:
                if has_json_repair and repair_loads:
                    parsed = repair_loads(reasoning_content)
                else:
                    parsed = json.loads(reasoning_content)
                if isinstance(parsed, dict) and "reasoning_trace" in parsed:
                    return parsed["reasoning_trace"]
            except Exception:
                pass
        
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
        
        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', content)
        if reasoning_match:
            reasoning_content = reasoning_match.group(1).strip()
            return {"current_intent": reasoning_content[:200] + ("..." if len(reasoning_content) > 200 else ""),
                    "decision_rationale": reasoning_content,
                    "reasoning_summary": reasoning_content[:100] + ("..." if len(reasoning_content) > 100 else "")}
        
        return None
    except Exception:
        return None


class ReActEngine:
    MAX_TURNS = 15
    MAX_CONSECUTIVE_PROGRESS = 3
    MAX_SAME_TOOL_STREAK = 5
    MAX_EXACT_REPEAT = 2
    MAX_CONTEXT_TOKENS = 28000
    SAFETY_MARGIN = 4000
    MAX_RESULT_CHARS = 8000
    MAX_FORCED_ABORT = 8
    MAX_CONSECUTIVE_FAILURES = 5
    WARNING_TURN = 12
    CONFIRMATION_TIMEOUT = 300
    WRITE_TOOLS = {"execute_command", "modify_registry", "write_file", "edit_file",
                   "replace_in_file", "move_file", "copy_file", "delete_file",
                   "scan_directory"}
    BUILD_TOOLS = {"ppt_add_slide", "ppt_add_text", "ppt_add_image", "ppt_add_chart",
                   "ppt_create", "excel_write", "excel_pivot", "excel_format",
                   "excel_chart_advanced", "excel_merge", "excel_formula",
                   "excel_convert", "excel_conditional_format", "excel_batch_process",
                   "write_file", "edit_file", "replace_in_file"}
    
    def __init__(self, trace_id: str = "", session_id: str = "", session_name: str = ""):
        self.trace_id = trace_id
        self.session_id = session_id
        self.session_name = session_name
        self.streamer = SSEStreamer()
        self.formatter = StreamingFormatter()
        self.execution_state = AgentExecutionState()
        self.context_detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
        self._turn_state: Optional[AgentTurnState] = None  # 当前轮次状态引用，供 _on_final 回调使用
    
    
    def run(self, user_message: str, model_choice: Dict[str, Any],
            confirmation_handler: Optional[Callable] = None,
            planning_extra: str = "",
            resumed_checkpoint: Optional[TaskCheckpoint] = None,
            cancel_event=None) -> Generator[str, None, None]:

        
        model_id = model_choice.get("id")
        provider = model_choice["provider"]
        model_name = model_choice["model"]
        temperature = model_choice.get("temperature", 0.75)
        base_url = model_choice.get("base_url", "https://api.deepseek.com")
        
        known_models = [m["model"] for m in get_available_models()]
        known_providers = {m["model"]: m["provider"] for m in get_available_models()}
        need_fallback = False
        
        if model_name not in known_models:
            need_fallback = True
        elif known_providers.get(model_name) != provider:
            need_fallback = True
        
        if need_fallback:
            available = get_available_models()
            fallback_model = available[0] if available else {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}
            model_choice = dict(fallback_model)
            provider = model_choice["provider"]
            model_name = model_choice["model"]
            model_id = model_choice.get("id")
            base_url = model_choice.get("base_url", "https://api.deepseek.com")

        default_model = model_name
        default_provider = provider
        call_seq = [0]
        self.completed_steps = []
        remaining_steps = []
        
        yield self.streamer.emit_step("开始分析问题...")
        get_logger().info("Agent启动", trace_id=self.trace_id, session_id=self.session_id, 
                          user_message=user_message[:100])
        
        cached_long_term_memory = ""
        cached_short_term_memory = ""
        _matched_domains = []
        _switch_result = None  # 由正常路径填充，BM25 段复用
        
        try:
            if resumed_checkpoint:
                yield self.streamer.emit_step(f"正在恢复任务，上次执行到第 {resumed_checkpoint.turn} 轮")
                self.completed_steps = resumed_checkpoint.completed_steps
                remaining_steps = resumed_checkpoint.remaining_steps
                
                context_messages = build_context_messages(self.session_id, is_continuation=True)
                short_term_parts = []
                for m in context_messages:
                    if m["role"] == "system":
                        short_term_parts.append(m["content"])
                cached_short_term_memory = "\n\n".join(short_term_parts) if short_term_parts else ""
                
                cached_long_term_memory = get_relevant_memory(user_message)
                
                loaded_domains = get_session_domains(self.session_id)
                _matched_domains = list(loaded_domains)
                
                full_system = build_dynamic_system_prompt(domains=_matched_domains, persona_mode="")
                
                resume_prompt = f"""【任务恢复】以下是之前未完成任务的状态，请从第{resumed_checkpoint.turn+1}步继续执行：

已完成步骤：
{json.dumps(self.completed_steps, ensure_ascii=False, indent=2)}

剩余步骤：
{json.dumps(remaining_steps, ensure_ascii=False)}

当前进度：{resumed_checkpoint.progress_summary}

请继续执行后续步骤，不要重复已完成的工作。"""
                
                full_system = full_system + "\n\n" + resume_prompt
                self._full_system = full_system
                
                checkpoint_manager.mark_as_resumed(resumed_checkpoint.checkpoint_id)
                
            else:
                t0 = time.perf_counter()
                domains = match_domains(user_message)
                persona_mode = match_persona(user_message)
                _matched_domains = domains
                for _d in _matched_domains:
                    add_domain_to_session(self.session_id, _d)
                elapsed_i = int((time.perf_counter() - t0) * 1000)
                log_trace(self.trace_id, "intent.match_domains", "intent", elapsed_i,
                          session_id=self.session_id, status="success",
                          input_summary=user_message[:200], output_summary=str(domains))
                get_logger().info("域匹配完成", trace_id=self.trace_id, session_id=self.session_id, 
                                  domains=domains, persona=persona_mode, elapsed_ms=elapsed_i)
                
                # BM25 上下文路由：提前到 build_context_messages 之前执行，
                # 将引擎的判定结果传入 context builder，避免两套检测器结论矛盾
                _switch_result = self.context_detector.detect(user_message)
                get_logger().debug(f"[上下文路由] BM25 检测结果: is_switch={_switch_result['is_switch']}, "
                                  f"sim={_switch_result['norm_similarity']}, reason={_switch_result['reason']}, "
                                  f"new_terms={_switch_result.get('new_unique_terms', [])}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                
                context_messages = build_context_messages(
                    self.session_id,
                    is_continuation=not _switch_result["is_switch"]
                )

                short_term_parts = []
                for m in context_messages:
                    if m["role"] == "system":
                        short_term_parts.append(m["content"])
                short_term_context = "\n\n".join(short_term_parts) if short_term_parts else ""

                relevant_memory = get_relevant_memory(user_message)

                cached_long_term_memory = relevant_memory
                cached_short_term_memory = short_term_context

                full_system = build_dynamic_system_prompt(
                    domains=domains, persona_mode=persona_mode
                )
                self._full_system = full_system

                log_session_turn("user", user_message, trace_id=self.trace_id,
                                 session_id=self.session_id)
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield self.streamer.emit_error(translate_error(str(e)[:500]))
            return
        
        state = global_state_manager.create_turn_state(user_message)
        # 从 session db 读取真实 turn 号（除恢复场景外），避免所有轮次挤在 turn=0
        last_turn = get_last_turn(self.session_id)
        if resumed_checkpoint:
            state.turn = resumed_checkpoint.turn
        elif last_turn > 0:
            state.turn = last_turn + 1
        else:
            state.turn = max(last_turn, 0)  # 首次对话 turn=0
        self._turn_state = state
        # 挂钩 emit_final 回调：每次 emit_final 时自动记录轮次完成 + 更新 turn 号
        def _on_emit_final(text: str, timestamp: Optional[str]):
            try:
                ts = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                get_logger().log_turn_complete(
                    session_id=self.session_id,
                    session_name=self.session_name,
                    turn=state.turn,
                    ai_reply=(text or "")[:2000],
                    ai_reply_time=ts,
                )
                update_last_turn(self.session_id, state.turn)
            except Exception:
                pass
        self.streamer.set_on_final(_on_emit_final)
        get_logger().info(f"初始轮次: {state.turn}, resumed_checkpoint: {resumed_checkpoint}", 
                         trace_id=self.trace_id, session_id=self.session_id)
        
        if resumed_checkpoint:
            start_step = resumed_checkpoint.current_step + 1
            get_logger().info(f"从检查点恢复，轮次设为: {state.turn}，从第 {start_step} 步继续", 
                             trace_id=self.trace_id, session_id=self.session_id)
        else:
            start_step = state.turn

        def _validate_tool_dependency(tool_name: str) -> Optional[str]:
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
        
        def _save_checkpoint(_step: int, tool_name: str = "", tool_args: Dict = None, 
                             progress: str = "", messages_param=None):
            if not self.session_id:
                return
            try:
                checkpoint = TaskCheckpoint(
                    session_id=self.session_id,
                    trace_id=self.trace_id,
                    user_task=user_message,
                    turn=state.turn,
                    current_step=_step, 
                    messages=messages_param or [],
                    completed_steps=self.completed_steps,
                    remaining_steps=remaining_steps,
                    current_tool_name=tool_name,
                    current_tool_args=tool_args or {},
                    progress_summary=progress,
                )
                checkpoint_manager.save_checkpoint(checkpoint)
                get_logger().info(f"检查点已保存: {checkpoint.checkpoint_id}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
            except Exception as e:
                get_logger().error(f"保存检查点失败: {e}", trace_id=self.trace_id, session_id=self.session_id)
        
        def llm_stream(messages: List[Dict[str, Any]], system_prompt: str = "", 
                       user_input: str = "") -> Generator[Any, None, None]:
            idx = call_seq[0]
            call_seq[0] += 1
            t0 = time.perf_counter()
            
            tools_param = None
            if self.session_id:
                loaded_domains = get_session_domains(self.session_id)
                tools_param = build_tools_param(loaded_domains=list(loaded_domains))
            
            if tools_param is None and _matched_domains:
                tools_param = build_tools_param(loaded_domains=_matched_domains)
            
            max_retries = 2
            retry_delays = [5, 15]
            
            _in_reasoning_block = False
            for attempt in range(max_retries + 1):
                full_content = ""
                full_reasoning = ""
                retry_needed = False
                
                try:
                    api_key = get_api_key(model_id)
                    base_url = get_base_url(model_id) or base_url
                    get_logger().info(
                        f"LLM调用: provider={default_provider}, model={default_model}, "
                        f"api_key={'已配置' if api_key else '⚠️ 为空'}, base_url={base_url}",
                        trace_id=self.trace_id, session_id=self.session_id
                    )
                    if default_provider == "ollama":
                        stream_gen = _ollama_call_stream(messages, default_model, trace_id=self.trace_id, tools=tools_param)
                    elif not api_key:
                        get_logger().error(f"API Key 为空，无法调用{default_provider}模型", trace_id=self.trace_id)
                        yield {"type": "error", "message": f"{default_provider} API Key 未配置，请在设置中添加 API Key"}
                        return
                    else:
                        stream_gen = _deepseek_call_stream(messages, default_model,
                            api_key, base_url,
                            trace_id=self.trace_id, tools=tools_param,temperature=temperature)
                    
                    last_heartbeat = time.time()
                    for chunk in stream_gen:
                        if cancel_event and cancel_event.is_set():
                            yield {"type": "cancelled", "message": "任务已取消"}
                            return

                        if time.time() - last_heartbeat > 10:
                            yield ": keepalive\n\n"
                            last_heartbeat = time.time()

                        content = chunk.get("content", "")
                        reasoning = chunk.get("reasoning", "")

                        if content or reasoning:
                            full_content += content
                            full_reasoning += reasoning
                            
                            get_logger().debug(f"llm_stream原始chunk: content前100字符={repr(content[:100])}, reasoning前100字符={repr(reasoning[:100])}, in_block={_in_reasoning_block}",
                                              trace_id=self.trace_id, session_id=self.session_id)
                            
                            if _in_reasoning_block:
                                reasoning_end = content.find("```")
                                if reasoning_end != -1:
                                    reasoning_content = content[:reasoning_end]
                                    content = content[reasoning_end + 3:]
                                    _in_reasoning_block = False
                                else:
                                    reasoning_content = content
                                    content = ""
                            else:
                                reasoning_start = content.find("```reasoning")
                                if reasoning_start != -1:
                                    reasoning_end = content.find("```", reasoning_start + 12)
                                    if reasoning_end != -1:
                                        reasoning_content = content[reasoning_start + 12:reasoning_end]
                                        content = content[:reasoning_start] + content[reasoning_end + 3:]
                                    else:
                                        reasoning_content = content[reasoning_start + 12:]
                                        content = content[:reasoning_start]
                                        _in_reasoning_block = True
                                else:
                                    reasoning_content = ""
                            
                            if reasoning_content.strip():
                                yield {
                                    "type": "chunk",
                                    "content": content,
                                    "reasoning": reasoning_content.strip(),
                                }
                            else:
                                yield {
                                    "type": "chunk",
                                    "content": content,
                                    "reasoning": reasoning,
                                }
                            continue

                        if chunk.get("type") == "result":
                            full_content = chunk.get("content", full_content)
                            full_reasoning = chunk.get("reasoning_content", full_reasoning)
                            yield {
                                "type": "result",
                                "content": full_content,
                                "reasoning": full_reasoning,
                            }
                            return

                        if chunk.get("type") == "error":
                            err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
                            if attempt < max_retries:
                                full_content = ""
                                full_reasoning = ""
                                get_logger().warning(f"LLM调用错误，第{attempt+1}/{max_retries}次重试", trace_id=self.trace_id, error=err_msg)
                                yield {"type": "retry", "attempt": attempt + 1, "max_retries": max_retries, "message": f"LLM调用失败，第{attempt+1}/{max_retries+1}次重试..."}
                                retry_needed = True
                                break
                            yield {"type": "error", "message": translate_error(err_msg)}
                            return
                    
                    if retry_needed:
                        continue
                    
                    actual_user_input = user_input
                    for m in reversed(messages):
                        if m.get("role") == "user":
                            actual_user_input = m.get("content", "")
                            break
                    
                    reasoning_trace = parse_reasoning_trace(full_content)
                    reasoning_trace_str = json.dumps(reasoning_trace, ensure_ascii=False) if reasoning_trace else ""
                    
                    elapsed = int((time.perf_counter() - t0) * 1000)
                    get_logger().log_llm_call(
                        trace_id=self.trace_id, session_id=self.session_id,
                        turn=state.turn, child_turn=state.child_turn,
                        user_task=state.user_task,
                        response=full_content,
                        model_name=default_model, provider=default_provider,
                        elapsed_ms=elapsed, session_name=self.session_name,
                        reasoning=reasoning_trace_str if reasoning_trace_str else full_reasoning,
                        system_prompt=system_prompt,
                        user_input=actual_user_input,
                        messages=messages,
                        user_task_time=state.user_task_time,
                    )
                    state.child_turn += 1
                    yield {"content": full_content, "reasoning_content": full_reasoning, "reasoning_trace": reasoning_trace}
                    return
                    
                except Exception as e:
                    if attempt < max_retries:
                        get_logger().warning(f"LLM流式调用异常，第{attempt+1}次重试", 
                                            trace_id=self.trace_id, error=str(e))
                        if cancel_event is not None:
                            cancel_event.wait(retry_delays[attempt])
                        else:
                            time.sleep(retry_delays[attempt])
                    else:
                        yield {"type": "error", "message": translate_error(str(e))}
                        return
        
        def save_ai_thinking(summary: str):
            """保存 AI 思考过程摘要（msg_type='think'，不参与上下文构建）"""
            if self.session_id and summary:
                append_session_message(self.session_id, "assistant", summary,
                                       trace_id=self.trace_id, msg_type="think",
                                       skip_compress=True)

        # save_ai_reply 已移除 —— AI 消息持久化收敛为前端 saveSessionToBackend 单一入口
        _cache = {}
        _last_tool_name = ""
        _last_args_norm = ""
        _consecutive_same_tool = 0
        _consecutive_tool_failures = {}
        _global_consecutive_failures = 0
        _failure_breaker_msg_injected = False
        _breaker_hits = 0
        _last_error_category = ""
        _last_exception_type = ""
        _thinking_summaries = []
        _tool_call_history = []
        
        def reset_breaker_state():
            nonlocal _breaker_hits, _last_error_category, _last_exception_type, _consecutive_same_tool, _consecutive_tool_failures, _failure_breaker_msg_injected, _global_consecutive_failures
            _breaker_hits = 0
            _last_error_category = ""
            _last_exception_type = ""
            _consecutive_same_tool = 0
            _consecutive_tool_failures = {}
            _global_consecutive_failures = 0
            _failure_breaker_msg_injected = False

        final_reply_saved = False
        consecutive_empty_turns = 0
        state.child_turn = 0

        def _build_user_content(*, step_num=0):
            """构建用户消息各部分，统一正常路径和压缩路径"""
            parts = []
            
            # 1. 长期记忆（首次执行时发送，后续不变不重复发）
            # step_num == start_step 确保在新任务（start_step=0）和恢复任务（start_step>0）时都能添加长期记忆
            if step_num == start_step and cached_long_term_memory:
                parts.append(f"{cached_long_term_memory}")
            
            # 2. 短期记忆
            if cached_short_term_memory:
                parts.append(f"{cached_short_term_memory}")
            
            # 3. 已完成步骤概览
            if self.completed_steps:
                steps_list = []
                for s in self.completed_steps[-5:]:
                    # 修复问题4：避免直接截断 JSON 字符串导致格式破坏
                    # 改为转换为字符串后再截断，保证文本可读性
                    args_dict = s.get('arguments', {})
                    # 生成易读的参数摘要
                    args_items = [f"{k}={v}" for k, v in args_dict.items()]
                    args_preview = ", ".join(args_items)
                    if len(args_preview) > 60:
                        args_preview = args_preview[:57] + "..."

                    steps_list.append(f"{s.get('tool_name','')}: {args_preview}")
                parts.append(f"【已完成步骤】\n" + "\n".join(steps_list))
            
            # 4. 用户任务
            parts.append(f"【任务】\n{user_message}")
            
            # 5. AI上一轮工具调用
            if __history:
                last_tc = __history[-1]
                result_snippet = str(last_tc.get('result', ''))[:800]
                tool_info = f"工具：{last_tc['name']} | 参数：{last_tc['args']} | 状态：{last_tc['status']} | 结果：{result_snippet}"
                parts.append(f"【上一步】\n{tool_info}")
            
            # 6. 思考轨迹（紧凑格式）
            if _thinking_summaries:
                trace_lines = []
                for i, rt in enumerate(_thinking_summaries[-2:]):
                    items = [f"#{i+1}"]
                    if rt.get("current_intent"):
                        items.append(rt["current_intent"])
                    if rt.get("decision_rationale"):
                        items.append(f"理由:{rt['decision_rationale'][:60]}")
                    if rt.get("plan_adjustment"):
                        items.append(f"调整:{rt['plan_adjustment'][:60]}")
                    trace_lines.append(" | ".join(items))
                parts.append(f"【思考轨迹】\n" + "\n".join(trace_lines))
            
            # 7. 智能提示与卸载路径引导
            # 修复问题2 & 3：直接从 completed_steps 获取原始对象，避免解析截断的字符串；且立即触发提示
            uninstall_string_found = ""
            
            # 检查最近一次已完成的步骤
            if self.completed_steps:
                last_step = self.completed_steps[-1]
                
                # 只要上一步是获取卸载命令且执行成功，立即尝试提取路径
                if last_step.get("tool_name") == "get_uninstall_string":
                    # 直接获取原始结果对象（通常是字典），避免 JSON 解析错误
                    raw_result = last_step.get("result")
                    result_obj = {}
                    
                    # 兼容处理：结果可能是字典（直接用）或字符串（需解析）
                    if isinstance(raw_result, dict):
                        result_obj = raw_result
                    elif isinstance(raw_result, str):
                        # 尝试解析，如果解析失败（截断等情况），使用正则兜底
                        try:
                            result_obj = json.loads(raw_result)
                        except json.JSONDecodeError:
                            # 解析失败，尝试正则提取关键路径
                            match = re.search(r'"UninstallString":\s*"((?:[^"\\]|\\.)*)"', raw_result)
                            if match:
                                uninstall_string_found = match.group(1).strip('"').strip("'")
                    
                    # 如果成功解析为字典，提取路径
                    if not uninstall_string_found and isinstance(result_obj, dict):
                        programs = result_obj.get("programs", [])
                        if programs:
                            prog = programs[0] # 取第一个
                            uninstall_string_found = prog.get("UninstallString") or prog.get("QuietUninstallString") or ""
                            if uninstall_string_found:
                                uninstall_string_found = uninstall_string_found.strip('"').strip("'")
            
            # 如果找到了卸载路径，生成强制性提示
            if uninstall_string_found:
                parts.append(f"🚨 已获取卸载路径：{uninstall_string_found}\n请立即调用 execute_command 执行卸载。")
            else:
                # 仅当没有获取到卸载路径时，才检查重复调用警告
                if __history:
                    duplicate_warning = ""
                    if len(__history) >= 3:
                        last_three = [tc['name'] for tc in __history[-3:]]
                        if len(set(last_three)) == 1:
                            if last_three[0] not in self.BUILD_TOOLS:
                                duplicate_warning = f"\n\n⚠️ 已连续3次调用同一工具 '{last_three[0]}'，请改用其他方案"
                    if duplicate_warning:
                        parts.append(duplicate_warning.strip())

            # 8. 规划信息
            if planning_extra:
                parts.append(f"【规划信息】\n{planning_extra}")
            
            return "\n\n".join(parts)

        # ── BM25 上下文路由：检测任务切换 ──
        # detect() 已在正常路径中提前执行（确保 build_context_messages 拿到一致判断），
        # 此处复用结果；checkpoint 恢复路径走 else 分支不填充 _switch_result，跳过此段
        switch_result = _switch_result
        if switch_result is None:
            switch_result = {"is_switch": False, "norm_similarity": 1.0, "reason": "checkpoint_resume"}
        
        if switch_result["is_switch"]:
            # 任务切换 → 清空本轮累积的旧上下文
            reset_breaker_state()
            self.completed_steps.clear()
            _thinking_summaries.clear()
            _tool_call_history.clear()
            cached_short_term_memory = ""
            cached_long_term_memory = get_relevant_memory(user_message)
            start_step = 0
            get_logger().info(f"[上下文路由] ⚠ 检测到任务切换: 旧上下文已清空 | "
                             f"sim={switch_result['norm_similarity']}, "
                             f"distinctive={switch_result.get('new_unique_terms', [])}",
                             trace_id=self.trace_id, session_id=self.session_id)
            yield self.streamer.emit_step(f"检测到新任务，已切换上下文")
        
        # 将当前任务记入历史（不影响后续检测，仅当 detect() 内部管理）
        self.context_detector.commit_task(user_message)

        for step in range(start_step, self.MAX_TURNS):
            # 取消信号检查：在每轮开始前检查，避免取消后继续发起 LLM 调用
            if cancel_event is not None and cancel_event.is_set():
                get_logger().info("检测到取消信号，终止任务循环",
                                  trace_id=self.trace_id, session_id=self.session_id)
                yield self.streamer.emit_final("任务已取消")
                return

            full_content = ""
            full_reasoning = ""
            _llm_reasoning_trace = None
            
            try:
                current_user_content = _build_user_content(step_num=step)
                
                get_logger().debug(f"===== 第{step}轮 LLM 请求上下文 =====", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"用户任务: {user_message[:200]}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"长期记忆: {'有' if cached_long_term_memory else '无'}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"短期记忆: {'有' if cached_short_term_memory else '无'}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"思考摘要数量: {len(_thinking_summaries)}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                for i, rt in enumerate(_thinking_summaries[-2:]):
                    if isinstance(rt, dict):
                        get_logger().debug(f"  思考摘要{i+1}: intent={rt.get('current_intent','')}, rationale={rt.get('decision_rationale','')[:50]}", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"工具记录数量: {len(_tool_call_history)}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                for i, tc in enumerate(_tool_call_history):
                    get_logger().debug(f"  工具记录{i+1}: name={tc['name']}, status={tc['status']}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"用户输入总长度: {len(current_user_content)} 字符", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"===============================", 
                                  trace_id=self.trace_id, session_id=self.session_id)
                
                messages = [
                    {"role": "system", "content": self._full_system},
                    {"role": "user", "content": current_user_content}
                ]
                
                total_tokens = estimate_messages_tokens(messages)
                if total_tokens > self.MAX_CONTEXT_TOKENS:
                    get_logger().warning(f"上下文 token 超过预算 ({total_tokens}/{self.MAX_CONTEXT_TOKENS})，正在压缩思考摘要", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                    
                    MAX_KEEP = 2
                    if len(_thinking_summaries) > MAX_KEEP:
                        early_thinking = _thinking_summaries[:-MAX_KEEP]
                        merged_intents = [rt.get("current_intent", "")[:30] for rt in early_thinking if isinstance(rt, dict)]
                        merged_thinking = {
                            "current_intent": f"早期步骤({len(early_thinking)}个)",
                            "decision_rationale": "；".join(merged_intents),
                            "active_hypothesis": ""
                        }
                        _thinking_summaries = [merged_thinking] + _thinking_summaries[-MAX_KEEP:]
                    
                    current_user_content = _build_user_content(step_num=step)
                    messages = [
                        {"role": "system", "content": self._full_system},
                        {"role": "user", "content": current_user_content}
                    ]
                
                # LLM 调用前检查取消信号
                if cancel_event is not None and cancel_event.is_set():
                    get_logger().info("检测到取消信号，终止任务（LLM 调用前）",
                                      trace_id=self.trace_id, session_id=self.session_id)
                    yield self.streamer.emit_final("任务已取消")
                    return

                yield self.streamer.emit_step("正在调用模型思考...")

                _reasoning_streamed = False
                last_heartbeat = time.time()
                for chunk in llm_stream(messages, system_prompt=self._full_system, user_input=current_user_content):
                    if isinstance(chunk, str) and chunk.startswith(":"):
                        yield chunk
                        continue
                    
                    if time.time() - last_heartbeat > 10:
                        yield ": keepalive\n\n"
                        last_heartbeat = time.time()
                    
                    if isinstance(chunk, dict):
                        if chunk.get("type") == "error":
                            yield self.streamer.emit_error(chunk["message"])
                            _save_checkpoint(step, progress=f"第{step}次LLM调用失败: {chunk['message']}", messages_param=messages)
                            final_reply_saved = True
                            return
                        elif chunk.get("type") == "cancelled":
                            yield self.streamer.emit_final(chunk["message"])
                            final_reply_saved = True
                            return
                        elif chunk.get("type") == "retry":
                            yield self.streamer.emit_step(chunk.get("message", "LLM调用重试中..."))
                            continue
                        elif chunk.get("type") == "chunk":
                            reasoning = chunk.get("reasoning", "")
                            content = chunk.get("content", "")
                            get_logger().debug(f"流式chunk: content前100字符={repr(content[:100])}, reasoning前100字符={repr(reasoning[:100])}",
                                              trace_id=self.trace_id, session_id=self.session_id)
                            if reasoning:
                                _reasoning_streamed = True
                                yield self.streamer.emit_reasoning(reasoning)
                            continue
                        
                        full_content = chunk.get("content", "")
                        full_reasoning = chunk.get("reasoning_content", chunk.get("reasoning", ""))
                        _llm_reasoning_trace = chunk.get("reasoning_trace")
                
                if not full_content.strip():
                    consecutive_empty_turns += 1
                    get_logger().warning(f"LLM返回空内容，第{consecutive_empty_turns}次", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                    if consecutive_empty_turns >= 3:
                        yield self.streamer.emit_final("模型连续多次返回空内容，任务已暂停。请稍后重试或换一种方式描述您的需求。",
                            timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        final_reply_saved = True
                        return
                    continue
                consecutive_empty_turns = 0
                
                reasoning_trace = _llm_reasoning_trace if _llm_reasoning_trace else parse_reasoning_trace(full_content)
                if reasoning_trace:
                    _thinking_summaries.append(reasoning_trace)
                    get_logger().debug(f"思考摘要已追加: intent={reasoning_trace.get('current_intent','')}, rationale={reasoning_trace.get('decision_rationale','')[:50]}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                
                if not _reasoning_streamed:
                    extracted_full_reasoning = full_reasoning
                    if not extracted_full_reasoning and full_content:
                        import re
                        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', full_content)
                        if reasoning_match:
                            extracted_full_reasoning = reasoning_match.group(1).strip()
                    if extracted_full_reasoning.strip():
                        yield self.streamer.emit_reasoning(extracted_full_reasoning)
                
                parsed_action = parse_ai_json(full_content)
                
                get_logger().info(f"parse_ai_json结果: parsed_action={parsed_action}",
                              trace_id=self.trace_id, session_id=self.session_id)
                
                if parsed_action is None:
                    get_logger().warning(f"parse_ai_json返回None，full_content前500字符: {full_content[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    final_text = strip_code_blocks(full_content)
                    get_logger().info(f"strip_code_blocks后final_text前500字符: {final_text[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    if not final_text.strip():
                        final_text = full_content[:1000]
                    
                    get_logger().info(f"最终发送给用户的final_text前500字符: {final_text[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    yield self.streamer.emit_final(final_text, 
                        timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    final_reply_saved = True
                    return
                
                if step == self.WARNING_TURN:
                    if cancel_event is not None and cancel_event.is_set():
                        get_logger().info("检测到取消信号，跳过进度摘要生成",
                                          trace_id=self.trace_id, session_id=self.session_id)
                        yield self.streamer.emit_final("任务已取消")
                        return
                    
                    yield self.streamer.emit_step("⚠️ 即将达到最大思考轮次，正在生成进度摘要...")
                    
                    if self.completed_steps:
                        steps_summary_lines = []
                        for idx, step_data in enumerate(self.completed_steps, 1):
                            tool_name = step_data.get("tool_name", "")
                            arguments = step_data.get("arguments", {})
                            result = step_data.get("result", {})
                            step_msg = f"{idx}. 使用 {tool_name}"
                            if isinstance(arguments, dict):
                                args_str = ", ".join([f"{k}={v}" for k, v in arguments.items()])
                                if args_str:
                                    step_msg += f" ({args_str})"
                            elif isinstance(arguments, str):
                                step_msg += f" ({arguments})"
                            if isinstance(result, dict):
                                success = result.get("success", True)
                                message = result.get("message", "")
                                if message:
                                    step_msg += f" - {message}"
                            steps_summary_lines.append(step_msg)
                        steps_summary = "\n".join(steps_summary_lines)
                    else:
                        steps_summary = "无"
                    
                    progress_prompt = f"""请总结当前任务执行进度：
1. 已完成的步骤
2. 未完成的步骤
3. 当前状态

任务：{user_message}
已完成步骤：
{steps_summary}
"""
                    try:
                        progress_result = ai_concat(
                            messages=[{"role": "user", "content": progress_prompt}],
                            model_name=default_model,
                            provider=default_provider,
                            trace_id=self.trace_id,
                            model_id=model_id,
                            temperature=temperature
                        )
                        progress_summary = progress_result["message"].get("content", "").strip()
                        _save_checkpoint(step, progress=progress_summary, messages_param=messages)
                        yield self.streamer.emit_step(f"进度摘要已保存：{progress_summary[:100]}...")
                    except Exception as e:
                        get_logger().warning("进度摘要生成失败", trace_id=self.trace_id, error=str(e))
                
                need_capability = parsed_action.get("need_capability")
                domain = parsed_action.get("domain")
                
                if need_capability and domain:
                    get_logger().debug(f"收到能力加载请求: need_capability={need_capability}, domain={domain}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                    
                    add_domain_to_session(self.session_id, domain)
                    
                    try:
                        result_data = json.loads(load_domain_skills(domain, need_capability, self.session_id))
                    except json.JSONDecodeError as e:
                        get_logger().error(f"load_domain_skills 返回非法 JSON: {e}", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                        result_data = {"success": False, "message": f"技能域加载失败，返回数据格式错误: {e}"}
                    
                    get_logger().debug(f"域加载结果: success={result_data.get('success')}, domain={result_data.get('domain')}, tools={result_data.get('tools')}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                    
                    step_msg = self._build_step_summary("load_domain_skills", 
                        {"domain": domain, "reason": need_capability}, 
                        result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    
                    yield self.streamer.emit_tool_result("load_domain_skills", 
                        {"domain": domain, "tools": result_data.get("tools", []), 
                         "message": result_data.get("message", ""), "success": result_data.get("success")})
                    
                    continue
                
                tool_name = parsed_action.get("tool_name")
                arguments = parsed_action.get("arguments", {})
                
                if not tool_name:
                    get_logger().info(f"LLM未调用工具，直接回复: {full_content[:200]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    
                    final_text = ""
                    if parsed_action and parsed_action.get("final_message"):
                        final_text = parsed_action.get("final_message")
                        get_logger().info(f"提取final_message作为最终回复: {final_text[:200]}",
                                      trace_id=self.trace_id, session_id=self.session_id)
                    else:
                        final_text = strip_code_blocks(full_content)
                        get_logger().info(f"strip_code_blocks后final_text前200字符: {final_text[:200]}",
                                      trace_id=self.trace_id, session_id=self.session_id)
                        if not final_text.strip():
                            get_logger().warning(f"strip_code_blocks后内容为空，使用原始内容前1000字符",
                                          trace_id=self.trace_id, session_id=self.session_id)
                            final_text = full_content[:1000]
                    
                    get_logger().info(f"最终发送给用户的final_text前500字符: {final_text[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    yield self.streamer.emit_final(final_text, 
                        timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    final_reply_saved = True
                    return
                
                get_logger().info(f"LLM决定调用工具: {tool_name}",
                              trace_id=self.trace_id, session_id=self.session_id)
                yield self.streamer.emit_reasoning(f"决定调用工具: {tool_name}")
                
                dep_error = _validate_tool_dependency(tool_name)
                if dep_error:
                    yield self.streamer.emit_step(f"工具依赖验证失败: {dep_error}", "error")
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                    if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                        args_preview += "..."
                    _tool_call_history.append({
                        "name": tool_name,
                        "args": args_preview,
                        "status": "failed",
                        "result": f"依赖验证失败: {dep_error[:500]}..." if len(dep_error) > 500 else dep_error
                    })
                    step_msg = self._build_step_summary(tool_name, arguments, "", "failed", dep_error, step)
                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    continue
                
                yield self.streamer.emit_tool_call(tool_name, arguments)
                
                try:
                    tool_func = None
                    timeout = 60
                    
                    tool_info = self._get_tool(tool_name)
                    if tool_info:
                        tool_func = tool_info["function"]
                        timeout = tool_info["timeout"]
                    
                    if not tool_func:
                        yield self.streamer.emit_step(f"工具 '{tool_name}' 不存在", "error")
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        _tool_call_history.append({
                            "name": tool_name,
                            "args": args_preview,
                            "status": "failed",
                            "result": f"工具不存在"
                        })
                        step_msg = self._build_step_summary(tool_name, arguments, "", "failed",
                            f"工具 '{tool_name}' 不存在，请检查是否已加载对应技能域", step)
                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        continue
                    
                    # 工具执行前检查取消信号
                    if cancel_event is not None and cancel_event.is_set():
                        get_logger().info(f"检测到取消信号，终止工具执行: {tool_name}",
                                          trace_id=self.trace_id, session_id=self.session_id)
                        yield self.streamer.emit_final("任务已取消")
                        return

                    # 注入 session_id 到 load_domain_skills 等需要会话上下文的工具
                    if tool_name == "load_domain_skills":
                        arguments["session_id"] = self.session_id
                    
                    # 需要修改主进程状态的工具：直接在主进程执行（不走子进程）
                    _IN_PROCESS_TOOLS = {"load_domain_skills", "ask_user_clarification"}
                    if tool_name in _IN_PROCESS_TOOLS:
                        try:
                            raw_result = tool_func(**arguments)
                            result_data = _safe_json_loads(raw_result) if isinstance(raw_result, str) else raw_result
                            if not isinstance(result_data, dict):
                                result_data = {"success": True, "result": raw_result}
                        except Exception as _e:
                            result_data = {"success": False, "message": str(_e)}
                        
                        yield self.streamer.emit_tool_result(tool_name, result_data)
                        
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        _tool_call_history.append({"name": tool_name, "args": args_preview, "status": "success" if result_data.get("success") else "failed", "result": result_data.get("message", "")[:200]})
                        step_msg = self._build_step_summary(tool_name, arguments, result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        _save_checkpoint(step, tool_name, arguments, progress="完成" if result_data.get("success") else result_data.get("message", ""), messages_param=messages)
                        continue  # 跳过子进程路径，进入下一轮 LLM 调用

                    # 注入预验证的卸载路径到 execute_command（跨进程安全设计）
                    if tool_name == "execute_command":
                        try:
                            from tools.system_tools import _uninstall_path_cache, _PATH_CACHE_TTL_SECONDS, _uninstall_path_lock, _is_uninstall_command
                            import time as _t
                            prevalidated = []
                            with _uninstall_path_lock:
                                for _pn, (_ts, _paths) in list(_uninstall_path_cache.items()):
                                    if _t.time() - _ts < _PATH_CACHE_TTL_SECONDS:
                                        prevalidated.extend(list(_paths))
                            
                            cmd = arguments.get("command", "")
                            if _is_uninstall_command(cmd) and not prevalidated:
                                get_logger().info(f"检测到卸载命令但缓存为空，自动获取卸载路径",
                                                  trace_id=self.trace_id, session_id=self.session_id)
                                try:
                                    import re
                                    exe_match = re.search(r'"?([^"\s]+\\\.exe)"?', cmd, re.IGNORECASE)
                                    if exe_match:
                                        exe_path = exe_match.group(1)
                                        parent_dir = os.path.dirname(exe_path)
                                        program_name = os.path.basename(parent_dir)
                                        if not program_name or program_name.lower() in ["uninstall", "bin"]:
                                            program_name = re.sub(r'\.exe$', '', os.path.basename(exe_path), flags=re.IGNORECASE)
                                        
                                        get_uninstall_func = self._get_tool("get_uninstall_string")
                                        if get_uninstall_func:
                                            func = get_uninstall_func["function"]
                                            result = func(program_name)
                                            raw_data = _safe_json_loads(result) if isinstance(result, str) else result
                                            if isinstance(raw_data, dict) and raw_data.get("programs"):
                                                _paths_set = set()
                                                for _p in raw_data["programs"]:
                                                    _us = _p.get("UninstallString") or _p.get("QuietUninstallString") or ""
                                                    if _us:
                                                        _paths_set.add(_us.strip('"').strip("'"))
                                                if _paths_set:
                                                    with _uninstall_path_lock:
                                                        _uninstall_path_cache[program_name.lower()] = (
                                                            _t.time(),
                                                            list(_paths_set)
                                                        )
                                                        prevalidated.extend(list(_paths_set))
                                                        get_logger().info(f"成功获取卸载路径: {len(_paths_set)} 条",
                                                                          trace_id=self.trace_id, session_id=self.session_id)
                                except Exception as _auto_e:
                                    get_logger().debug(f"自动获取卸载路径失败（不影响主流程）: {_auto_e}",
                                                       trace_id=self.trace_id, session_id=self.session_id)
                            
                            if prevalidated:
                                arguments["prevalidated_uninstall_paths"] = prevalidated
                        except Exception as _e:
                            get_logger().debug(f"注入预验证路径失败（不影响主流程）: {_e}",
                                               trace_id=self.trace_id, session_id=self.session_id)

                    manager = Manager()
                    result_container = manager.dict({"result": None, "error": None})

                    worker_process = multiprocessing.Process(target=_tool_worker, args=(result_container, tool_func, arguments))
                    worker_process.start()
                    
                    worker_process.join(timeout=timeout)
                    
                    if worker_process.is_alive():
                        get_logger().warning(f"工具 '{tool_name}' 执行超时，正在终止进程", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                        worker_process.terminate()
                        worker_process.join(timeout=5)
                        if worker_process.is_alive():
                            get_logger().error(f"工具 '{tool_name}' 进程终止失败，尝试强制杀死", 
                                            trace_id=self.trace_id, session_id=self.session_id)
                            try:
                                worker_process.kill()
                                worker_process.join(timeout=2)
                            except Exception:
                                pass
                        yield self.streamer.emit_step(f"工具 '{tool_name}' 执行超时（{timeout}秒），已终止进程", "failed")
                        _consecutive_tool_failures[tool_name] = _consecutive_tool_failures.get(tool_name, 0) + 1
                        if _consecutive_tool_failures[tool_name] >= self.MAX_CONSECUTIVE_FAILURES:
                            yield self.streamer.emit_final(f"工具 '{tool_name}' 连续失败 {_consecutive_tool_failures[tool_name]} 次。建议检查参数、路径或权限，或尝试其他方案。")
                            final_reply_saved = True
                            return
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        _tool_call_history.append({
                            "name": tool_name,
                            "args": args_preview,
                            "status": "failed",
                            "result": f"执行超时（{timeout}秒）"
                        })
                        step_msg = self._build_step_summary(tool_name, arguments, "", "failed",
                            f"工具 '{tool_name}' 执行超时（{timeout}秒）", step)
                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        continue

                    if result_container["error"]:
                        error_msg = translate_error(str(result_container["error"]))
                        yield self.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
                        _consecutive_tool_failures[tool_name] = _consecutive_tool_failures.get(tool_name, 0) + 1
                        if _consecutive_tool_failures[tool_name] >= self.MAX_CONSECUTIVE_FAILURES:
                            yield self.streamer.emit_final(f"工具 '{tool_name}' 连续失败 {_consecutive_tool_failures[tool_name]} 次。建议检查参数、路径或权限，或尝试其他方案。")
                            final_reply_saved = True
                            return
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        _tool_call_history.append({
                            "name": tool_name,
                            "args": args_preview,
                            "status": "failed",
                            "result": f"执行失败: {error_msg[:500]}..." if len(error_msg) > 500 else error_msg
                        })
                        step_msg = self._build_step_summary(tool_name, arguments, "", "failed", error_msg, step)
                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        _save_checkpoint(step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=messages)
                        continue
                    
                    result = result_container["result"]
                    raw_data = _safe_json_loads(result) if isinstance(result, str) else result
                    # 防御：工具可能返回 list/str 等非 dict 类型，统一包装为 dict
                    result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}

                    # get_uninstall_string 路径缓存到主进程，使 execute_command 能通过验证
                    if tool_name == "get_uninstall_string" and result_data.get("programs"):
                        try:
                            import tools.system_tools as _st
                            _paths_set = set()
                            for _p in result_data["programs"]:
                                _us = _p.get("UninstallString") or _p.get("QuietUninstallString") or ""
                                if _us:
                                    _paths_set.add(_us.strip('"').strip("'"))
                            if _paths_set:
                                _prog_name = (arguments.get("program_name") or
                                              (result_data["programs"][0].get("DisplayName", "")))
                                if _prog_name:
                                    with _st._uninstall_path_lock:
                                        _st._uninstall_path_cache[_prog_name.lower()] = (
                                            __import__("time").time(),
                                            list(_paths_set)
                                        )
                        except Exception:
                            pass

                    get_logger().info(f"工具 '{tool_name}' 执行完成: success={result_data.get('success')}, message={result_data.get('message','')[:200]}",
                                  trace_id=self.trace_id, session_id=self.session_id)

                    get_logger().debug(f"confirmation_required={result_data.get('confirmation_required')}, confirmation_handler={confirmation_handler}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    if result_data.get("confirmation_required"):
                        get_logger().debug(f"confirmation_required is True", trace_id=self.trace_id, session_id=self.session_id)
                        get_logger().debug(f"confirmation_handler: {confirmation_handler}", trace_id=self.trace_id, session_id=self.session_id)
                        get_logger().debug(f"confirmation_token: {result_data.get('confirmation_token')}", trace_id=self.trace_id, session_id=self.session_id)
                        if confirmation_handler:
                            token = result_data.get("confirmation_token") or result_data.get("challenge_token")
                            challenge_phrase = result_data.get("challenge_phrase")
                            
                            if token:
                                get_logger().debug(f"Got token, emitting confirm event", trace_id=self.trace_id, session_id=self.session_id)
                                title = result_data.get("message", "操作需要确认")
                                command = arguments.get("command", "")
                                details = result_data.get("details", {})
                                yield self.streamer.emit_confirm(token, title=title, command=command, 
                                                                  details=details, tool_name=tool_name,
                                                                  challenge_phrase=challenge_phrase)
                                get_logger().debug(f"Waiting for confirmation...", trace_id=self.trace_id, session_id=self.session_id)
                                confirmation_result = self._wait_for_confirmation(token, confirmation_handler, timeout=self.CONFIRMATION_TIMEOUT)
                                get_logger().debug(f"Confirmation result: {confirmation_result}", trace_id=self.trace_id, session_id=self.session_id)
                                if confirmation_result.get("confirmed"):
                                    if cancel_event is not None and cancel_event.is_set():
                                        yield self.streamer.emit_final("任务已取消")
                                        final_reply_saved = True
                                        return
                                    
                                    challenge_response = confirmation_result.get("challenge_response", "")
                                    if challenge_phrase:
                                        confirmed_args = {**arguments, "challenge_token": token, "challenge_response": challenge_response}
                                    else:
                                        confirmed_args = {**arguments, "confirmation_token": token}
                                    # 预注册 token：根据工具名分发到对应的 _pending_confirmations
                                    try:
                                        import time as _time
                                        if tool_name == "web_download":
                                            import tools.web_download as _wd
                                            with _wd._pending_lock:
                                                if token not in _wd._pending_confirmations:
                                                    _wd._pending_confirmations[token] = {
                                                        "url": arguments.get("url", ""),
                                                        "output_dir": arguments.get("output_dir", ""),
                                                        "expiry": _time.time() + 300,
                                                    }
                                        else:
                                            import tools.system_tools as _st
                                            with _st._get_lock():
                                                _st._pending_confirmations[token] = (
                                                    command,
                                                    _time.time() + 300
                                                )
                                    except Exception:
                                        pass
                                    try:
                                        confirm_result = tool_func(**confirmed_args)
                                    except Exception as e:
                                        yield self.streamer.emit_step(f"工具执行失败: {e}", "failed")
                                        final_reply_saved = True
                                        return

                                    raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                                    result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                                    yield self.streamer.emit_tool_result(tool_name, result_data)
                                    if result_data.get("is_gui_operation"):
                                        yield self.streamer.emit_final(result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"))
                                        final_reply_saved = True
                                        return
                                    if not result_data.get("success"):
                                        yield self.streamer.emit_final(f"工具执行失败: {result_data.get('message', '未知错误')}")
                                        final_reply_saved = True
                                        return
                                else:
                                    if confirmation_result.get("timeout"):
                                        yield self.streamer.emit_final(confirmation_result.get("message", "确认超时，任务已取消"))
                                    else:
                                        yield self.streamer.emit_final("用户拒绝了操作，任务已取消")
                                    final_reply_saved = True
                                    return
                            elif challenge_phrase:
                                challenge_token = result_data.get("challenge_token")
                                title = result_data.get("message", "高危操作需要确认")
                                command = arguments.get("command", "")
                                details = {"挑战短语": challenge_phrase}
                                yield self.streamer.emit_confirm(challenge_token, title=title, command=command, 
                                                                  details=details, tool_name=tool_name,
                                                                  challenge_phrase=challenge_phrase)
                                confirmation_result = self._wait_for_confirmation(challenge_token, confirmation_handler, timeout=self.CONFIRMATION_TIMEOUT)
                                if confirmation_result.get("confirmed") and confirmation_result.get("challenge_response"):
                                    if cancel_event is not None and cancel_event.is_set():
                                        yield self.streamer.emit_final("任务已取消")
                                        final_reply_saved = True
                                        return
                                    
                                    confirmed_args = {**arguments, "challenge_token": challenge_token, "challenge_response": confirmation_result["challenge_response"]}
                                    # 预注册 challenge token 到主进程
                                    try:
                                        import tools.system_tools as _st
                                        with _st._get_lock():
                                            _st._pending_confirmations[challenge_token] = (
                                                command,
                                                __import__("time").time() + 300
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        confirm_result = tool_func(**confirmed_args)
                                    except Exception as e:
                                        yield self.streamer.emit_step(f"工具执行失败: {e}", "failed")
                                        final_reply_saved = True
                                        return

                                    raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                                    result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                                    yield self.streamer.emit_tool_result(tool_name, result_data)
                                    if result_data.get("is_gui_operation"):
                                        yield self.streamer.emit_final(result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"))
                                        final_reply_saved = True
                                        return
                                    if not result_data.get("success"):
                                        yield self.streamer.emit_final(f"工具执行失败: {result_data.get('message', '未知错误')}")
                                        final_reply_saved = True
                                        return
                                else:
                                    if confirmation_result.get("timeout"):
                                        yield self.streamer.emit_final(confirmation_result.get("message", "确认超时，任务已取消"))
                                    else:
                                        yield self.streamer.emit_final("用户拒绝了操作或挑战短语验证失败，任务已取消")
                                    final_reply_saved = True
                                    return
                            else:
                                yield self.streamer.emit_tool_result(tool_name, result_data)
                        else:
                            yield self.streamer.emit_error("安全确认机制未配置，高危操作已被拒绝，请联系管理员启用确认功能。")
                            final_reply_saved = True
                            return
                    else:
                        get_logger().info(f"工具 '{tool_name}' 执行成功，发送工具结果事件",
                                      trace_id=self.trace_id, session_id=self.session_id)
                        yield self.streamer.emit_tool_result(tool_name, result_data)
                    
                    self.completed_steps.append({
                        "step": step,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result_data,
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    
                    get_logger().info(f"步骤已记录，completed_steps数量: {len(self.completed_steps)}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    
                    if tool_name in _consecutive_tool_failures:
                        del _consecutive_tool_failures[tool_name]
                    _global_consecutive_failures = 0
                    
                    # 回填工具执行结果到 LLM 调用日志
                    _tool_result_str = json.dumps(result_data, ensure_ascii=False) if not isinstance(result_data, str) else result_data
                    get_logger().update_tool_result(
                        session_id=self.session_id,
                        session_name=self.session_name,
                        turn=state.turn,
                        child_turn=state.child_turn - 1,
                        tool_result=_tool_result_str
                    )
                    
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                    if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                        args_preview += "..."
                    result_preview = _truncate_result(_tool_result_str, max_length=self.MAX_RESULT_CHARS)
                    _tool_call_history.append({
                        "name": tool_name,
                        "args": args_preview,
                        "status": "success",
                        "result": result_preview
                    })
                    
                    if len(_tool_call_history) > 20:
                        _tool_call_history = _tool_call_history[-20:]

                    step_msg = self._build_step_summary(tool_name, arguments, _tool_result_str, "success", "", step)

                    if result_data.get("is_gui_operation"):
                        yield self.streamer.emit_final(result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"))
                        final_reply_saved = True
                        return

                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    
                    _save_checkpoint(step, tool_name, arguments, messages_param=messages)
                    
                    if tool_name != _last_tool_name:
                        _consecutive_same_tool = 0
                        _last_tool_name = tool_name
                    else:
                        _consecutive_same_tool += 1
                        max_streak = self.MAX_SAME_TOOL_STREAK * 2 if tool_name in self.BUILD_TOOLS else self.MAX_SAME_TOOL_STREAK
                        if _consecutive_same_tool >= max_streak:
                            yield self.streamer.emit_final(f"检测到连续调用同一工具 {_consecutive_same_tool} 次，任务已终止以防止无限循环。")
                            final_reply_saved = True
                            return
                    
                except Exception as e:
                    error_msg = translate_error(str(e))
                    yield self.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
                    # 回填工具执行失败结果到 LLM 调用日志
                    get_logger().update_tool_result(
                        session_id=self.session_id,
                        session_name=self.session_name,
                        turn=state.turn,
                        child_turn=state.child_turn - 1,
                        tool_result=f"工具执行失败: {error_msg}"
                    )
                    
                    _consecutive_tool_failures[tool_name] = _consecutive_tool_failures.get(tool_name, 0) + 1
                    _global_consecutive_failures += 1
                    
                    if _consecutive_tool_failures[tool_name] >= self.MAX_CONSECUTIVE_FAILURES:
                        yield self.streamer.emit_final(f"工具 '{tool_name}' 连续失败 {_consecutive_tool_failures[tool_name]} 次，任务已终止。")
                        final_reply_saved = True
                        return
                    
                    if _global_consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        yield self.streamer.emit_final(f"检测到连续失败 {_global_consecutive_failures} 次（跨工具），任务已终止以防止无限循环。")
                        final_reply_saved = True
                        return
                    
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                    if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                        args_preview += "..."
                    _tool_call_history.append({
                        "name": tool_name,
                        "args": args_preview,
                        "status": "failed",
                        "result": f"执行失败: {error_msg[:500]}..." if len(error_msg) > 500 else error_msg
                    })

                    step_msg = self._build_step_summary(tool_name, arguments, "", "failed", error_msg, step)

                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "user", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    _save_checkpoint(step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=messages)
            except Exception as e:
                yield self.streamer.emit_error(f"处理异常: {translate_error(str(e))}")
                _save_checkpoint(step, progress=f"处理异常: {str(e)}")
        
        if cancel_event is not None and cancel_event.is_set():
            get_logger().info("检测到取消信号，跳过最终进度摘要生成",
                              trace_id=self.trace_id, session_id=self.session_id)
            yield self.streamer.emit_final("任务已取消")
            return
        
        if self.completed_steps:
            steps_summary_lines = []
            for idx, step_data in enumerate(self.completed_steps, 1):
                tool_name = step_data.get("tool_name", "")
                arguments = step_data.get("arguments", {})
                result = step_data.get("result", {})
                step_msg = f"{idx}. 使用 {tool_name}"
                if isinstance(arguments, dict):
                    args_str = ", ".join([f"{k}={v}" for k, v in arguments.items()])
                    if args_str:
                        step_msg += f" ({args_str})"
                elif isinstance(arguments, str):
                    step_msg += f" ({arguments})"
                if isinstance(result, dict):
                    success = result.get("success", True)
                    message = result.get("message", "")
                    if message:
                        step_msg += f" - {message}"
                steps_summary_lines.append(step_msg)
            steps_summary = "\n".join(steps_summary_lines)
        else:
            steps_summary = "无"
        
        progress_prompt = f"""请总结当前任务执行进度：
1. 已完成的步骤
2. 未完成的步骤
3. 当前状态

任务：{user_message}
已完成步骤：
{steps_summary}
"""
        try:
            progress_result = ai_concat(
                messages=[{"role": "user", "content": progress_prompt}],
                model_name=default_model,
                provider=default_provider,
                trace_id=self.trace_id,
                model_id=model_id,
                temperature=temperature
            )
            progress_summary = progress_result["message"].get("content", "").strip()
        except Exception as e:
            progress_summary = f"任务执行到第{self.MAX_TURNS}轮终止。已完成{len(self.completed_steps)}个步骤。"
        
        _save_checkpoint(self.MAX_TURNS, progress=progress_summary)
        
        final_message = f"任务已暂停（已执行{len(self.completed_steps)}个步骤）。如需继续，请回复'继续'或'继续任务'"
        yield self.streamer.emit_final(final_message,
            timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    def _build_step_summary(self, tool_name: str, arguments: dict, result_str: str,
                             status: str, error_msg: str = "", step: int = 0) -> str:
        """构建子轮次的工具执行摘要消息。

        只传递事实：做了什么、结果如何、接下来该做什么。不传原始思考链。
        """
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        args_preview = json.dumps(arguments, ensure_ascii=False)
        if len(args_preview) > 300:
            args_preview = args_preview[:297] + "..."

        result_preview = result_str
        if len(result_preview) > self.MAX_RESULT_CHARS:
            result_preview = result_preview[:self.MAX_RESULT_CHARS - 3] + "..."

        if status == "success":
            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
结果  : {result_preview}
状态  : ✅ 成功
上下文: 操作已完成，请根据此结果继续后续任务"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "result_preview": result_preview[:500],
                "status": "success", "step": step,
            }
        else:
            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
失败原因: {error_msg}
状态  : ❌ 失败
上下文: 操作失败，请分析失败原因并尝试替代方案，或向用户请求帮助"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "error": error_msg[:500],
                "status": "failed", "step": step,
            }
        return content

    def _check_and_compress_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return messages

    def _compress_messages(self, messages: List[Dict[str, Any]], target_tokens: int) -> List[Dict[str, Any]]:
        return messages

    def _get_tool(self, tool_name: str) -> Optional[Dict[str, Any]]:
        from core.tool_data_provider import tool_data_provider as _tdp
        
        _tinfo = _tdp.get_tool(tool_name)
        if not _tinfo or not _tinfo.get("function"):
            return None
        
        if self.session_id:
            loaded_domains = get_session_domains(self.session_id)
            always_inject = _tdp.get_always_inject_tools()
            if tool_name in always_inject:
                return {"function": _tinfo["function"], "timeout": _tinfo.get("timeout", 60)}
            
            for domain in loaded_domains:
                domain_tools = _tdp.get_domain_tools(domain)
                if tool_name in domain_tools:
                    return {"function": _tinfo["function"], "timeout": _tinfo.get("timeout", 60)}
            
            return None
        
        return {"function": _tinfo["function"], "timeout": _tinfo.get("timeout", 60)}
    
    def _wait_for_confirmation(self, token: str, confirmation_handler: Callable, timeout: int = 300) -> Dict[str, Any]:
        """带超时的确认等待"""
        result_container = {"result": None}
        
        def wait_worker():
            try:
                result_container["result"] = confirmation_handler(token)
            except Exception as e:
                result_container["result"] = {"confirmed": False, "error": str(e)}
        
        wait_thread = threading.Thread(target=wait_worker, daemon=True)
        wait_thread.start()
        wait_thread.join(timeout=timeout)
        
        if wait_thread.is_alive():
            return {"confirmed": False, "timeout": True, "message": f"确认超时（{timeout}秒），任务已取消"}
        
        return result_container["result"] or {"confirmed": False}