import json
import time
import uuid
import datetime
import re
import os
import threading
import multiprocessing
from multiprocessing import Manager
from typing import Dict, Any, List, Optional, Callable, Generator
from dataclasses import dataclass, field
from utils.dependency_manager import feature_availability

from llm_client import (
    ai_concat, parse_ai_json, call_llm_and_parse,
    get_available_models, _ollama_call_stream, _deepseek_call_stream,
    get_api_key, get_base_url, stream_llm_response,
)
from tools import (
    build_dynamic_system_prompt, build_tools_param,
    match_domains, match_persona, get_session_domains,
    add_domain_to_session, clear_session_domains,
    load_domain_skills, ask_user_clarification,
)
from memory import (
    get_relevant_memory, log_session_turn,
    append_session_message, log_trace, build_context_messages,
    get_full_session_messages,
    estimate_messages_tokens, get_last_turn, update_last_turn,
    get_layered_memory, clear_session_messages,
    add_task_memory,
)
from logger import get_logger
from utils.error_mapper import translate_error

from core.message_builder import build_system_prompt

from .agent_state import (
    AgentTurnState, AgentExecutionState, global_state_manager,
    checkpoint_manager, TaskCheckpoint,
    inject_memory_if_needed, safe_final, load_cross_turn_history,
    RunState, PrepareResult, BreakerState,
    _allocate_turn, save_ai_thinking, reset_breaker_state,
    on_emit_final, save_checkpoint, build_user_content, prepare_run_state,
)
from memory.compressor import check_and_compress_messages, compress_messages
from .streaming import (
    SSEStreamer, StreamingFormatter,
    strip_code_blocks, _truncate_result, _build_context_memory_summary,
    _strip_reasoning_trace, _safe_json_loads, _tool_worker, parse_reasoning_trace,
    tool_call_signature, is_file_not_found, classify_tool_failure,
    validate_tool_dependency, strip_temp_system_messages, inline_compress,
)
from .tool_data_provider import (
    SearchCircuitBreaker, ToolLoopError, ToolLoopPolicy, LOOP_POLICY,
    compute_call_signature, ToolLoopDetector, ToolExecutionPipeline,
    execute_one_step, handle_tool_failure, handle_tool_success, do_final_cleanup,
)

# BM25 上下文路由：纯统计任务切换检测，无需 LLM / 向量模型 / 关键词枚举
from memory.context_router import ContextSwitchDetector
from core.progress_controller import ProgressEvaluator, Verdict

# 功能开关（config/settings.py）
from config.settings import (
    PROGRESS_EVALUATOR_ENABLED, CONTEXT_ROUTER_ENABLED, COMPRESSOR_ENABLED,
    TURN_PERSIST_ENABLED,
)

