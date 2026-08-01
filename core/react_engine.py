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
from utils.dependency_manager import feature_availability

from llm_client import (
    ai_concat, parse_ai_json,
    get_available_models, _ollama_call_stream, _deepseek_call_stream,
    get_api_key, get_base_url,
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
    checkpoint_manager, TaskCheckpoint
)
from .streaming import SSEStreamer, StreamingFormatter

# BM25 上下文路由：纯统计任务切换检测，无需 LLM / 向量模型 / 关键词枚举
from memory.context_router import ContextSwitchDetector
from core.progress_controller import ProgressEvaluator, Verdict

# 功能开关（config/settings.py）
from config.settings import (
    PROGRESS_EVALUATOR_ENABLED, CONTEXT_ROUTER_ENABLED, COMPRESSOR_ENABLED,
    TURN_PERSIST_ENABLED,
)


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
class SearchCircuitBreaker:
    """搜索熔断器：连续 N 次失败/低质量后，自动降级跳过搜索。"""
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 60):
        self._consecutive_failures = 0
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._last_failure_time = 0.0
        self._state = "closed"  # closed / open / half_open
        self._lock = threading.Lock()

    def record_result(self, success: bool, relevance_passed: bool):
        with self._lock:
            if success and relevance_passed:
                self._consecutive_failures = 0
                self._state = "closed"
            else:
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                if self._consecutive_failures >= self._threshold:
                    self._state = "open"
                    get_logger().warning(
                        f"搜索熔断器开启: 连续 {self._consecutive_failures} 次失败"
                    )

    def should_skip_search(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return False
            if self._state == "open":
                if time.time() - self._last_failure_time > self._cooldown:
                    self._state = "half_open"
                    return False
                return True
            return False  # half_open

    def get_degradation_message(self) -> str:
        return (
            "搜索服务连续失败，已自动降级。"
            "以下内容基于模型知识生成，未经实时搜索验证。"
            "建议用户自行核实关键数据。"
        )


class ToolLoopError(Exception):
    """工具循环检测异常，触发后终止当前任务。"""
    pass


class ToolLoopPolicy:
    """工具循环检测策略配置。"""

    def __init__(self, exact_repeat_limit: int = 2,
                 same_tool_soft_limit: int = 5,
                 same_tool_hard_limit: int = 10,
                 global_max_turns: int = 15,
                 exempt_tools: set = None):
        self.exact_repeat_limit = exact_repeat_limit          # 同工具+同参数连续重复次数 → 硬终止
        self.same_tool_soft_limit = same_tool_soft_limit      # 同工具累计次数 → 软警告（不终止）
        self.same_tool_hard_limit = same_tool_hard_limit      # 同工具累计次数 → 硬终止
        self.global_max_turns = global_max_turns              # 全局最大步数兜底
        self.exempt_tools = exempt_tools or {
            "web_search", "file_search", "database_query",
            "api_request", "code_execute", "shell_command",
        }


LOOP_POLICY = ToolLoopPolicy()


def compute_call_signature(tool_name: str, arguments: dict) -> str:
    """计算工具调用签名：tool_name + 排序后的 arguments 的 MD5 哈希。
    只有 tool_name 和 arguments 完全相同，签名才相同。
    """
    import hashlib
    normalized = json.dumps(
        {"tool": tool_name, "args": arguments},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.md5(normalized.encode()).hexdigest()


class ToolLoopDetector:
    """三层递进式工具循环检测器。

    Layer 1: 精确签名去重 → 同工具+同参数连续重复 → 硬终止
    Layer 2: 同名工具频率 → 同工具不同参数累计过多 → 软警告/硬终止
    Layer 3: 全局步数兜底 → 由外层 MAX_TURNS 控制
    """

    def __init__(self, policy: ToolLoopPolicy = None):
        self.policy = policy or LOOP_POLICY
        # 完整调用历史: [(signature, tool_name, step_num), ...]
        self._call_history = []
        # 最近一次调用的签名（用于连续重复检测）
        self._last_signature = None
        self._consecutive_exact_repeat = 0

    def check(self, tool_name: str, arguments: dict, step_num: int) -> None:
        """在每次工具调用前检查。检测到真循环时抛出 ToolLoopError。"""
        signature = compute_call_signature(tool_name, arguments)

        # ── Layer 1: 精确签名连续重复 ──
        if signature == self._last_signature:
            self._consecutive_exact_repeat += 1
        else:
            self._consecutive_exact_repeat = 1
        self._last_signature = signature

        if self._consecutive_exact_repeat > self.policy.exact_repeat_limit:
            raise ToolLoopError(
                f"工具 '{tool_name}' 以完全相同的参数连续调用 "
                f"{self._consecutive_exact_repeat} 次，判定为无效循环并终止。"
                f"\n参数: {json.dumps(arguments, ensure_ascii=False)}"
            )

        # ── Layer 2: 同名工具频率（豁免工具跳过） ──
        if tool_name not in self.policy.exempt_tools:
            same_tool_count = sum(
                1 for _, name, _ in self._call_history if name == tool_name
            ) + 1  # +1 是本次

            if same_tool_count > self.policy.same_tool_hard_limit:
                raise ToolLoopError(
                    f"工具 '{tool_name}' 累计调用 {same_tool_count} 次"
                    f"超过硬限制 ({self.policy.same_tool_hard_limit})，任务终止。"
                )

        # ── 记录历史 ──
        self._call_history.append((signature, tool_name, step_num))

    def get_warning_hint(self) -> str | None:
        """返回软警告提示，注入到下一轮 LLM 调用以引导模型收敛。"""
        if not self._call_history:
            return None

        from collections import Counter
        recent = self._call_history[-5:]
        counter = Counter(name for _, name, _ in recent)
        most_common_name, most_common_count = counter.most_common(1)[0]

        if most_common_count >= 4 and most_common_name not in self.policy.exempt_tools:
            return (
                f"[系统提示] 你已连续多次调用 '{most_common_name}'，"
                f"请评估是否已收集到足够信息，考虑进入总结/执行阶段。"
            )
        return None

    def reset(self):
        """重置状态（新任务时调用）。"""
        self._call_history.clear()
        self._last_signature = None
        self._consecutive_exact_repeat = 0



# ── 会话级 turn 游标（进程内缓存 + session DB 持久化） ──
# 跨任务/跨引擎实例存活，避免每任务新建引擎后 turn 归零。
_SESSION_TURN: dict[str, int] = {}
# P0-4/RE3: 进程内兜底计数器——写盘失败时仍可分配唯一 turn，避免同进程内撞 turn。
_TURN_FALLBACK: dict[str, int] = {}


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
    CONFIRMATION_TIMEOUT = 300
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
        self._loop_detector = ToolLoopDetector()
        # ── 确认缓存：同一工具+同一关键参数 120s 内确认过一次后不再重复弹确认框 ──
        self._confirmed_cache: dict[str, float] = {}

    def __del__(self):
        if hasattr(self, '_manager'):
            self._manager.shutdown()
    
    def _allocate_turn(self, session_id: str) -> int:
        """会话内用户问题序号，0-indexed：第1个问题→0，第10个→9。
        持久化为真相源，进程内 _TURN_FALLBACK 兜底：无论写盘成功与否都更新内存，
        保证写盘失败时同进程内不撞 turn。"""
        from memory.session import load_turn_count, save_turn_count

        try:
            cur = load_turn_count(session_id)
        except Exception as e:
            get_logger().error(f"load_turn_count failed: {e}", exc_info=True)
            cur = _TURN_FALLBACK.get(session_id, -1)   # 从内存兜底读
        nxt = cur + 1
        try:
            save_turn_count(session_id, nxt)
        except Exception as e:
            get_logger().error(f"save_turn_count failed: {e}", exc_info=True)
        _TURN_FALLBACK[session_id] = nxt   # 无论写盘成功与否，都更新内存
        return nxt
    
    
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

        # 🆕 TankLogger trace 初始化
        from logger import get_tank_logger
        _tl = get_tank_logger()
        _tl.start_trace(self.session_id, user_message[:80])

        # 🆕 SessionLogger 初始化
        from logger import get_session_logger
        _sl = get_session_logger()

        # 🆕 Per-Session 标准文本日志初始化
        from logger import get_per_session_logger
        _session_std_log = get_per_session_logger(self.session_id)
        _session_std_log.info(f"═══ Session {self.session_id} Run 开始 ═══")
        _session_std_log.info(f"用户输入: {user_message[:200]}")

        yield self.streamer.emit_step("开始分析问题...")
        get_logger().info("Agent启动", trace_id=self.trace_id, session_id=self.session_id, 
                          user_message=user_message[:100])
        
        layered_cache = {}
        _matched_domains = []
        _switch_result = None  # 由正常路径填充，BM25 段复用
        
        try:
            if resumed_checkpoint:
                yield self.streamer.emit_step(f"正在恢复任务，上次执行到第 {resumed_checkpoint.turn} 轮")
                self.completed_steps = resumed_checkpoint.completed_steps
                remaining_steps = resumed_checkpoint.remaining_steps
                
                # 保留 build_context_messages 调用以触发增量压缩，但结果不再用于子轮次拼接
                build_context_messages(self.session_id, is_continuation=True)

                layered_cache = get_layered_memory(self.session_id, user_message)
                
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
                if CONTEXT_ROUTER_ENABLED:
                    _switch_result = self.context_detector.detect(user_message)
                    get_logger().debug(f"[上下文路由] BM25 检测结果: is_switch={_switch_result['is_switch']}, "
                                      f"sim={_switch_result['norm_similarity']}, reason={_switch_result['reason']}, "
                                      f"new_terms={_switch_result.get('new_unique_terms', [])}",
                                      trace_id=self.trace_id, session_id=self.session_id)
                else:
                    _switch_result = {"is_switch": False, "norm_similarity": 1.0, "reason": "feature_disabled"}

                # 话题切换清理已统一到主循环前的完整版（内存状态/域/记忆/消息/start_step 一次性处理）。
                # 此处不再重复清理，避免 clear_session_messages 二次执行清掉中间新写入消息、
                # 以及 completed_steps.clear 等冗余调用。build_context_messages 在旧消息清理前
                # 执行以触发增量压缩（结果不再用于子轮次拼接），符合原注释意图。
                build_context_messages(
                    self.session_id,
                    is_continuation=not _switch_result["is_switch"]
                )

                layered_cache = get_layered_memory(self.session_id, user_message)

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
        # ✅ 会话级 turn 游标：跨任务累计，持久化到 session DB（进程重启不归零）
        #    恢复场景用检查点的 turn 保持一致性
        state.turn = resumed_checkpoint.turn if resumed_checkpoint else self._allocate_turn(self.session_id)
        state.child_turn = 0
        state.final_emitted = False
        self.progress = ProgressEvaluator()
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
                    reasoning_trace="",
                )
                # TankLogger / SessionLogger 结束 Turn
                _tl.log_turn_end(
                    ai_reply=(text or "")[:2000],
                    reasoning_trace="",
                )
                _tl.end_trace(
                    success=True,
                    summary=(text or "")[:200],
                )
                _sl.end_turn(
                    ai_reply=(text or "")[:2000],
                    status="completed",
                    error="",
                    reasoning_trace="",
                    route_level="",
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
                checkpoint_manager.save_checkpoint(checkpoint=checkpoint)
                get_logger().info(f"检查点已保存: {checkpoint.checkpoint_id}", 
                                  trace_id=self.trace_id, session_id=self.session_id)
            except Exception as e:
                get_logger().error(f"保存检查点失败: {e}", trace_id=self.trace_id, session_id=self.session_id)
        
        def llm_stream(messages: List[Dict[str, Any]], system_prompt: str = "", 
                       user_input: str = "") -> Generator[Any, None, None]:
            idx = call_seq[0]
            call_seq[0] += 1
            t0 = time.perf_counter()
            
            # 复用 run() 入口缓存的 tool_schemas，避免每轮重复构建（问题2）
            tools_param = _tools_param
            
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
                    try:
                        for chunk in stream_gen:
                            if cancel_event and cancel_event.is_set():
                                yield {"type": "cancelled", "message": "任务已取消"}
                                return

                            if time.time() - last_heartbeat > 10:
                                yield ": keepalive\n\n"
                                last_heartbeat = time.time()

                            content = chunk.get("content", "")
                            reasoning = chunk.get("reasoning", "")

                            # 先检查 "result" 类型，避免"result"事件以 content 身份被二次累加导致内容重复
                            if chunk.get("type") == "result":
                                full_content = chunk.get("content", full_content)
                                full_reasoning = chunk.get("reasoning_content", full_reasoning)

                                # ✅ 提前返回前记录 LLM 调用日志，防止跳过 for 循环后的日志写入
                                elapsed = int((time.perf_counter() - t0) * 1000)
                                reasoning_trace = parse_reasoning_trace(full_content)
                                reasoning_trace_str = json.dumps(reasoning_trace, ensure_ascii=False) if reasoning_trace else ""

                                actual_user_input = user_input
                                for m in reversed(messages):
                                    if m.get("role") == "user":
                                        actual_user_input = m.get("content", "")
                                        break

                                try:
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
                                        route_level="",
                                        context_memory=_build_context_memory_summary(layered_cache),
                                    )
                                except Exception as _llm_log_err2:
                                    get_logger().warning(f"log_llm_call(result分支) 异常（已忽略）: {_llm_log_err2}",
                                                         trace_id=self.trace_id, session_id=self.session_id)
                                state.child_turn += 1

                                yield {
                                    "type": "result",
                                    "content": full_content,
                                    "reasoning": full_reasoning,
                                    "tool_calls": chunk.get("tool_calls", []),  # ✅ 透传原生 tool_calls
                                }
                                return

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
                    
                    finally:
                        stream_gen.close()
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
                    try:
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
                            route_level="",
                            context_memory=_build_context_memory_summary(layered_cache),
                        )
                    except Exception as _llm_log_err:
                        get_logger().warning(f"log_llm_call 异常（已忽略，不影响主流程）: {_llm_log_err}",
                                             trace_id=self.trace_id, session_id=self.session_id)
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
                        # 记录所有重试耗尽后的失败 LLM 调用
                        elapsed = int((time.perf_counter() - t0) * 1000)
                        try:
                            get_logger().log_llm_call(
                                trace_id=self.trace_id, session_id=self.session_id,
                                turn=state.turn, child_turn=state.child_turn,
                                user_task=state.user_task,
                                response=f"[LLM调用失败] {translate_error(str(e))}",
                                model_name=default_model, provider=default_provider,
                                elapsed_ms=elapsed, session_name=self.session_name,
                                system_prompt=system_prompt,
                                user_input=user_input,
                                messages=messages,
                                user_task_time=state.user_task_time,
                            )
                        except Exception:
                            pass
                        yield {"type": "error", "message": translate_error(str(e))}
                        return
        
        def save_ai_thinking(summary: str):
            """保存 AI 思考过程摘要（msg_type='think'，不参与上下文构建）"""
            if self.session_id and summary:
                try:
                    append_session_message(self.session_id, "assistant", summary,
                                           trace_id=self.trace_id, msg_type="think",
                                           skip_compress=True)
                except Exception as e:
                    get_logger().warning(f"append think msg failed: {e}", trace_id=self.trace_id, session_id=self.session_id)

        # save_ai_reply 已移除 —— AI 消息持久化收敛为前端 saveSessionToBackend 单一入口
        _cache = {}
        _last_tool_call_signature = ""      # 上次工具调用签名（失败熔断用）
        _consecutive_fail_count = 0          # 同名同参数连续真失败计数
        _global_consecutive_failures = 0
        _consecutive_empty_search = 0         # 连续空搜索结果计数（用于自适应降级）
        _failure_breaker_msg_injected = False
        _breaker_hits = 0
        _last_error_category = ""
        _last_exception_type = ""
        _thinking_summaries = []
        _tool_call_history = []

        def _tool_call_signature(tool_name: str, tool_args: dict) -> str:
            """生成工具调用的唯一签名（名称 + 参数）"""
            import hashlib
            payload = json.dumps(
                {"name": tool_name, "args": tool_args},
                sort_keys=True, ensure_ascii=False
            )
            return hashlib.md5(payload.encode()).hexdigest()

        def _classify_tool_failure(tool_result: dict) -> str:
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
            if _is_file_not_found(error) or _is_file_not_found(message):
                return "file_not_found"
            return "unknown"

        def _is_file_not_found(text: str) -> bool:
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
        
        def reset_breaker_state():
            nonlocal _breaker_hits, _last_error_category, _last_exception_type, _last_tool_call_signature, _consecutive_fail_count, _failure_breaker_msg_injected, _global_consecutive_failures, _consecutive_empty_search
            _breaker_hits = 0
            _last_error_category = ""
            _last_exception_type = ""
            _last_tool_call_signature = ""
            _consecutive_fail_count = 0
            _global_consecutive_failures = 0
            _consecutive_empty_search = 0
            _failure_breaker_msg_injected = False
            self._loop_detector.reset()

        final_reply_saved = False
        consecutive_empty_turns = 0
        state.child_turn = 0
        # 每次 run 重置循环检测器，保证多轮对话间调用历史不累积（原为 run 内局部变量，天然隔离）
        self._loop_detector.reset()

        def _build_user_content():
            """构建用户消息各部分，统一正常路径和压缩路径"""
            # 各段预算占比（总和 = 1.0）
            _BUDGET = {
                "user_message":       0.50,   # ① 用户任务（核心）
                "plan_and_tools":     0.40,   # ② 规划 + 工具 + 思考轨迹
            }
            
            MAX_USER_CONTENT_CHARS = 12000
            segments = {}  # key → raw text with header

            # ① 用户任务（每次请求）
            segments["user_message"] = f"【任务】\n{user_message}"

            # ⑥ AI思考摘要（近两轮）
            plan_tool_parts = []
            if _thinking_summaries:
                trace_lines = []
                for i, rt in enumerate(_thinking_summaries[-2:]):
                    items = [f"#{i+1}"]
                    if rt.get("current_intent"):
                        items.append(rt["current_intent"])
                    if rt.get("decision_rationale"):
                        items.append(f"理由:{rt['decision_rationale'][:60]}")
                    trace_lines.append(" | ".join(items))
                plan_tool_parts.append("【思考轨迹】\n" + "\n".join(trace_lines))

            if plan_tool_parts:
                segments["plan_and_tools"] = "\n\n".join(plan_tool_parts)

            # ─── 按预算截断 ───
            parts = []
            for key, budget_ratio in _BUDGET.items():
                raw = segments.get(key, "")
                if not raw:
                    continue
                budget = int(MAX_USER_CONTENT_CHARS * budget_ratio)
                if len(raw) <= budget:
                    parts.append(raw)
                else:
                    parts.append(raw[:budget] + "\n[…该段已截断]")

            return "\n\n".join(parts)

        # ── BM25 上下文路由：检测任务切换 ──
        # detect() 已在正常路径中提前执行（确保 build_context_messages 拿到一致判断），
        # 此处复用结果；checkpoint 恢复路径走 else 分支不填充 _switch_result，跳过此段
        switch_result = _switch_result
        if switch_result is None:
            switch_result = {"is_switch": False, "norm_similarity": 1.0, "reason": "checkpoint_resume"}
        
        if switch_result["is_switch"]:
            get_logger().info(
                f"[话题切换] 旧任务 → 新任务: '{user_message[:50]}'，"
                f"清空所有旧上下文"
            )

            # 1. 清空引擎内存状态
            reset_breaker_state()
            self.completed_steps.clear()
            _thinking_summaries.clear()
            _tool_call_history.clear()
            _last_tool_call_signature = ""
            _consecutive_fail_count = 0
            _global_consecutive_failures = 0

            # 2. 清空会话域（关键！移除旧域工具）
            clear_session_domains(self.session_id)
            for _d in _matched_domains:
                add_domain_to_session(self.session_id, _d)

            # 3. 重新获取记忆并清理所有层级
            layered_cache = get_layered_memory(self.session_id, user_message)

            # 3a. 清理 recent_structured（移除 child_turns 详情）
            if layered_cache.get("recent_structured"):
                try:
                    rc = json.loads(layered_cache["recent_structured"])
                    for turn in rc:
                        turn["child_turns"] = []
                        # 只保留一句话摘要
                        turn["context_summary"] = f"[已完成] {turn.get('user_task', '')[:60]}"
                    layered_cache["recent_structured"] = json.dumps(rc, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    layered_cache["recent_structured"] = ""

            # 3b. 清理 long_term（移除任务相关记忆，只保留用户偏好）
            if layered_cache.get("long_term"):
                layered_cache["long_term"] = ""

            # 3c. 清理 older_summary
            if layered_cache.get("older_summary"):
                layered_cache["older_summary"] = ""

            # 4. 用新域重建 system prompt
            full_system = build_dynamic_system_prompt(
                domains=_matched_domains,
                persona_mode=persona_mode
            )
            self._full_system = full_system

            # 5. 清空会话历史消息（可选：保留最近 1 条系统消息）
            clear_session_messages(self.session_id, keep_system=True)

            start_step = 0

            get_logger().info(f"[话题切换] 完成。新域: {_matched_domains}")
            yield self.streamer.emit_step(f"检测到新任务，已切换上下文")
        
        # 将当前任务记入历史（不影响后续检测，仅当 detect() 内部管理）
        self.context_detector.commit_task(user_message)

        # ── Turn 级日志：主循环外创建 Turn ──
        _tl.log_turn_start(
            turn_index=state.turn,
            user_input=user_message,
            system_prompt=self._full_system,
            route_level="",
        )
        _sl.start_turn(
            user_input=user_message,
            user_intent="",
            route_level="",
            system_prompt=self._full_system,
            system_prompt_version="",
            reasoning_trace="",
            metadata={},
        )

        # 初始化 Multi-Turn 消息数组（优先从检查点恢复完整对话历史）
        # P0 修复：显式调用 build_system_prompt 组装 system prompt（角色/模式 + 工具列表 + 安全规则），
        # 以 build_dynamic_system_prompt 动态结果（core.md/safety.md/output.md/技能注入）作为安全规则基础，
        # 保证任何路径下 conversation_messages[0] 都是 system。
        _loaded_domains = get_session_domains(self.session_id) if self.session_id else []
        _tools_param = build_tools_param(loaded_domains=list(_loaded_domains)) or build_tools_param(loaded_domains=_matched_domains)
        _system_content = build_system_prompt(
            mode=persona_mode or "default",
            tools=_tools_param,
            safety_rules=self._full_system,
        )
        if resumed_checkpoint and resumed_checkpoint.messages:
            conversation_messages = [dict(m) for m in resumed_checkpoint.messages]
            # 更新 system prompt（可能因会话上下文变化）；若恢复历史首条不是 system 则强制前置
            if conversation_messages and conversation_messages[0].get("role") == "system":
                conversation_messages[0]["content"] = _system_content
            else:
                conversation_messages.insert(0, {"role": "system", "content": _system_content})
        else:
            conversation_messages: List[Dict[str, Any]] = [
                {"role": "system", "content": _system_content}
            ]
            # ── 注入跨 Turn 历史对话（修复多轮失忆：此前仅传当前 user 输入）──
            # 仅注入用户可见的 user/assistant 轮次，过滤内部 tool/think/system 消息；
            # 首轮无历史时为空，行为与原来一致；长度/条数截断由 _load_cross_turn_history 负责。
            try:
                _cross_turn_history = self._load_cross_turn_history(user_message)
                if _cross_turn_history:
                    conversation_messages.extend(_cross_turn_history)
                    get_logger().info(
                        f"[跨Turn历史] 注入 {len(_cross_turn_history)} 条历史对话到上下文",
                        trace_id=self.trace_id, session_id=self.session_id,
                    )
            except Exception as _h_err:
                get_logger().warning(f"[跨Turn历史] 注入失败: {_h_err}",
                                     trace_id=self.trace_id, session_id=self.session_id)
            # 构建初始用户消息（含记忆注入）
            initial_user_content = _build_user_content()
            conversation_messages.append({
                "role": "user",
                "content": initial_user_content
        })

        def _strip_temp_system_messages(messages):
            """移除历史临时 system 消息（[格式纠错]/[进展控制器]），防止动态反馈跨轮累积；
            按内容标记精准识别，不影响固定 system prompt / 记忆注入 / 跨Turn历史 / 压缩摘要。"""
            return [m for m in messages if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and ("[格式纠错]" in m["content"] or "[进展控制器]" in m["content"])
            )]

        for step in range(start_step, self.MAX_TURNS):
            # 取消信号检查：在每轮开始前检查，避免取消后继续发起 LLM 调用
            if cancel_event is not None and cancel_event.is_set():
                get_logger().info("检测到取消信号，终止任务循环",
                                  trace_id=self.trace_id, session_id=self.session_id)
                yield from self._safe_final(state, "任务已取消", outcome="cancelled")
                return

            full_content = ""
            full_reasoning = ""
            _llm_reasoning_trace = None
            
            try:
                get_logger().debug(f"===== 第{step}轮 Multi-Turn LLM 请求 =====",
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"用户任务: {user_message[:200]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"长期记忆: {'有' if layered_cache.get('long_term') else '无'}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"近三轮结构化: {'有' if layered_cache.get('recent_structured') else '无'}",
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
                get_logger().debug(f"Multi-Turn 消息数: {len(conversation_messages)}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                get_logger().debug(f"===============================",
                                  trace_id=self.trace_id, session_id=self.session_id)

                # LLM 调用前检查取消信号
                if cancel_event is not None and cancel_event.is_set():
                    get_logger().info("检测到取消信号，终止任务（LLM 调用前）",
                                      trace_id=self.trace_id, session_id=self.session_id)
                    yield from self._safe_final(state, "任务已取消", outcome="cancelled")
                    return

                yield self.streamer.emit_step("正在调用模型思考...")

                _reasoning_streamed = False
                last_heartbeat = time.time()

                # ✅ 先注入记忆（在 token 估算之前，确保压缩能准确计算注入后的上下文用量）
                if layered_cache:
                    conversation_messages = self._inject_memory_if_needed(conversation_messages, layered_cache)

                # ── Token 估算与上下文窗口管理（使用 conversation_messages） ──
                try:
                    from memory.compressor import estimate_messages_tokens
                    UNIFIED_THRESHOLD = (self.MAX_CONTEXT_TOKENS - self.SAFETY_MARGIN
                                         if COMPRESSOR_ENABLED else float("inf"))
                    total_tokens = estimate_messages_tokens(conversation_messages)
                    layer_flags = []

                    # ── Layer 1: 轻量压缩（压缩思考摘要，~ms 级） ──
                    if total_tokens > UNIFIED_THRESHOLD:
                        get_logger().warning(
                            f"Token 预算超限 ({total_tokens}/{UNIFIED_THRESHOLD})，Layer1: 压缩思考摘要",
                            trace_id=self.trace_id, session_id=self.session_id
                        )
                        if _thinking_summaries:
                            def _inline_compress(summaries, max_chars=800):
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
                            _thinking_summaries = _inline_compress(_thinking_summaries)
                        total_tokens = estimate_messages_tokens(conversation_messages)
                        layer_flags.append("L1")

                    # ── Layer 2: 中量压缩（压缩工具历史，~ms 级） ──
                    if total_tokens > UNIFIED_THRESHOLD:
                        get_logger().warning(
                            f"Token 预算仍超限 ({total_tokens}/{UNIFIED_THRESHOLD})，Layer2: 压缩工具历史",
                            trace_id=self.trace_id, session_id=self.session_id
                        )
                        if len(_tool_call_history) > 5:
                            _tool_call_history[:] = _tool_call_history[-5:]
                        for tc in _tool_call_history:
                            if tc.get("result") and isinstance(tc["result"], str) and len(tc["result"]) > 200:
                                tc["result"] = tc["result"][:200] + "..."
                        total_tokens = estimate_messages_tokens(conversation_messages)
                        layer_flags.append("L2")

                    # ── Layer 3: 重量压缩（压缩 conversation_messages 历史轮次，~10ms 级） ──
                    if total_tokens > UNIFIED_THRESHOLD:
                        get_logger().warning(
                            f"Token 预算仍超限 ({total_tokens}/{UNIFIED_THRESHOLD})，Layer3: 压缩消息历史",
                            trace_id=self.trace_id, session_id=self.session_id
                        )
                        conversation_messages = self._compress_messages(conversation_messages, target_tokens=UNIFIED_THRESHOLD)
                        layer_flags.append("L3")

                    if layer_flags:
                        get_logger().info(
                            f"Token 预算压缩完成，触发层级: {'→'.join(layer_flags)}，最终 token: {total_tokens}",
                            trace_id=self.trace_id, session_id=self.session_id
                        )
                except ImportError:
                    pass
                except Exception as _tok_e:
                    get_logger().warning(f"token 估算失败（不影响主流程）: {_tok_e}",
                                        trace_id=self.trace_id, session_id=self.session_id)

                for chunk in llm_stream(conversation_messages):
                    if isinstance(chunk, str) and chunk.startswith(":"):
                        yield chunk
                        continue
                    
                    if time.time() - last_heartbeat > 10:
                        yield ": keepalive\n\n"
                        last_heartbeat = time.time()
                    
                    if isinstance(chunk, dict):
                        if chunk.get("type") == "error":
                            yield self.streamer.emit_error(chunk["message"])
                            _save_checkpoint(step, progress=f"第{step}次LLM调用失败: {chunk['message']}", messages_param=conversation_messages)
                            final_reply_saved = True
                            return
                        elif chunk.get("type") == "cancelled":
                            yield from self._safe_final(state, chunk["message"], outcome="cancelled")
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
                        _llm_native_tool_calls = chunk.get("tool_calls", [])
                        visible_content, _ = _strip_reasoning_trace(full_content)
                
                if not full_content.strip():
                    consecutive_empty_turns += 1
                    get_logger().warning(f"LLM返回空内容，第{consecutive_empty_turns}次", 
                                          trace_id=self.trace_id, session_id=self.session_id)
                    if consecutive_empty_turns >= 3:
                        yield from self._safe_final(state, "模型连续多次返回空内容，任务已暂停。请稍后重试或换一种方式描述您的需求。",
                            timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        final_reply_saved = True
                        return
                    continue
                consecutive_empty_turns = 0
                
                reasoning_trace = _llm_reasoning_trace if _llm_reasoning_trace else parse_reasoning_trace(full_content)
                if reasoning_trace is None:
                    # fallback: 从 full_content 取前 200 字符作为 intent
                    intro = full_content.strip()[:200].replace("\n", " ")
                    reasoning_trace = {
                        "current_intent": intro[:200] + ("..." if len(intro) > 200 else ""),
                        "decision_rationale": "fallback: LLM未输出结构化思考轨迹",
                        "active_hypothesis": ""
                    }
                if reasoning_trace:
                    if _thinking_summaries and _thinking_summaries[-1].get("current_intent") == reasoning_trace.get("current_intent"):
                        _thinking_summaries[-1] = {**_thinking_summaries[-1], **reasoning_trace}
                    else:
                        _thinking_summaries.append(reasoning_trace)
                    get_logger().debug(f"思考摘要已追加: intent={reasoning_trace.get('current_intent','')}, rationale={reasoning_trace.get('decision_rationale','')[:50]}", 
                                      trace_id=self.trace_id, session_id=self.session_id)
                
                if not _reasoning_streamed:
                    extracted_full_reasoning = full_reasoning
                    if not extracted_full_reasoning and full_content:
                        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', full_content)
                        if reasoning_match:
                            extracted_full_reasoning = reasoning_match.group(1).strip()
                        else:
                            trace_match = re.search(r'<reasoning_trace>\s*(\{[\s\S]*?\})\s*</reasoning_trace>', full_content)
                            if trace_match:
                                extracted_full_reasoning = trace_match.group(1).strip()
                    if extracted_full_reasoning.strip():
                        yield self.streamer.emit_reasoning(extracted_full_reasoning)
                
                parsed_action = parse_ai_json(full_content)
                
                get_logger().info(f"parse_ai_json结果: parsed_action={parsed_action}",
                              trace_id=self.trace_id, session_id=self.session_id)
                
                if parsed_action is None:
                    get_logger().warning(f"parse_ai_json返回None，full_content前500字符: {full_content[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)

                    # ── 智能兜底：检测是否为"纯文本最终回复" ──
                    # 场景：LLM在工具执行成功后直接输出自然语言回复（而非 ```action {"final_message": ...} ```），
                    # 这是模型的合理行为（尤其是 deepseek-chat 等非严格指令跟随模型）。
                    # 检测条件：内容是纯自然语言（不含JSON/代码块/工具调用特征）
                    # 不再要求 completed_steps 非空 — 首轮也可能直接返回纯文本回复
                    content_text = full_content.strip()
                    looks_like_plain_text = (
                        len(content_text) > 3
                        and not re.search(r'```(?:json|action)\s*\{', content_text)
                        and not re.search(r'\{\s*"tool_name"', content_text)
                        and not re.search(r'\{\s*"final_message"', content_text)
                        and not re.search(r'\{\s*"tool_call"', content_text)
                        and not re.search(r'\{\s*"progress"', content_text)
                        and not re.search(r'\{\s*"next_step"', content_text)
                        and (
                            re.search(r'[\u4e00-\u9fff]', content_text)
                            or re.match(r'^[A-Za-z]', content_text)
                            or re.match(r'^[✅✔✓🎯📋🔧📄💻🎨⚡🔥🌟✨]', content_text)
                        )
                    )
                    if looks_like_plain_text:
                        get_logger().info(f"检测到纯文本回复，视为final_message，跳过兜底重试",
                                          trace_id=self.trace_id, session_id=self.session_id)
                        parsed_action = {
                            "final_message": content_text,
                            "_auto_detected": True,
                        }
                        state.parse_fail = 0
                        # 跳过重试逻辑，直接走 final_message 处理分支
                        pass
                    else:
                        state.parse_fail = getattr(state, "parse_fail", 0) + 1

                    if parsed_action is None and state.parse_fail >= 2:
                        fallback = ("[系统兜底] 模型连续两次输出无法解析为合法动作，以下为原始输出，"
                                    "任务可能未完成，请人工核对：\n" + strip_code_blocks(visible_content))
                        yield from self._safe_final(state, fallback)
                        final_reply_saved = True
                        return
                    if parsed_action is None:
                        # 注入纠错提示让模型重试
                        format_fix_prompt = """[格式纠错] 上一次输出无法解析为合法动作。请严格按以下格式之一输出：
【调用工具时】：
```action
{"tool_name": "工具名称", "arguments": {"参数1": "值1", "参数2": "值2"}}
```
【任务完成时】：
```action
{"final_message": "给用户的最终回复内容"}
```
注意：
1. 必须输出 ```action 代码块包裹的 JSON
2. tool_name 必须是已注册的工具名
3. 不要输出自然语言，必须输出结构化 JSON
"""
                        conversation_messages = _strip_temp_system_messages(conversation_messages)
                        conversation_messages.append({"role":"system","content":format_fix_prompt})
                        continue
                
                state.parse_fail = 0

                if step == self.WARNING_TURN:
                    if cancel_event is not None and cancel_event.is_set():
                        get_logger().info("检测到取消信号，跳过进度摘要生成",
                                          trace_id=self.trace_id, session_id=self.session_id)
                        yield from self._safe_final(state, "任务已取消", outcome="cancelled")
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
                        _save_checkpoint(step, progress=progress_summary, messages_param=conversation_messages)
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
                    
                    step_msg = self._build_step_summary("load_domain_skills", 
                        {"domain": domain, "reason": need_capability}, 
                        result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                    append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    
                    yield self.streamer.emit_tool_result("load_domain_skills", 
                        {"domain": domain, "tools": result_data.get("tools", []), 
                         "message": result_data.get("message", ""), "success": result_data.get("success")})
                    
                    continue
                
                # ===== 多 tool_calls 协议重构：assistant 只 append 一次（完整列表），遍历全部 tool_call 并一一回传 =====
                if not _llm_native_tool_calls and not parsed_action.get("tool_name"):
                    get_logger().info(f"LLM未调用工具，直接回复: {full_content[:200]}",
                                  trace_id=self.trace_id, session_id=self.session_id)

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

                    get_logger().info(f"最终发送给用户的final_text前500字符: {final_text[:500]}",
                                  trace_id=self.trace_id, session_id=self.session_id)
                    yield from self._safe_final(state, final_text,
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
                    _assistant_tool_calls.append({
                        "id": _ptc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
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
                    tool_call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    func = tc.get("function") or {}
                    tool_name = func.get("name", "")
                    _raw_args = func.get("arguments", "{}")
                    try:
                        arguments = json.loads(_raw_args) if isinstance(_raw_args, str) else _raw_args
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {"raw": arguments}

                    if not tool_name:
                        conversation_messages.append({
                            "role": "tool", "tool_call_id": tool_call_id,
                            "content": "[系统] 工具调用格式异常，已忽略。",
                        })
                        continue

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
                        append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        conversation_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": json.dumps({"success": False, "message": f"依赖验证失败: {dep_error}"}, ensure_ascii=False)
                        })
                        continue
                
                    yield self.streamer.emit_tool_call(tool_name, arguments)

                    # ===== 三层递进式循环检测（ToolLoopDetector） =====
                    # Layer 1: 精确签名去重（同工具+同参数 → 硬终止）
                    # Layer 2: 同名工具频率（同工具不同参数 → 硬终止，搜索类豁免）
                    # Layer 3: 全局步数上限（MAX_TURNS 兜底，在主循环 for step 中）
                    try:
                        self._loop_detector.check(tool_name, arguments, step)
                    except ToolLoopError as _loop_err:
                        get_logger().warning(f"[熔断] {_loop_err}", trace_id=self.trace_id, session_id=self.session_id)
                        yield from self._safe_final(state,
                            f"⚠️ {_loop_err}\n\n"
                            f"可能原因：工具返回结果不满足需求，或任务超出当前能力范围。\n"
                            f"建议：请尝试更具体的描述，或确认是否需要安装额外工具。"
                        )
                        final_reply_saved = True
                        return

                    # ── SubTurn 开始 ──
                    _sl.start_sub_turn(
                        tool_name=tool_name,
                        tool_params=arguments,
                        reasoning=full_reasoning,
                        context_memory=_build_context_memory_summary(layered_cache),
                        context_memory_note="",
                        metadata={},
                    )
                
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
                            append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps({"success": False, "message": f"工具 '{tool_name}' 不存在"}, ensure_ascii=False)
                            })
                            continue
                    
                        # 工具执行前检查取消信号
                        if cancel_event is not None and cancel_event.is_set():
                            get_logger().info(f"检测到取消信号，终止工具执行: {tool_name}",
                                              trace_id=self.trace_id, session_id=self.session_id)
                            yield from self._safe_final(state, "任务已取消", outcome="cancelled")
                            return

                        # ── 安全分级检查（P0/P1 需确认） ──
                        risk_msg, challenge_phrase, risk_level = self._check_tool_risk_level(tool_name, arguments)
                        if risk_msg:
                            yield self.streamer.emit_reasoning(risk_msg)
                            # ── 确认缓存：同一工具+同一关键参数 120s 内已确认过 → 直接放行，不再弹确认框 ──
                            cache_key = f"{tool_name}:{json.dumps({k: v for k, v in sorted(arguments.items()) if k in ('file_path', 'path', 'command')}, sort_keys=True, ensure_ascii=False, default=str)}"
                            _cached_at = self._confirmed_cache.get(cache_key)
                            if _cached_at is not None and (time.time() - _cached_at) < 120:
                                get_logger().info(f"confirm cache hit: {tool_name}, skip",
                                                  trace_id=self.trace_id, session_id=self.session_id)
                            elif confirmation_handler:
                                token = f"risk_confirm_{tool_name}_{int(__import__('time').time())}"
                                yield self.streamer.emit_confirm(
                                    token,
                                    title=f"安全风险确认：{tool_name}（{risk_level}）",
                                    command=risk_msg,
                                    tool_name=tool_name,
                                    details=arguments,
                                    challenge_phrase=challenge_phrase,
                                )
                                confirm_result = self._wait_for_confirmation(token, confirmation_handler)
                                confirm_result = confirm_result or {}

                                # ✅ 校验（对齐路径 B）
                                if challenge_phrase:
                                    ok = bool(confirm_result.get("confirmed")) and \
                                         (confirm_result.get("challenge_response", "").strip() == challenge_phrase)
                                else:
                                    ok = bool(confirm_result.get("confirmed"))
                                if not ok:
                                    yield self.streamer.emit_step(f"用户拒绝/挑战校验失败 '{tool_name}'，已跳过", "warning")
                                    args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                                    if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                                        args_preview += "..."
                                    _tool_call_history.append({
                                        "name": tool_name, "args": args_preview,
                                        "status": "rejected", "result": "用户拒绝/挑战校验失败"
                                    })
                                    step_msg = self._build_step_summary(tool_name, arguments, "", "failed",
                                        f"用户拒绝/挑战校验失败 '{tool_name}'", step)
                                    append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                                    append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                                    conversation_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call_id,
                                        "name": tool_name,
                                        "content": json.dumps({"success": False, "message": "用户拒绝/挑战校验失败"}, ensure_ascii=False)
                                    })
                                    continue

                                # ✅ 确认通过：按是否带挑战短语注入对应确认参数
                                # 注：已删除向 tools.system_tools._pending_confirmations 的预注册——
                                # system_tools 不存在该属性（AttributeError 被吞），且 execute_command
                                # 已从 Path A 移除，该预注册从不生效，属死代码。
                                if not challenge_phrase:
                                    arguments["confirmed"] = True

                                # ✅ 确认通过：写入确认缓存（同工具+同关键参数 120s 内不再重复确认）
                                self._confirmed_cache[cache_key] = time.time()

                        # 注入 session_id 到 load_domain_skills 等需要会话上下文的工具
                        if tool_name == "load_domain_skills":
                            arguments["session_id"] = self.session_id
                    
                        # 需要修改主进程状态的工具：直接在主进程执行（不走子进程）
                        _IN_PROCESS_TOOLS = {"load_domain_skills", "ask_user_clarification", "web_download"}
                        if tool_name in _IN_PROCESS_TOOLS:
                            try:
                                import time as _vt
                                _vt0 = _vt.time()
                                raw_result = tool_func(**arguments)
                                _latency = (_vt.time() - _vt0) * 1000
                                validated = self._execute_tool_with_validation(
                                    tool_name, arguments, raw_result, self.trace_id, _latency
                                )
                                if isinstance(validated, dict) and validated.get("status") in ("low_quality", "empty"):
                                    result_data = validated
                                else:
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
                            append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                            # 记录 tool 结果到 conversation_messages
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps(result_data, ensure_ascii=False)
                            })
                            _save_checkpoint(step, tool_name, arguments, progress="完成" if result_data.get("success") else result_data.get("message", ""), messages_param=conversation_messages)
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

                        result_container = self._manager.dict({"result": None, "error": None})

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
                            _timeout_sig = _tool_call_signature(tool_name, arguments)
                            if _timeout_sig == _last_tool_call_signature:
                                _consecutive_fail_count += 1
                            else:
                                _consecutive_fail_count = 1
                                _last_tool_call_signature = _timeout_sig
                            _global_consecutive_failures += 1
                            if _consecutive_fail_count >= self.MAX_CONSECUTIVE_FAILURES:
                                yield from self._safe_final(state, f"工具 {tool_name} 以相同参数连续失败 {self.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                                final_reply_saved = True
                                return
                            if _global_consecutive_failures >= self.MAX_GLOBAL_FAILURES:
                                yield from self._safe_final(state, f"检测到连续失败 {_global_consecutive_failures} 次（跨工具），任务已终止。")
                                final_reply_saved = True
                                return
                            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                                args_preview += "..."
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps({"success": False, "message": f"执行超时（{timeout}秒）"}, ensure_ascii=False)
                            })
                            _tool_call_history.append({
                                "name": tool_name,
                                "args": args_preview,
                                "status": "failed",
                                "result": f"执行超时（{timeout}秒）"
                            })
                            step_msg = self._build_step_summary(tool_name, arguments, "", "failed",
                                f"工具 '{tool_name}' 执行超时（{timeout}秒）", step)
                            append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                            append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                            continue

                        if result_container["error"]:
                            error_msg = translate_error(str(result_container["error"]))
                            yield self.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
                            _error_sig = _tool_call_signature(tool_name, arguments)
                            if _error_sig == _last_tool_call_signature:
                                _consecutive_fail_count += 1
                            else:
                                _consecutive_fail_count = 1
                                _last_tool_call_signature = _error_sig
                            _global_consecutive_failures += 1
                            if _consecutive_fail_count >= self.MAX_CONSECUTIVE_FAILURES:
                                yield from self._safe_final(state, f"工具 {tool_name} 以相同参数连续失败 {self.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                                final_reply_saved = True
                                return
                            if _global_consecutive_failures >= self.MAX_GLOBAL_FAILURES:
                                yield from self._safe_final(state, f"检测到连续失败 {_global_consecutive_failures} 次（跨工具），任务已终止。")
                                final_reply_saved = True
                                return
                            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                                args_preview += "..."
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps({"success": False, "message": f"执行失败: {error_msg[:500]}"}, ensure_ascii=False)
                            })
                            _tool_call_history.append({
                                "name": tool_name,
                                "args": args_preview,
                                "status": "failed",
                                "result": f"执行失败: {error_msg[:500]}..." if len(error_msg) > 500 else error_msg
                            })
                            step_msg = self._build_step_summary(tool_name, arguments, "", "failed", error_msg, step)
                            append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                            append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                            _save_checkpoint(step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)
                            continue
                    
                        result = result_container["result"]
                        # 工具执行已完成（子进程），对结果进行校验
                        validated = self._execute_tool_with_validation(
                            tool_name, arguments, result, self.trace_id, latency_ms=0
                        )

                        # ===== 判断是否为有效成功 =====
                        # 处理 validated 为 JSON 字符串（如 web_search 正常返回）的情况
                        _validated_data = validated
                        if isinstance(validated, str):
                            _validated_data = _safe_json_loads(validated)
                        is_valid_success = (
                            isinstance(_validated_data, dict)
                            and _validated_data.get("success", False) is True
                            and _validated_data.get("status") not in ("empty", "low_quality", "error")
                        )

                        if not is_valid_success:
                            # ===== 走失败路径 =====
                            fail_msg = ""
                            if isinstance(validated, dict):
                                fail_msg = validated.get("message", validated.get("error", "工具执行异常"))
                                # 将降级建议附加到失败信息中，使 LLM 能看到回退选项
                                suggestion = validated.get("suggestion", "")
                                if suggestion:
                                    fail_msg = f"{fail_msg}\n\n[降级建议] {suggestion}"
                            else:
                                fail_msg = str(validated)[:200]

                            # 失败分类与签名熔断
                            _cur_sig = _tool_call_signature(tool_name, arguments)
                            _failure_type = _classify_tool_failure(
                                _validated_data if isinstance(_validated_data, dict) else {}
                            )

                            if _failure_type == "fatal":
                                # 致命失败：不计数，直接标记结果让 LLM 换方案
                                fail_msg += "\n[此工具当前不可用，请使用其他工具完成目标]"
                            elif _failure_type == "file_not_found":
                                # 文件不存在：不计入任何熔断计数，重置循环检测使 LLM 可换路径重试
                                fail_msg += "\n[文件不存在，请检查路径或尝试其他位置]"
                                self._loop_detector._consecutive_exact_repeat = 0
                                self._loop_detector._last_signature = None
                            elif _failure_type == "retryable" and _cur_sig == _last_tool_call_signature:
                                _consecutive_fail_count += 1
                            elif _failure_type == "empty_result" or _failure_type == "unknown":
                                # 空结果/软失败不计入熔断计数
                                pass
                            else:
                                # 同名同参数真失败才累计
                                if _cur_sig == _last_tool_call_signature:
                                    _consecutive_fail_count += 1
                                else:
                                    _consecutive_fail_count = 1
                                    _last_tool_call_signature = _cur_sig

                            if _failure_type not in ("file_not_found", "empty_result", "unknown"):
                                _global_consecutive_failures += 1

                            get_logger().warning(
                                f"[Step {step}] 工具 {tool_name} 失败/空结果 "
                                f"(连续失败: {_consecutive_fail_count}, "
                                f"全局: {_global_consecutive_failures})"
                            )

                            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                                args_preview += "..."
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps({"success": False, "message": fail_msg[:500] if isinstance(fail_msg, str) else str(fail_msg)[:500]}, ensure_ascii=False)
                            })
                            _tool_call_history.append({
                                "name": tool_name,
                                "args": args_preview,
                                "status": "failed",
                                "result": fail_msg[:200]
                            })
                            step_msg = self._build_step_summary(tool_name, arguments, "", "failed", fail_msg, step)
                            append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                            append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")

                            # ── 自适应降级：连续空搜索结果 → 注入回退提示 ──
                            if tool_name == "web_search" and _failure_type == "empty_result":
                                _consecutive_empty_search += 1
                                if _consecutive_empty_search >= 2:
                                    _degrade_hint = (
                                        "[系统提示] web_search 连续多次未获取到有效素材。"
                                        "请停止搜索，直接基于模型已有知识完成任务，"
                                        "在数据/结论处标注'基于模型知识，未经实时搜索验证'。"
                                        "不要反复重试搜索。"
                                    )
                                    conversation_messages.append({
                                        "role": "system",
                                        "content": _degrade_hint,
                                    })
                                    get_logger().warning(
                                        f"自适应降级: 连续 {_consecutive_empty_search} 次空搜索，已注入回退提示",
                                        trace_id=self.trace_id, session_id=self.session_id
                                    )
                            elif tool_name == "web_search":
                                _consecutive_empty_search = 0  # 非空结果 → 重置

                            # 熔断检查：只有同名同参数真失败才触发熔断
                            if _consecutive_fail_count >= self.MAX_CONSECUTIVE_FAILURES:
                                yield from self._safe_final(state,
                                    f"工具 {tool_name} 以相同参数连续失败 {self.MAX_CONSECUTIVE_FAILURES} 次，任务终止。\n"
                                    f"失败原因：{fail_msg}\n请更换方案或提供更多上下文。"
                                )
                                final_reply_saved = True
                                return

                            if _global_consecutive_failures >= self.MAX_GLOBAL_FAILURES:
                                yield from self._safe_final(state,
                                    f"连续 {self.MAX_GLOBAL_FAILURES} 次工具调用失败，任务终止。"
                                )
                                final_reply_saved = True
                                return

                            continue  # ← 跳过后续成功逻辑，进入下一轮

                        # ===== 以下是真正的成功路径 =====
                        # 重置熔断计数：只要一次调用成功就清零
                        _consecutive_fail_count = 0
                        _last_tool_call_signature = ""

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
                                            yield from self._safe_final(state, "任务已取消", outcome="cancelled")
                                            final_reply_saved = True
                                            return
                                    
                                        challenge_response = confirmation_result.get("challenge_response", "")
                                        if challenge_phrase:
                                            confirmed_args = {**arguments, "challenge_token": token, "challenge_response": challenge_response}
                                        else:
                                            confirmed_args = {**arguments, "confirmation_token": token}
                                        # 预注册 token：仅 web_download 需要引擎代写（工具不自存 token）。
                                        # 统一写入 core.confirmation_manager；execute_command 等工具自身
                                        # create_challenge/create_confirmation 已自存，引擎不重复预注册
                                        # （原 _st._pending_confirmations 预注册因属性不存在从不生效，已删除）。
                                        if tool_name == "web_download":
                                            try:
                                                import time as _time
                                                from core.confirmation_manager import confirmation_manager as _cm
                                                if not _cm.pop(token):
                                                    _cm.store(token, {
                                                        "type": "download",
                                                        "url": arguments.get("url", ""),
                                                        "output_dir": arguments.get("output_dir", ""),
                                                        "expiry": _time.time() + 300,
                                                    })
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
                                            yield from self._safe_final(state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                                            final_reply_saved = True
                                            return
                                        if not result_data.get("success"):
                                            yield from self._safe_final(state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                                            final_reply_saved = True
                                            return
                                    else:
                                        if confirmation_result.get("timeout"):
                                            yield from self._safe_final(state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                                        else:
                                            yield from self._safe_final(state, "用户拒绝了操作，任务已取消", outcome="cancelled")
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
                                            yield from self._safe_final(state, "任务已取消", outcome="cancelled")
                                            final_reply_saved = True
                                            return
                                    
                                        confirmed_args = {**arguments, "challenge_token": challenge_token, "challenge_response": confirmation_result["challenge_response"]}
                                        # 注：不再预注册 challenge token——execute_command 工具侧已在返回
                                        # confirmation_required 前自行 create_challenge 存入 confirmation_manager，
                                        # 引擎此处重复预注册到 _st._pending_confirmations（属性不存在）从不生效。
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
                                            yield from self._safe_final(state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                                            final_reply_saved = True
                                            return
                                        if not result_data.get("success"):
                                            yield from self._safe_final(state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                                            final_reply_saved = True
                                            return
                                    else:
                                        if confirmation_result.get("timeout"):
                                            yield from self._safe_final(state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                                        else:
                                            yield from self._safe_final(state, "用户拒绝了操作或挑战短语验证失败，任务已取消", outcome="cancelled")
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
                    
                        # 重置熔断计数：工具执行成功
                        _consecutive_fail_count = 0
                        _last_tool_call_signature = ""
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

                        # ── ChildTurn 记录 + SubTurn 结束 ──
                        _tl.log_child_turn(
                            child_index=state.child_turn - 1,
                            ai_thinking=full_reasoning,
                            ai_output=full_content,
                            tool_name=tool_name,
                            tool_input=arguments,
                            tool_result=_tool_result_str,
                            context_memory=_build_context_memory_summary(layered_cache),
                            context_memory_note="",
                        )
                        _sl.end_sub_turn(
                            tool_result=_tool_result_str,
                            status="completed",
                        )
                    
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        result_preview = _truncate_result(_tool_result_str, max_length=self.MAX_RESULT_CHARS)
                        # 记录 tool 结果到 conversation_messages
                        conversation_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": json.dumps(result_data, ensure_ascii=False)
                        })
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
                            yield from self._safe_final(state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                            final_reply_saved = True
                            return

                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                    
                        _save_checkpoint(step, tool_name, arguments, messages_param=conversation_messages)

                        # ── Progress 进展评估 ──
                        if PROGRESS_EVALUATOR_ENABLED:
                            verdict, reason = self.progress.decide(
                                tool_name, arguments, result_data,
                                changed=result_data.get("changed"),
                                polling=result_data.get("polling"))
                        else:
                            verdict, reason = Verdict.PROGRESS, "feature_disabled"

                        if verdict == Verdict.PROGRESS:
                            pass  # 有用功，照常继续
                        elif verdict == Verdict.STALLED:
                            conversation_messages = _strip_temp_system_messages(conversation_messages)
                            conversation_messages.append({"role":"system","content":
                                f"[进展控制器] {reason}。上一步结果摘要：{str(result_data)[:200]}。"
                                f"禁止重复上一步；请更换参数/工具，或直接给出最终答复。"})
                            yield self.streamer.emit_step(f"进展停滞：{reason}", "warning")
                        elif verdict == Verdict.FUTILE:
                            final = self._build_futile_final(tool_name, arguments, reason, result_data)
                            yield from self._safe_final(state, final)
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

                        # ── ChildTurn 记录 + SubTurn 结束（失败） ──
                        _tl.log_child_turn(
                            child_index=state.child_turn - 1,
                            ai_thinking=full_reasoning,
                            ai_output=full_content,
                            tool_name=tool_name,
                            tool_input=arguments,
                            tool_result=f"工具执行失败: {error_msg}",
                            context_memory=_build_context_memory_summary(layered_cache),
                            context_memory_note="",
                        )
                        _sl.end_sub_turn(
                            tool_result=f"工具执行失败: {error_msg}",
                            status="failed",
                            error=error_msg,
                        )
                    
                        _exc_sig = _tool_call_signature(tool_name, arguments)
                        if _exc_sig == _last_tool_call_signature:
                            _consecutive_fail_count += 1
                        else:
                            _consecutive_fail_count = 1
                            _last_tool_call_signature = _exc_sig
                        _global_consecutive_failures += 1
                    
                        if _consecutive_fail_count >= self.MAX_CONSECUTIVE_FAILURES:
                            yield from self._safe_final(state, f"工具 {tool_name} 以相同参数连续失败 {self.MAX_CONSECUTIVE_FAILURES} 次，任务已终止。")
                            final_reply_saved = True
                            return
                    
                        if _global_consecutive_failures >= self.MAX_GLOBAL_FAILURES:
                            yield from self._safe_final(state, f"检测到连续失败 {_global_consecutive_failures} 次（跨工具），任务已终止以防止无限循环。")
                            final_reply_saved = True
                            return
                    
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        conversation_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": json.dumps({"success": False, "message": f"执行失败: {error_msg[:500]}"}, ensure_ascii=False)
                        })
                        _tool_call_history.append({
                            "name": tool_name,
                            "args": args_preview,
                            "status": "failed",
                            "result": f"执行失败: {error_msg[:500]}..." if len(error_msg) > 500 else error_msg
                        })

                        step_msg = self._build_step_summary(tool_name, arguments, "", "failed", error_msg, step)

                        append_session_message(self.session_id, "assistant", full_content, trace_id=self.trace_id, msg_type="tool_call")
                        append_session_message(self.session_id, "tool", step_msg, trace_id=self.trace_id, msg_type="tool_result")
                        _save_checkpoint(step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)
            except Exception as e:
                yield self.streamer.emit_error(f"处理异常: {translate_error(str(e))}")
                _save_checkpoint(step, progress=f"处理异常: {str(e)}")
        
        if cancel_event is not None and cancel_event.is_set():
            get_logger().info("检测到取消信号，跳过最终进度摘要生成",
                              trace_id=self.trace_id, session_id=self.session_id)
            yield from self._safe_final(state, "任务已取消", outcome="cancelled")
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
        
        yield from self._safe_final(state,
            f"[系统] 已达到最大推理步数 {self.MAX_TURNS}，自动结束。"
            f"已完成的步骤见上文；若任务未完成，请缩小范围后重试。",
            outcome="max_steps")
        return


    def _inject_memory_if_needed(self, messages, layered_cache):
        """将记忆作为额外的 system 消息注入，不污染 user/assistant 序列"""
        # 幂等保护：主循环每轮 step 都会调用本方法，而 layered_cache 在主循环外只构建一次、
        # 轮次间内容不变；若已存在记忆 system 消息（含 [长期记忆]/[近期交互] 标记）则直接返回，
        # 避免每轮重复插入 N 条完全相同记忆块导致上下文膨胀。
        # 全遍历而非仅查 index=1：兼容 checkpoint 恢复等场景下记忆消息位置不固定的情况。
        for _m in messages:
            if _m.get("role") == "system":
                _c = _m.get("content", "") or ""
                if "[长期记忆]" in _c or "[近期交互]" in _c:
                    return messages

        result = list(messages)  # shallow copy

        memory_parts = []
        if layered_cache.get("long_term"):
            memory_parts.append(f"[长期记忆]\n{layered_cache['long_term']}")
        if layered_cache.get("recent_structured"):
            memory_parts.append(f"[近期交互]\n{layered_cache['recent_structured']}")

        if memory_parts:
            # ✅ 作为 system 消息插入到 messages[0] 之后
            result.insert(1, {
                "role": "system",
                "content": "\n\n".join(memory_parts)
            })
        return result

    def _safe_final(self, state, text, outcome="success", **kw):
        """全任务仅 emit 一次 final。重复调用直接吞掉，防止前端收到双 final。"""
        if getattr(state, "final_emitted", False):
            return  # 已 final，静默忽略（不 yield、不 append）
        state.final_emitted = True
        try:
            append_session_message(self.session_id, "assistant", text, msg_type="final_answer")
        except Exception as e:
            import traceback
            get_logger().error(f"[ReactEngine] 保存AI回复失败: {e}", traceback=traceback.format_exc(), trace_id=self.trace_id, session_id=self.session_id)
        self._persist_task_memory(state, text, outcome=outcome)
        # ── 写入长期记忆 ──
        try:
            add_task_memory(
                session_id=self.session_id,
                turn=getattr(state, "turn", 0),
                query=getattr(state, "user_task", "") or "",
                outcome=outcome,
                key_tools=[],
                summary=text[:300],
            )
        except Exception as e:
            get_logger().warning(f"long_term.add failed: {e}", trace_id=self.trace_id, session_id=self.session_id)
        yield self.streamer.emit_final(text, **kw)

    def _persist_task_memory(self, state, final_text, outcome="success"):
        """P1-4: 任务结束时把本次会话沉淀进长期记忆，供后续 get_relevant_memory 检索。"""
        try:
            key_tools = [t for _, t, _ in getattr(self._loop_detector, "_call_history", [])]
            add_task_memory(
                session_id=self.session_id,
                turn=getattr(state, "turn", 0),
                query=getattr(state, "user_task", "") or "",
                outcome=outcome,
                key_tools=key_tools[-10:],
                summary=final_text or "",
            )
        except Exception as _mem_err:
            get_logger().warning(
                f"写入长期记忆失败: {_mem_err}",
                trace_id=self.trace_id, session_id=self.session_id,
            )

    def _load_cross_turn_history(self, user_message: str, max_entries: int = 30,
                                 max_chars: int = 6000) -> List[Dict[str, Any]]:
        """加载跨 Turn 历史对话（仅用户可见轮次），修复多轮对话模型失忆。

        数据源：SQLite chat_history（get_full_session_messages，已过滤 is_temporary）。
        注入过滤规则（按实际 msg_type 结构）：
        - role=user 且 msg_type ∈ {chat, text, ""/None} → 注入（backend/chat.py 以默认 msg_type="chat" 入库）
        - role=assistant 且 msg_type == "final_answer" → 注入（_safe_final 入库的最终回复）
        - 过滤 tool_call / tool_result / think / system 等内部消息，避免把工具结果、思考轨迹、
          确认流程等内部状态塞给 LLM
        防重复：backend 在处理请求时已将当前 user_message 写入 chat_history，
        若最后一条 user 与 user_message 相同则排除，避免当前输入被注入两次。
        截断：单条超过 max_chars 截断；总条数超过 max_entries 只保留最近 max_entries 条。
        返回标准 OpenAI 格式的 [{"role", "content"}, ...] 列表，可直接注入 messages。
        """
        try:
            history = get_full_session_messages(self.session_id, limit=500)
        except Exception as e:
            get_logger().warning(f"[跨Turn历史] 读取会话历史失败: {e}",
                                 trace_id=self.trace_id, session_id=self.session_id)
            return []

        injected: List[Dict[str, Any]] = []
        for m in history:
            role = m.get("role")
            content = m.get("content", "") or ""
            msg_type = m.get("msg_type") or "chat"
            if role == "user":
                if msg_type not in ("chat", "text"):
                    continue
            elif role == "assistant":
                if msg_type != "final_answer":
                    continue
            else:
                continue
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n...(内容过长已截断，原长度 {len(content)})..."
            injected.append({"role": role, "content": content})

        # 排除最后一条与当前输入相同的 user（防止 backend 预入库导致重复注入）
        if user_message:
            last_user_idx = None
            for i in range(len(injected) - 1, -1, -1):
                if injected[i]["role"] == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None and injected[last_user_idx]["content"] == user_message:
                injected.pop(last_user_idx)

        if len(injected) > max_entries:
            injected = injected[-max_entries:]
        return injected

    def _build_futile_final(self, tool, args, reason, result):
        """纯模板，绝不再调 LLM。诚实总结+建议。"""
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)[:300]
        except Exception:
            args_preview = str(args)[:300]
        return (f"[系统收敛] 检测到针对工具 `{tool}` 的循环已无进展，已自动停止以避免空转。\n"
                f"- 判定原因：{reason}\n"
                f"- 最近参数：{args_preview}\n"
                f"- 最近结果：{str(result)[:300]}\n"
                f"建议：更换思路/参数，或拆分为更小步骤后重试。")

    def _check_tool_risk_level(self, tool_name: str, arguments: dict) -> tuple:
        """工具执行前安全分级检查。

        根据 RISK_LEVELS 映射表检查工具的风险等级：
        - P0/P1：返回 (确认消息, 挑战短语, 风险等级) — 调用方应弹出确认窗口
        - P2/P3：返回 (None, None, None) — 直接执行

        Returns:
            (msg, challenge_phrase, risk_level):
            - msg: 描述性确认消息
            - challenge_phrase: 挑战短语（P1 带固定短语供前端渲染输入框；P0 为 None 渲染按钮）
            - risk_level: "P0" / "P1" / None
        """
        risk = self.RISK_LEVELS.get(tool_name, "P3")
        if risk in ("P0", "P1"):
            args_preview = json.dumps(arguments, ensure_ascii=False)[:300]
            level_label = "P0（最高风险）" if risk == "P0" else "P1（高风险）"
            msg = (
                f"[安全拦截] 工具 '{tool_name}' 风险等级 {level_label}，"
                f"参数：{args_preview}。"
            )
            challenge = "我知晓此操作为危险操作，确认执行。" if risk == "P1" else None
            return msg, challenge, risk
        return None, None, None

    def _execute_tool_with_validation(self, action: str, action_input: dict,
                                       raw_result, trace_id: str, latency_ms: float = 0):
        """工具结果校验与增强包装器。

        对 web_search 等工具的结果进行后处理校验：
        - 检测相关性校验失败 → 返回 {"status": "low_quality", ...}
        - 检测空结果 → 返回 {"status": "empty", ...}
        - 对 snippet 过长做截断
        - 结构化日志记录

        Returns:
            正常情况返回 raw_result 原值；
            web_search 检测到问题时返回包含 status/suggestion 的 dict。
        """
        import time

        if action == "web_search":
            # 熔断器检查：连续失败/低质量后跳过搜索
            if self._search_breaker.should_skip_search():
                return {
                    "status": "degraded",
                    "message": self._search_breaker.get_degradation_message(),
                    "results": [],
                }

            result_data = _safe_json_loads(raw_result)
            if not isinstance(result_data, dict):
                result_data = {"success": True, "output": result_data}

            # 相关性校验未通过
            if result_data.get("success") and not result_data.get("relevance_passed", True):
                self._search_breaker.record_result(False, False)
                get_logger().warning(
                    f"[{trace_id}] 搜索结果相关性校验未通过，query={action_input.get('query')}"
                )
                return {
                    "status": "low_quality",
                    "message": "搜索结果与查询无关，已全部过滤。",
                    "suggestion": (
                        "请更换更具体的搜索关键词，"
                        "或使用 fetch_webpage 直接访问目标网站。"
                    ),
                    "results": [],
                }

            results = result_data.get("results", [])
            if len(results) == 0:
                self._search_breaker.record_result(False, False)
                return {
                    "status": "empty",
                    "message": "未找到相关结果。",
                    "suggestion": (
                        "搜索未获取到有效素材。"
                        "建议：1) 缩短关键词重试  2) 或直接基于模型已有知识生成内容，"
                        "在数据处标注'(基于模型知识，未实时验证)'后继续任务。"
                        "不要反复搜索相同主题，避免无效循环。"
                    ),
                }

            # snippet 截断
            for r in results:
                snippet = r.get("snippet", "")
                if isinstance(snippet, str) and len(snippet) > 200:
                    r["snippet"] = snippet[:200] + "..."

            # 正常结果：记录成功，重置熔断器
            self._search_breaker.record_result(True, result_data.get("relevance_passed", True))
            return raw_result

        # ── 通用空结果检测（所有工具） ──
        if not raw_result or (isinstance(raw_result, str) and raw_result.strip() == ""):
            get_logger().warning(
                f"[{trace_id}] 工具 '{action}' 返回空结果",
            )
            return {
                "status": "empty",
                "success": False,
                "message": f"工具 {action} 返回空结果。引擎已注入占位提示，请勿重复调用同一工具，改用其他方案。",
            }

        # 结构化日志（若 structured_logger 存在）
        try:
            from utils.structured_logger import log_tool_call, ToolCallLog

            if action == "web_search":
                rd = _safe_json_loads(raw_result)
                log_tool_call(ToolCallLog(
                    tool_name=action,
                    tool_input=action_input,
                    tool_output={
                        "result_count": len(rd.get("results", [])) if isinstance(rd, dict) else 1
                    },
                    success=True,
                    latency_ms=latency_ms,
                    provider_used=rd.get("provider", "") if isinstance(rd, dict) else "",
                    raw_result_count=rd.get("raw_result_count", 0) if isinstance(rd, dict) else 0,
                    filtered_result_count=rd.get("filtered_result_count", 0) if isinstance(rd, dict) else 0,
                    relevance_passed=rd.get("relevance_passed", True) if isinstance(rd, dict) else True,
                ))
            else:
                log_tool_call(ToolCallLog(
                    tool_name=action,
                    tool_input=action_input,
                    tool_output={"result_count": len(raw_result) if isinstance(raw_result, str) else 1},
                    success=True,
                    latency_ms=latency_ms,
                ))
        except ImportError:
            pass

        return raw_result

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

        if status == "failed":
            error_preview = error_msg
            if len(error_preview) > self.MAX_RESULT_CHARS:
                error_preview = error_preview[:self.MAX_RESULT_CHARS - 3] + "..."

            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
状态  : ❌ 失败
失败原因: {error_preview}

⚠️ 重要提示：
- 不要重复调用同一个工具（相同参数）
- 如果工具不可用，尝试替代方案或直接告知用户
- 如果所有方案都不可行，输出 Final Answer 说明原因"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "error": error_msg[:500],
                "status": "failed", "step": step,
            }
        else:
            result_preview = result_str
            if len(result_preview) > self.MAX_RESULT_CHARS:
                result_preview = result_preview[:self.MAX_RESULT_CHARS - 3] + "..."

            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
状态  : ✅ 成功
结果  : {result_preview}
上下文: 操作已完成，请根据此结果继续后续任务"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "result_preview": result_preview[:500],
                "status": "success", "step": step,
            }
        return content

    def _check_and_compress_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检查消息列表的 token 总量，接近上限时自动压缩。

        使用 estimate_messages_tokens 估算 token 数，
        当超过 MAX_CONTEXT_TOKENS - SAFETY_MARGIN 时触发压缩。
        """
        try:
            from memory.compressor import estimate_messages_tokens
        except ImportError:
            return messages

        total_tokens = estimate_messages_tokens(messages)
        threshold = self.MAX_CONTEXT_TOKENS - self.SAFETY_MARGIN

        if total_tokens > threshold:
            get_logger().info(
                f"上下文 token 数 {total_tokens} 超过阈值 {threshold}，"
                f"触发压缩（目标：{self.MAX_CONTEXT_TOKENS}）",
                trace_id=self.trace_id, session_id=self.session_id
            )
            return self._compress_messages(messages, target_tokens=threshold)
        return messages

    def _compress_messages(self, messages: List[Dict[str, Any]], target_tokens: int) -> List[Dict[str, Any]]:
        """压缩消息列表至目标 token 数以内。

        策略：保留 system 消息 + 最近若干轮对话，中间历史合并为摘要。
        """
        try:
            from memory.compressor import estimate_tokens, estimate_messages_tokens
            from memory.session import compress_conversation_async
            from memory.config import MAX_SUMMARY_CHARS
        except ImportError:
            return messages

        # 分离 system 消息
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if not non_system:
            return messages

        # 估算每轮 token 并保留末尾未压缩的轮次
        accumulated = 0
        keep_count = 0
        for m in reversed(non_system):
            mt = estimate_tokens(m.get("content", ""))
            accumulated += mt
            keep_count += 1
            if accumulated > target_tokens // 2:
                break

        if keep_count < len(non_system):
            # 调整切割边界：确保 assistant(tool_calls) + tool 配对不被切断
            # 避免 API 400（OpenAI/DeepSeek 要求 tool 消息必须紧跟含对应 tool_call_id 的 assistant）
            cut_idx = len(non_system) - keep_count

            # 1) 如果 to_keep 第一条是 tool，向上回溯包含其 assistant
            while cut_idx < len(non_system) and non_system[cut_idx].get("role") == "tool":
                cut_idx -= 1
                keep_count = len(non_system) - cut_idx

            # 2) 如果 to_compress 最后一条是 assistant(tool_calls)，把配对的 tool 也归入 to_keep
            while cut_idx > 0 and cut_idx < len(non_system) \
                    and non_system[cut_idx - 1].get("role") == "assistant" \
                    and non_system[cut_idx - 1].get("tool_calls"):
                assistant_tool_ids = set()
                for tc in non_system[cut_idx - 1].get("tool_calls", []):
                    tid = tc.get("id") or ""
                    if tid:
                        assistant_tool_ids.add(tid)
                if not assistant_tool_ids:
                    break
                # 扫描后续 tool 消息，将匹配的移入 to_keep
                moved = False
                while cut_idx < len(non_system) \
                        and non_system[cut_idx].get("role") == "tool" \
                        and non_system[cut_idx].get("tool_call_id") in assistant_tool_ids:
                    cut_idx += 1
                    keep_count = len(non_system) - cut_idx
                    moved = True
                if not moved:
                    break

            # 用调整后的 cut_idx 重新划分
            to_compress = non_system[:cut_idx] if cut_idx > 0 else []
            to_keep = non_system[cut_idx:] if cut_idx < len(non_system) else []

            # 尝试压缩中间部分为摘要
            compressed = system_msgs + [
                {"role": "system", "content": "[上下文压缩] 以下为历史对话的压缩摘要，已省略详细轮次以节省上下文空间。"}
            ]

            try:
                if self.session_id and to_compress:
                    loop = None
                    try:
                        loop = __import__('asyncio').get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        import asyncio
                        task = asyncio.run_coroutine_threadsafe(
                            compress_conversation_async(self.session_id, to_compress),
                            loop
                        )
                        try:
                            summary = task.result(timeout=10)
                            if summary:
                                compressed.append({
                                    "role": "system",
                                    "content": f"[历史摘要] {summary[:MAX_SUMMARY_CHARS]}"
                                })
                        except Exception:
                            pass
                    else:
                        # 同步 fallback：用 estimate_tokens 做简单截断
                        total_chars = sum(len(m.get("content", "")) for m in to_compress)
                        if total_chars > self.MAX_RESULT_CHARS:
                            compressed.append({
                                "role": "system",
                                "content": f"[历史压缩] 已省略 {len(to_compress)} 轮历史对话（约 {total_chars} 字符）。"
                            })
                        else:
                            compressed.extend(to_compress)

                compressed.extend(to_keep)
            except Exception as e:
                get_logger().warning(f"上下文压缩失败，使用原始消息: {e}",
                                    trace_id=self.trace_id, session_id=self.session_id)
                return messages

            return compressed

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