class ReActEngine:
    MAX_TURNS = 15
    MAX_CONSECUTIVE_PROGRESS = 3
    MAX_CONSECUTIVE_FAILURES = 3   # 同名同参数连续真失败熔断阈值
    MAX_GLOBAL_FAILURES = 4        # 全局连续失败上限
    MAX_CONTEXT_TOKENS = 28000
    SAFETY_MARGIN = 4000
    MAX_RESULT_CHARS = 8000
    MAX_FORCED_ABORT = 8
    WARNING_TURN = 12
    CONFIRMATION_TIMEOUT = 60
    _uninstall_hint_sent = False  # 卸载路径提示去重标记
    WRITE_TOOLS = {"execute_command", "modify_registry", "write_file", "edit_file",
                   "replace_in_file", "move_file", "copy_file", "delete_file",
                   "scan_directory"}
    BUILD_TOOLS = {"ppt_add_slide", "ppt_add_text", "ppt_add_image", "ppt_add_chart",
                   "ppt_create", "excel_write", "excel_pivot", "excel_format",
                   "excel_chart_advanced", "excel_merge", "excel_formula",
                   "excel_convert", "excel_conditional_format", "excel_batch_process",
                   "write_file", "edit_file", "replace_in_file"}
    
    # ── 安全风险分级（P0=最高风险, P3=最低风险） ──
    RISK_LEVELS = {
        # P0：可导致系统损坏或数据永久丢失，需强制确认
        # execute_command 已移除 —— 其内置安全检测机制在工具内部通过 Path B 挑战短语确认，
        # 不走 Path A 可避免 LLM 先文本询问（问题一），同时消除 Path A 注入 confirmed=True
        # 绕过工具内部 challenge_phrase 的问题（问题三）。
        "modify_registry": "P0",
        "delete_file": "P0",
        # P1：可导致数据修改或系统状态变更，需确认
        "write_file": "P1",
        "edit_file": "P1",
        "replace_in_file": "P1",
        "move_file": "P1",
        "gui_click": "P1",
        "gui_type_text": "P1",
        "gui_press_keys": "P1",
        "gui_mouse_click": "P1",
        # P2：可逆操作或影响范围有限，直接执行
        "copy_file": "P2",
        "scan_directory": "P2",
        "web_download": "P2",
        "gui_open_application": "P2",
        "gui_screenshot": "P2",
        "gui_mouse_move": "P2",
        # P3：只读/查询/无害操作，直接执行（默认级别）
    }
    # 未在映射表中的工具默认为 P3
    
    def __init__(self, trace_id: str = "", session_id: str = "", session_name: str = ""):
        self.trace_id = trace_id
        self.session_id = session_id
        self.session_name = session_name
        self.streamer = SSEStreamer()
        self.formatter = StreamingFormatter()
        self.execution_state = AgentExecutionState()
        self.context_detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
        self._turn_state: Optional[AgentTurnState] = None  # 当前轮次状态引用，供 _on_final 回调使用
        self._manager = Manager()
        self._search_breaker = SearchCircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        self.tool_pipeline = ToolExecutionPipeline(
            search_breaker=self._search_breaker,
            risk_levels=self.RISK_LEVELS,
            write_tools=self.WRITE_TOOLS,
            max_result_chars=self.MAX_RESULT_CHARS,
        )
        self._loop_detector = ToolLoopDetector()
        # ── 确认缓存：同一工具+同一关键参数 120s 内确认过一次后不再重复弹确认框 ──
        self._confirmed_cache: dict[str, float] = {}
        self._breaker_state = BreakerState()

    def run(self, user_message: str, model_choice: Dict[str, Any],
            confirmation_handler: Optional[Callable] = None,
            planning_extra: str = "",
            resumed_checkpoint: Optional[TaskCheckpoint] = None,
            cancel_event=None) -> Generator[str, None, None]:

        # 初始化状态
        # RunState 承载可变状态，PrepareResult 返回配置
        rs = RunState()
        result = yield from prepare_run_state(self, rs, user_message, model_choice, resumed_checkpoint)
        if result.flow_control == "return":
            return
        # 解构配置
        state = result.state
        model_id = result.model_id
        base_url = result.base_url
        default_model = result.default_model
        default_provider = result.default_provider
        temperature = result.temperature
        call_seq = result.call_seq
        _tools_param = result.tools_param
        _tl = result.tank_logger
        _sl = result.session_logger
        remaining_steps = result.remaining_steps
        # 取回可变状态
        conversation_messages = rs.conversation_messages
        _thinking_summaries = rs.thinking_summaries
        _tool_call_history = rs.tool_call_history
        layered_cache = rs.layered_cache
        consecutive_empty_turns = rs.consecutive_empty_turns
        final_reply_saved = rs.final_reply_saved
        start_step = rs.start_step

        for step in range(start_step, self.MAX_TURNS):
            # 取消信号检查：在每轮开始前检查，避免取消后继续发起 LLM 调用
            if cancel_event is not None and cancel_event.is_set():
                get_logger().info("检测到取消信号，终止任务循环",
                                  trace_id=self.trace_id, session_id=self.session_id)
                yield from safe_final(self,state, "任务已取消", outcome="cancelled")
                return

            try:
                # 调用 LLM 并解析响应
                # 可变状态通过 RunState 承载，方法内修改后同步回局部变量
                rs = RunState(
                    conversation_messages=conversation_messages,
                    tool_call_history=_tool_call_history,
                    thinking_summaries=_thinking_summaries,
                    layered_cache=layered_cache,
                    consecutive_empty_turns=consecutive_empty_turns,
                    final_reply_saved=final_reply_saved,
                )
                _flow, visible_content, parsed_action, _llm_native_tool_calls = yield from call_llm_and_parse(
                    self, rs, state, user_message, step, cancel_event,
                    call_seq, _tools_param,
                    base_url, default_provider, default_model,
                    model_id, temperature, remaining_steps
                )
                # 同步状态回局部变量
                conversation_messages = rs.conversation_messages
                _tool_call_history = rs.tool_call_history
                _thinking_summaries = rs.thinking_summaries
                layered_cache = rs.layered_cache
                full_content = rs.full_content
                full_reasoning = rs.full_reasoning
                _llm_reasoning_trace = rs.llm_reasoning_trace
                consecutive_empty_turns = rs.consecutive_empty_turns
                final_reply_saved = rs.final_reply_saved

                if _flow == "return":
                    return
                if _flow == "continue":
                    continue
                
                state.parse_fail = 0

                if step == self.WARNING_TURN:
                    if cancel_event is not None and cancel_event.is_set():
                        get_logger().info("检测到取消信号，跳过进度摘要生成",
                                          trace_id=self.trace_id, session_id=self.session_id)
                        yield from safe_final(self,state, "任务已取消", outcome="cancelled")
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
                        save_checkpoint(self,user_message, state, remaining_steps, step, progress=progress_summary, messages_param=conversation_messages)
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
                    
                    if not isinstance(result_data, dict):
                        result_data = {"success": True, "output": result_data}
                    
                    get_logger().debug(f"域加载结果: success={result_data.get('success')}, domain={result_data.get('domain')}, tools={result_data.get('tools')}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                    
                    step_msg = self.tool_pipeline.build_step_summary("load_domain_skills", 
                        {"domain": domain, "reason": need_capability}, 
                        result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result", tool_call_id=f"call_load_domain_skills_{uuid.uuid4().hex[:8]}")
                    
                    yield self.streamer.emit_tool_result("load_domain_skills", 
                        {"domain": domain, "tools": result_data.get("tools", []), 
                         "message": result_data.get("message", ""), "success": result_data.get("success")})
                    
                    continue
                
                # ===== 多 tool_calls 协议重构：assistant 只 append 一次（完整列表），遍历全部 tool_call 并一一回传 =====
                if not _llm_native_tool_calls and not parsed_action.get("tool_name"):
                    final_text = ""
                    if parsed_action and parsed_action.get("final_message"):
                        final_text = parsed_action.get("final_message")
                        get_logger().info(f"提取final_message作为最终回复: {final_text[:200]}",
                                      trace_id=self.trace_id, session_id=self.session_id)
                    else:
                        final_text = strip_code_blocks(visible_content)
                        get_logger().info(f"strip_code_blocks后final_text前200字符: {final_text[:200]}",
                                      trace_id=self.trace_id, session_id=self.session_id)
                        if not final_text.strip():
                            get_logger().warning(f"strip_code_blocks后内容为空，使用原始内容前1000字符",
                                          trace_id=self.trace_id, session_id=self.session_id)
                            final_text = visible_content[:1000]

                    # ===== 引擎产物护栏：禁止 tool_calls=0 却宣称"文件产出成功" =====
                    # 针对 T-35/36/38/40/42/43/49 类"只说不做/幻觉成功"：
                    # 文件产出类任务 + 本会话无任何工具调用 + 文本宣称成功 → 不允许直接结束，
                    # 注入强制提示后继续下一轮，要求调用真实工具生成并校验物理产物。
                    _success_words = ("成功", "完成", "已创建", "已生成", "已保存", "已写入",
                                      "已输出", "已导出", "创建成功", "✅", "✔", "✓")
                    _file_hint_words = ("ppt", "word", "excel", "文档", "文件", "代码", "脚本",
                                        "图片", "报告", "表格", "生成", "创建", "写入",
                                        ".docx", ".xlsx", ".pptx", ".py", ".md")
                    _user_lower = user_message.lower()
                    _claims_success = any(w in final_text for w in _success_words)
                    _looks_file_task = any(w in _user_lower for w in _file_hint_words)
                    if _claims_success and _looks_file_task and not self.completed_steps:
                        get_logger().warning(
                            "引擎护栏触发：无工具调用但宣称文件产出成功，禁止直接结束",
                            trace_id=self.trace_id, session_id=self.session_id)
                        _guard_msg = (
                            "【引擎提示】检测到你尚未调用任何工具就宣称任务已完成。"
                            "对于文件产出类任务，禁止仅凭文本宣称成功。"
                            "请立即调用真实工具（如 write_file / execute_command / 对应 Office 工具）"
                            "执行并生成物理产物，然后校验产物存在后再汇报结果。"
                        )
                        conversation_messages.append({"role": "user", "content": _guard_msg})
                        yield self.streamer.emit_step(_guard_msg)
                        continue

                    get_logger().info(f"最终发送给用户的final_text前500字符: {final_text[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    yield from safe_final(self,state, final_text,
                        timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    final_reply_saved = True
                    return

                # 构建待处理 tool_call 列表：优先原生 tool_calls（可能 N 个），文本协议单 action 包装为单条
                if _llm_native_tool_calls:
                    pending_tool_calls = list(_llm_native_tool_calls)
                else:
                    _ptc_name = parsed_action.get("tool_name", "")
                    _ptc_args = parsed_action.get("arguments", {})
                    pending_tool_calls = [{
                        "id": f"call_{_ptc_name}_{step}_{int(time.time())}",
                        "type": "function",
                        "function": {
                            "name": _ptc_name,
                            "arguments": json.dumps(_ptc_args, ensure_ascii=False) if isinstance(_ptc_args, dict) else str(_ptc_args),
                        },
                    }]

                # assistant 消息只 append 一次，tool_calls 放完整列表（与后续 tool 消息一一对应）
                _assistant_tool_calls = []
                for _ptc in pending_tool_calls:
                    _ptc_fn = _ptc.get("function") or {}
                    # 确保 _ptc 有稳定 id：若 LLM 原生 tool_calls 缺 id，先原地补上，
                    # 保证 execute_one_step 读取 tc.get("id") 与 assistant.tool_calls.id 一致，
                    # 否则两侧各自生成随机 id 会导致 tool 消息无前置 assistant.tool_calls 而被 API 400 拒绝
                    if not _ptc.get("id"):
                        _ptc["id"] = f"call_{_ptc_fn.get('name', 'tool')}_{uuid.uuid4().hex[:8]}"
                    _assistant_tool_calls.append({
                        "id": _ptc.get("id") or f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": _ptc_fn.get("name", ""),
                            "arguments": _ptc_fn.get("arguments", "{}"),
                        },
                    })
                conversation_messages.append({
                    "role": "assistant",
                    "content": full_content or "",
                    "tool_calls": _assistant_tool_calls,
                })

                # 遍历全部 tool_call：每个都必须回传一条 role:"tool" 消息（tool_call_id 一一对应）
                for tc in pending_tool_calls:
                    rs.conversation_messages = conversation_messages
                    rs.tool_call_history = _tool_call_history
                    rs.layered_cache = layered_cache
                    rs.final_reply_saved = final_reply_saved
                    _flow = yield from execute_one_step(
                        self, rs, state, tc, step, full_content, full_reasoning,
                        _tl, _sl, cancel_event, confirmation_handler,
                        user_message, remaining_steps
                    )
                    conversation_messages = rs.conversation_messages
                    _tool_call_history = rs.tool_call_history
                    layered_cache = rs.layered_cache
                    final_reply_saved = rs.final_reply_saved
                    if _flow == "return":
                        return
                    # _flow == "continue" or "proceed": 继续下一个 tc
            except Exception as e:

                try:
                    err_text = f"处理异常: {translate_error(str(e))}"
                except (NameError, Exception):
                    err_text = f"处理异常: {str(e)}"
                yield self.streamer.emit_error(err_text)
                save_checkpoint(self,user_message, state, remaining_steps, step, progress=f"处理异常: {str(e)}")
        
        yield from do_final_cleanup(self, state, user_message, remaining_steps,
                                          cancel_event, default_model, default_provider,
                                          model_id, temperature)
        return
