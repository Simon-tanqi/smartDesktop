"""聊天后端模块：SSE 聊天路由、会话生命周期管理、确认机制、模型/日志/会话配置 API。"""
import re as _re
import time
import json
import datetime
import os
import queue
import traceback
import threading
import atexit
import uuid
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify, Response, stream_with_context, send_from_directory

from llm_client import get_available_models, ai_concat
from memory import (append_session_message, generate_trace_id,
                    get_user_id, log_operation, list_sessions)
from memory.long_term import _rewrite_md, _read_lines
from memory.config import USER_MD, FACTS_MD
from utils.error_mapper import translate_error
from logger import get_logger
from backend.agent import run_agent
from backend.routing import check_chat_layer, check_frequent_cache, check_complexity, record_success
from backend.rate_limiter import limiter
from config.settings import SCREENSHOTS_DIR, MODELS_CONFIG_PATH, USER_PROFILE_PATH, MODEL_PRESETS
from core.agent_state import checkpoint_manager
from core.react_engine import ReActEngine

chat_bp = Blueprint('chat', __name__)

# 会话 ID 会参与落盘目录拼接（memory 的 per-session 目录），必须限制字符集，
# 防止 "../" 之类的构造把数据写出数据目录。
_SESSION_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_valid_session_id(session_id) -> bool:
    """校验会话 ID：仅允许字母/数字/下划线/短横线，长度 1-64。"""
    return bool(session_id) and bool(_SESSION_ID_RE.fullmatch(str(session_id)))

# ── 全局会话状态管理 ──
_active_sessions = {}  # {session_id: {"started_at": timestamp, "thread_id": thread_id, "cancel_event": Event, "engine": ReActEngine}}
_active_sessions_lock = threading.Lock()
# ── 会话级 ReActEngine 跨 turn 缓存：_active_sessions 注销后仍保留，
#    避免每 turn 新建引擎导致 context_detector 历史丢失 ──
_session_engines = {}
_session_engines_lock = threading.Lock()


def _get_or_create_engine(session_id, trace_id="", session_name=""):
    """获取或创建会话级 ReActEngine 实例，跨 turn 复用以保留 context_detector 历史。"""
    with _session_engines_lock:
        engine = _session_engines.get(session_id)
        if engine is None:
            engine = ReActEngine(trace_id=trace_id, session_id=session_id,
                                 session_name=session_name)
            _session_engines[session_id] = engine
        return engine


def _register_session(session_id, trace_id="", session_name=""):
    """注册会话并返回 (cancel_event, request_id)。
    如果会话已存在，先取消旧任务再注册新任务。
    引入 request_id 区分同会话的不同请求，避免竞态问题。
    同时缓存会话级 ReActEngine 实例（跨 turn 复用，保留 context_detector 历史）。
    """
    cancel_event = threading.Event()
    request_id = str(uuid.uuid4())[:8]
    with _active_sessions_lock:
        existing = _active_sessions.get(session_id)
        if existing:
            existing["cancel_event"].set()
        engine = _get_or_create_engine(session_id, trace_id=trace_id, session_name=session_name)
        _active_sessions[session_id] = {
            "started_at": time.time(),
            "thread_id": threading.current_thread().ident,
            "cancel_event": cancel_event,
            "status": "running",
            "current_state": "running",
            "request_id": request_id,
            "engine": engine,
        }
    return cancel_event, request_id

def _unregister_session(session_id, request_id=None):
    """注销会话，仅当 request_id 匹配时才删除，避免竞态删除其他任务。"""
    with _active_sessions_lock:
        info = _active_sessions.get(session_id)
        if info:
            if request_id is None or info.get("request_id") == request_id:
                _active_sessions.pop(session_id, None)

def _cancel_session(session_id):
    """设置取消信号并标记会话为取消中，等待 worker 真正结束后再移除。"""
    with _active_sessions_lock:
        info = _active_sessions.get(session_id)
        if info:
            info["cancel_event"].set()
            info["status"] = "cancelling"
            info["cancelled_at"] = time.time()


def _cleanup_zombie_sessions():
    """定时清理超过阈值的僵尸会话：
    1. cancelling 状态超过 10 分钟未结束的会话
    2. running 状态但关联线程已死亡的会话
    """
    while True:
        time.sleep(60)
        now = time.time()
        zombies = []
        with _active_sessions_lock:
            for sid, info in _active_sessions.items():
                status = info.get("status")
                if status == "cancelling":
                    cancelled_at = info.get("cancelled_at", 0)
                    if now - cancelled_at > 600:
                        zombies.append(("timeout", sid))
                elif status == "running":
                    thread_id = info.get("thread_id")
                    if thread_id:
                        try:
                            import threading as th
                            thread = th._active.get(thread_id)
                            if thread is None or not thread.is_alive():
                                zombies.append(("dead_thread", sid))
                        except Exception:
                            pass
            for reason, sid in zombies:
                _active_sessions.pop(sid, None)
                get_logger().warning("清理僵尸会话", session_id=sid, reason=reason)


_cleanup_thread = threading.Thread(target=_cleanup_zombie_sessions, daemon=True, name="session-cleanup")
_cleanup_thread.start()

def _is_session_active(session_id):
    """判断指定会话当前是否处于活跃（运行中）状态。"""
    with _active_sessions_lock:
        return session_id in _active_sessions

def _has_active_sessions():
    """判断是否存在任意活跃会话。"""
    with _active_sessions_lock:
        return len(_active_sessions) > 0


def _get_session_title(session_id: str) -> str:
    """按 ID 直接查会话标题，使用统一的会话接口。"""
    try:
        from memory import get_session
        session_data = get_session(session_id)
        return session_data.get("title", "") if session_data else ""
    except Exception:
        return ""

# ── 附件标记常量 ──
_ATTACHMENT_START = "<attachments>"
_ATTACHMENT_END = "</attachments>"


def _extract_attachments(user_message: str):
    """从用户消息中提取末尾附件标记块，返回（清理后的消息, 附件路径列表）。"""
    start = user_message.find(_ATTACHMENT_START)
    if start == -1:
        return user_message, []
    end = user_message.find(_ATTACHMENT_END, start)
    if end == -1 or end <= start:
        return user_message, []
    # 仅当附件块位于消息末尾时才解析，防止正文中出现 <attachments> 字样被误判
    trailing = user_message[end + len(_ATTACHMENT_END):].strip()
    if trailing:
        return user_message, []

    file_list = []
    block = user_message[start + len(_ATTACHMENT_START):end]
    for line in block.strip().split("\n"):
        line = line.strip()
        m = _re.search(r"\[(.+?)\]\(<(.+?)>\)", line)
        if m:
            file_list.append((m.group(1), m.group(2)))

    clean = user_message[:start].strip()
    return clean, file_list


def _inject_attachment_context(user_message: str, file_list: list):
    """将附件列表重新拼接为消息尾部标记块（供上下文构建使用）。"""
    if not file_list:
        return user_message
    lines = ["", _ATTACHMENT_START]
    for name, path in file_list:
        lines.append(f"- [{name}]({path})")
    lines.append(_ATTACHMENT_END)
    return user_message + "\n".join(lines)


# ── 工具执行线程池单例 ──
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-")

def _shutdown_executor():
    """关闭全局工具执行线程池（进程退出时由 atexit 触发）。"""
    _TOOL_EXECUTOR.shutdown(wait=False)

atexit.register(_shutdown_executor)

# ── 命令确认机制（纯内存管理，无磁盘持久化） ──
_pending_confirmations: dict = {}
_pending_lock = threading.Lock()


def wait_for_confirmation(confirmation_token: str, timeout: float = 300,
                          cancel_event: threading.Event = None) -> dict:
    """阻塞等待用户确认。支持 cancel_event 提前中断（用于任务取消）。"""
    evt = threading.Event()
    result_holder = {"confirmed": False}
    with _pending_lock:
        _pending_confirmations[confirmation_token] = {"event": evt, "result": result_holder}

    start_time = time.time()
    while time.time() - start_time < timeout:
        if evt.is_set():
            break
        if cancel_event is not None and cancel_event.is_set():
            with _pending_lock:
                _pending_confirmations.pop(confirmation_token, None)
            return {"confirmed": False, "cancelled": True}
        time.sleep(0.1)

    got_signal = evt.is_set()
    with _pending_lock:
        _pending_confirmations.pop(confirmation_token, None)
    if not got_signal:
        return {"confirmed": False, "timeout": True}
    return result_holder


# ── 模型选择 ──
def _load_available_models():
    """实时读取可用模型列表（绕过模块级启动快照，保存新模型后无需重启即生效）。"""
    try:
        return get_available_models()
    except Exception as e:
        get_logger().error("加载模型列表失败，使用默认配置", error=str(e))
        return []


def _get_default_model():
    """实时返回默认模型配置（列表首项，为空时使用内置兜底）。"""
    models = _load_available_models()
    return models[0] if models else {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}


def get_model_choice(display_name=None, model_id=None):
    """按显示名或 id 选择模型配置，未命中时回退到默认模型。

    每次调用实时读取模型列表（get_available_models 走 mtime 缓存），
    设置面板保存新模型后，兜底路径无需重启即可命中。
    """
    for m in _load_available_models():
        if model_id and m.get("id") == model_id:
            return m
        if m["display"] == display_name:
            return m
    return _get_default_model()


# ── SSE 快捷响应辅助 ──
def _sse_quick_reply(text: str, session_id: str, trace_id: str, user_id: str):
    """构造并落库一个简易 SSE 回复（两步：处理中 + 最终答案）。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def generate():
        """按 SSE 协议逐行产出事件流。"""
        yield f"data: {json.dumps({'type': 'step', 'text': '处理中...', 'status': '进行中'}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'final', 'text': text, 'timestamp': now}, ensure_ascii=False)}\n\n"

    append_session_message(session_id, "assistant", text,
                           trace_id=trace_id, user_id=user_id,
                           skip_compress=True, msg_type="final_answer")
    return generate()


def _format_direct_tool_reply(result) -> str:
    """把 L1 直连工具的执行结果格式化为用户可读回复文本。

    工具返回可能是 JSON 字符串、dict 或普通字符串；失败结果转为友好的失败说明。
    """
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                if parsed.get("success") is False:
                    return f"执行失败：{parsed.get('message') or parsed.get('error') or '未知错误'}"
                msg = parsed.get("message") or parsed.get("summary") or ""
                if msg:
                    return str(msg)
        except (json.JSONDecodeError, TypeError):
            pass
        return result[:2000]
    if isinstance(result, dict):
        if result.get("success") is False:
            return f"执行失败：{result.get('message') or result.get('error') or '未知错误'}"
        return json.dumps(result, ensure_ascii=False)[:2000]
    return str(result)[:2000]


# ── 截图静态服务 ──
@chat_bp.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    """提供截图目录中的静态文件访问。"""
    return send_from_directory(SCREENSHOTS_DIR, filename)


# ── 聊天主路由 ──
@chat_bp.route("/api/chat", methods=["POST"])
def api_chat():
    """聊天主入口：多层级路由（闲聊/恢复/高频缓存）+ SSE 流式任务执行。"""
    trace_id = generate_trace_id()
    user_id = get_user_id()
    try:
        data = request.json
        user_input = data.get("message", "").strip()
        session_id = data.get("session_id")
        
        # 参数校验：session_id 缺失则直接返回错误
        get_logger().info(f"[/chat] API start: session_id={session_id}, trace_id={trace_id}", trace_id=trace_id)
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"}), 400
        if not _is_valid_session_id(session_id):
            return jsonify({"success": False, "message": "非法的 session_id"}), 400
        if not user_input:
            return jsonify({"success": False, "message": "消息内容不能为空"}), 400

        cleaned_msg, attachments = _extract_attachments(user_input)
        user_input = _inject_attachment_context(cleaned_msg, attachments)

        # 立即写入用户消息到数据库，防止连接中断导致消息丢失
        msg_id = data.get("msg_id", "")
        get_logger().info(f"[/chat] 开始保存用户消息，session_id={session_id}, msg_id={msg_id}", trace_id=trace_id)
        append_session_message(session_id, "user", user_input,
                              trace_id=trace_id, user_id=user_id,
                              skip_compress=True, msg_id=msg_id)
        get_logger().info(f"[/chat] 用户消息保存完成", trace_id=trace_id)

        # P1 写入验证：append 后立即 SELECT COUNT(*) 确认消息已落库，
        # 及时发现 INSERT OR IGNORE/约束冲突导致的静默丢消息。
        try:
            from memory.stores.sqlite_store import count_chat_messages as _count_msgs
            _msg_count = _count_msgs(session_id)
            if _msg_count <= 0:
                get_logger().error("[/chat] 用户消息写入验证失败：COUNT(*)=0，消息可能未落库",
                                   trace_id=trace_id, session_id=session_id)
            else:
                get_logger().info(f"[/chat] 用户消息写入验证通过：COUNT(*)={_msg_count}",
                                  trace_id=trace_id, session_id=session_id)
        except Exception as _ve:
            get_logger().warning(f"[/chat] 用户消息写入验证异常: {_ve}",
                                 trace_id=trace_id, session_id=session_id)

        model_choice = data.get("model_choice")
        if (not model_choice or not isinstance(model_choice, dict)
                or "provider" not in model_choice or "model" not in model_choice):
            model_display = data.get("model", None)
            model_id = data.get("model_id", None)
            model_choice = get_model_choice(model_display, model_id)

        get_logger().info("[/chat] 请求接入", session_id=session_id,
                          model=model_choice.get("model"),
                          provider=model_choice.get("provider"),
                          model_id=model_choice.get("id"),
                          model_choice=model_choice,
                          msg_len=len(user_input))

        # 直接按 ID 查会话标题，避免全量遍历 + 排序
        session_name = _get_session_title(session_id)

        # ── 第1层：闲聊匹配 ──
        chat_context = {"session_id": session_id, "user_id": user_id}
        chat_reply = check_chat_layer(user_input, context=chat_context)
        if chat_reply is not None:
            get_logger().info("[ROUTE L1] 闲聊命中 → 直接返回", session_id=session_id)
            gen = _sse_quick_reply(chat_reply, session_id, trace_id, user_id)
            return Response(stream_with_context(gen),
                            mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        # ── 第1.2层：意图分类直答（core.intent_router，零 LLM） ──
        # L0 问候/身份、L1 时间/日期/星期 → 预设回复直出；
        # L1 工具执行型 → 直连注册工具；任何异常降级回原有流程。
        intent_result = None
        try:
            from core.intent_router import classify_intent
            intent_result = classify_intent(cleaned_msg)
        except Exception as _ie:
            get_logger().warning(f"[ROUTE intent] 意图分类异常，降级原流程: {_ie}",
                                 trace_id=trace_id, session_id=session_id)

        # 长度护栏：直答仅对短句生效；复合长句（如"几点了，顺便写份报告"）
        # 若命中时间等 search 型规则会丢失后半段任务，故长句一律走原流程
        if (intent_result is not None and not intent_result.degraded
                and len(cleaned_msg) <= 30):
            # L0/L1 预设回复：本地直出，零 LLM 调用
            if intent_result.reply:
                get_logger().info(f"[ROUTE intent] {intent_result.route_level} 直答命中: {intent_result.detail}",
                                  trace_id=trace_id, session_id=session_id)
                gen = _sse_quick_reply(intent_result.reply, session_id, trace_id, user_id)
                return Response(stream_with_context(gen),
                                mimetype='text/event-stream',
                                headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
            # L1 工具执行型：校验工具存在后直连执行，结果格式化直出
            if intent_result.exec_action:
                try:
                    from tools import TOOL_REGISTRY
                    _tool_info = TOOL_REGISTRY.get(intent_result.exec_action)
                    if _tool_info is None:
                        get_logger().info(f"[ROUTE intent] L1 工具 {intent_result.exec_action} 未注册，降级原流程",
                                          trace_id=trace_id, session_id=session_id)
                    else:
                        get_logger().info(f"[ROUTE intent] L1 工具直连: {intent_result.exec_action}",
                                          trace_id=trace_id, session_id=session_id)
                        _tool_result = _tool_info["function"](**(intent_result.exec_params or {}))
                        gen = _sse_quick_reply(_format_direct_tool_reply(_tool_result),
                                               session_id, trace_id, user_id)
                        return Response(stream_with_context(gen),
                                        mimetype='text/event-stream',
                                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
                except Exception as _te:
                    get_logger().warning(f"[ROUTE intent] L1 工具直连异常，降级原流程: {_te}",
                                         trace_id=trace_id, session_id=session_id)
            # L3 命中不在此处拦截，由下方复杂度判定处强制进入规划模式

        # ── 第1.5层：任务恢复检测 ──
        # 仅当消息本身就是恢复短语时才触发，避免「继续删除文件」等误判
        # 支持带标点的输入如「继续。」「接着来！」
        resumed_checkpoint = None
        continue_keywords = {"继续", "继续任务", "接着来", "继续执行", "继续上次", "恢复任务"}
        user_input_lower = user_input.lower().strip()
        stripped_input = user_input_lower.rstrip("。！？，,?!.")
        is_continue_request = stripped_input in continue_keywords
        if is_continue_request:
            checkpoint = checkpoint_manager.get_latest_checkpoint(session_id)
            if checkpoint:
                get_logger().info("[ROUTE L1.5] 任务恢复请求 → 加载检查点",
                                  session_id=session_id, checkpoint=checkpoint.checkpoint_id)
                resumed_checkpoint = checkpoint
            else:
                get_logger().info("[ROUTE L1.5] 任务恢复请求 → 未找到检查点", session_id=session_id)

        # ── 第2层：高频固化 ──
        cache_hit, cache_reply = check_frequent_cache(user_input)
        if cache_hit and cache_reply:
            get_logger().info("[ROUTE L2] 高频固化命中 → 直接返回", session_id=session_id)
            gen = _sse_quick_reply(cache_reply, session_id, trace_id, user_id)
            return Response(stream_with_context(gen),
                            mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        def _run_agent_with_cleanup():
            """SSE 生成器：注册会话、启动 worker 线程执行任务并转发事件流，退出时统一清理。"""
            # 首先发送初始 heartbeat + 处理中占位，让前端立即感知已受理（不再表现为无响应）
            yield f"data: {json.dumps({'type': 'heartbeat', 'status': 'running'})}\n\n"
            yield f"data: {json.dumps({'type': 'step', 'text': '正在处理...', 'status': '进行中'}, ensure_ascii=False)}\n\n"

            # 会话注册移入生成器内部，确保与注销成对执行，异常时不泄漏
            cancel_event, request_id = _register_session(session_id, trace_id=trace_id, session_name=session_name)
            try:
                # 获取当前会话缓存的 ReActEngine 实例（跨 turn 复用，保留 context_detector 历史）
                engine = None
                with _active_sessions_lock:
                    _info = _active_sessions.get(session_id)
                    if _info:
                        engine = _info.get("engine")
                if engine is None:
                    engine = _get_or_create_engine(session_id, trace_id=trace_id,
                                                   session_name=session_name)
                # 构造带取消信号的确认处理器（捕获 cancel_event 闭包）
                def _confirmation_handler(token, timeout=ReActEngine.CONFIRMATION_TIMEOUT):
                    """确认处理器：进入等待确认状态并阻塞等待用户确认，结束后恢复运行状态。"""
                    with _active_sessions_lock:
                        info = _active_sessions.get(session_id)
                        if info:
                            info["current_state"] = "waiting_confirm"
                    try:
                        return wait_for_confirmation(token, timeout, cancel_event=cancel_event)
                    finally:
                        if cancel_event is None or not cancel_event.is_set():
                            with _active_sessions_lock:
                                info = _active_sessions.get(session_id)
                                if info:
                                    info["current_state"] = "running"

                event_queue: queue.Queue = queue.Queue(maxsize=100)

                def _worker():
                    """后台工作线程：先做复杂度判断（异步化，不阻塞 SSE 首包），再调用 run_agent 执行任务，把结果事件放入队列，异常时落库并转发错误。"""
                    with _active_sessions_lock:
                        info = _active_sessions.get(session_id)
                        if info:
                            info["thread_id"] = threading.current_thread().ident
                    # ── 第3层：复杂度判断（移入 worker 线程，SSE 主线程已推占位消息，不再同步阻塞） ──
                    try:
                        _complexity = check_complexity(user_input, model_choice)
                        get_logger().info(f"[ROUTE L3] 复杂度判定: {_complexity}", session_id=session_id)
                    except Exception as e:
                        get_logger().error(f"复杂度判断异常，使用默认值: {e}", trace_id=trace_id, session_id=session_id)
                        _complexity = "简单"
                    # intent_router L3 强信号命中：无论复杂度评分结果，强制进入步骤规划模式
                    if (intent_result is not None and intent_result.route_level == "L3"
                            and _complexity != "复杂"):
                        _complexity = "复杂"
                        get_logger().info("[ROUTE intent] L3 强信号命中 → 强制规划模式", session_id=session_id)
                    _planning_extra = ""
                    if _complexity == "复杂":
                        _planning_extra = (
                            "\n\n[系统指令 - 步骤规划] 这是一个复杂任务。"
                            "请在首轮以 JSON 格式输出结构化步骤规划：\n"
                            '{"plan": [{"step": 1, "description": "第一步做什么", "expected_tool": "建议工具"}, ...]}\n'
                            "输出规划后，按步骤顺序逐一执行。每完成一步，用 progress 报告完成进度（如 '已完成步骤1: xxx'）。"
                        )
                    try:
                        get_logger().info(f"[/chat] Worker线程启动，开始执行run_agent", trace_id=trace_id)
                        for chunk in run_agent(
                            user_message=user_input,
                            model_choice=model_choice,
                            session_id=session_id,
                            trace_id=trace_id,
                            user_id=user_id,
                            streaming=True,
                            session_name=session_name,
                            tool_executor=_TOOL_EXECUTOR,
                            default_model=_get_default_model(),
                            confirmation_handler=_confirmation_handler,
                            planning_extra=_planning_extra,
                            on_success=lambda msg, tn="", p=None, **kw: record_success(
                                msg, tn, p,
                                tool_call_chain=kw.get("tool_call_chain"),
                                run_was_complete=kw.get("run_was_complete", True),
                            ),
                            resumed_checkpoint=resumed_checkpoint,
                            cancel_event=cancel_event,
                            engine=engine,
                        ):
                            get_logger().info(f"[/chat] Worker线程收到chunk，长度={len(chunk)}", trace_id=trace_id)
                            while True:
                                try:
                                    event_queue.put(chunk, block=True, timeout=0.1)
                                    break
                                except queue.Full:
                                    if cancel_event is not None and cancel_event.is_set():
                                        get_logger().info("队列满时检测到取消信号，终止任务",
                                                          trace_id=trace_id, session_id=session_id)
                                        return
                            if isinstance(chunk, str) and '"type":"confirm"' in chunk:
                                get_logger().info(f"[CONFIRM-DEBUG] confirm event put to queue, len={len(chunk)}")
                        get_logger().info(f"[/chat] Worker线程run_agent执行完成", trace_id=trace_id)
                    except Exception as e:
                        import traceback as _tb
                        tb_text = _tb.format_exc()
                        get_logger().error("Agent worker 执行异常 (详细traceback)\n" + tb_text,
                                           trace_id=trace_id, session_id=session_id, error=str(e))
                        # 写 traceback 到文件，方便定位 TypeError 根因
                        _tb_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'worker_traceback.txt')
                        try:
                            with open(_tb_file, 'w', encoding='utf-8') as _f:
                                _f.write(f"TypeError 诊断 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                                _f.write(tb_text)
                        except Exception:
                            pass
                        # 异常消息落库，确保刷新页面后聊天记录仍可见（BUG-FIX: 原实现仅 SSE 推送不落库）
                        # 与 agent.py 异常兜底路径互斥：此处仅在 run_agent 生成器整体抛异常时触发
                        try:
                            append_session_message(
                                session_id, "assistant",
                                f"任务执行异常，请重试。错误详情：{str(e)}",
                                trace_id=trace_id, user_id=user_id,
                                skip_compress=True, msg_type="chat",
                            )
                        except Exception as _persist_err:
                            get_logger().warning(f"Worker异常消息落库失败: {_persist_err}",
                                                 trace_id=trace_id, session_id=session_id)
                        event_queue.put(("__error__", str(e)))
                    finally:
                        event_queue.put(None)  # 哨兵：通知消费端结束
                        get_logger().info(f"[/chat] Worker线程结束，已放入哨兵", trace_id=trace_id)

                worker_thread = threading.Thread(
                    target=_worker, daemon=True, name=f"agent-{session_id[:8]}")
                worker_thread.start()

                while True:
                    try:
                        item = event_queue.get(timeout=5)
                    except queue.Empty:
                        # 心跳事件：确认等待等长阻塞期间保持 SSE 连接存活
                        with _active_sessions_lock:
                            info = _active_sessions.get(session_id)
                            current_state = info.get("current_state", "running") if info else "running"
                        yield f"data: {json.dumps({'type': 'heartbeat', 'status': current_state}, ensure_ascii=False)}\n\n"
                        continue

                    if item is None:
                        break
                    if isinstance(item, tuple) and len(item) == 2 and item[0] == "__error__":
                        yield f"data: {json.dumps({'type': 'error', 'text': '任务执行异常，请重试。'}, ensure_ascii=False)}\n\n"
                        break
                    try:
                        if isinstance(item, str) and '"type":"confirm"' in item:
                            get_logger().info(f"[CONFIRM-DEBUG] confirm event yielding to SSE response, len={len(item)}")
                        yield item
                    except GeneratorExit:
                        get_logger().info("客户端连接已断开，终止任务",
                                          trace_id=trace_id, session_id=session_id)
                        raise
            finally:
                cancel_event.set()  # 通知 worker 尽快停止
                _unregister_session(session_id, request_id)

        return Response(
            stream_with_context(_run_agent_with_cleanup()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    except Exception as e:
        log_operation(trace_id, user_id, session_id if 'session_id' in locals() else "",
                      "error", str(e))
        get_logger().error("[/chat] 处理异常", trace_id=trace_id, error=str(e),
                           stack=traceback.format_exc())
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


# ── 取消请求 API ──
@chat_bp.route("/api/cancel", methods=["POST"])
def api_cancel():
    """取消指定会话的进行中任务。"""
    try:
        data = request.json
        session_id = data.get("session_id", "").strip()
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"}), 400

        _cancel_session(session_id)

        get_logger().info("会话已取消", session_id=session_id)
        return jsonify({"success": True, "message": "会话已取消"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ── 命令确认 API ──
@chat_bp.route("/api/confirm", methods=["POST"])
def api_confirm():
    """处理前端命令确认回调：写入确认结果并唤醒等待中的确认处理器。"""
    try:
        data = request.json
        token = data.get("confirmation_token", "").strip()
        confirmed = data.get("confirmed", False)
        challenge_response = data.get("challenge_response", "").strip()
        if not token:
            return jsonify({"success": False, "message": "缺少 confirmation_token"}), 400
        with _pending_lock:
            pending = _pending_confirmations.get(token)
        if not pending:
            return jsonify({"success": False, "message": "令牌无效或已过期"}), 404
        pending["result"]["confirmed"] = confirmed
        if challenge_response:
            pending["result"]["challenge_response"] = challenge_response
        pending["event"].set()
        get_logger().info("命令确认", confirmation_token=token[:8], confirmed=confirmed, challenge=bool(challenge_response))
        return jsonify({"success": True, "message": "已确认" if confirmed else "已取消"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ── 会话状态 API ──
@chat_bp.route("/api/session/status", methods=["GET"])
def api_session_status():
    """查询会话活跃状态：指定会话是否运行中及全部活跃会话列表。"""
    try:
        session_id = request.args.get("session_id")
        if session_id:
            is_active = _is_session_active(session_id)
            return jsonify({
                "success": True,
                "has_active_sessions": _has_active_sessions(),
                "session_active": is_active,
                "active_sessions": list(_active_sessions.keys())
            })
        else:
            return jsonify({
                "success": True,
                "has_active_sessions": _has_active_sessions(),
                "active_sessions": list(_active_sessions.keys())
            })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ── 模型配置 API ──
def _load_models_config():
    """读取模型配置文件；文件不存在时返回空结构。"""
    if os.path.exists(MODELS_CONFIG_PATH):
        with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"models": [], "active_model_id": ""}


def _save_models_config(config):
    """将模型配置写入配置文件（UTF-8 缩进 JSON）。"""
    with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_user_profile():
    """读取用户画像文件；文件不存在时返回空字典。"""
    if os.path.exists(USER_PROFILE_PATH):
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_user_profile(profile):
    """持久化用户画像，并同步更新用户信息 Markdown 与长期事实记忆。"""
    from memory import append_fact
    with open(USER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    name = profile.get("name") or profile.get("nickname") or ""
    parts = []
    if name:
        parts.append(f"- 姓名: {name}")
    if profile.get("nickname") and profile.get("name") and profile["nickname"] != profile["name"]:
        parts.append(f"- 称呼偏好: {profile['nickname']}")
    if parts:
        _rewrite_md(USER_MD, "# 用户信息\n\n" + "\n".join(parts) + "\n\n# 偏好\n\n(无)\n")

    if profile.get("background"):
        existing = _read_lines(FACTS_MD)
        text = f"- [职业背景] {profile['background']}"
        if text not in existing:
            append_fact(text)
    if profile.get("hobbies"):
        existing = _read_lines(FACTS_MD)
        text = f"- [兴趣爱好] {profile['hobbies']}"
        if text not in existing:
            append_fact(text)


@limiter.exempt
@chat_bp.route('/api/config/models', methods=['GET'])
def api_get_models_config():
    """查询当前模型配置。"""
    return jsonify({"success": True, **_load_models_config()})


@chat_bp.route('/api/config/models', methods=['POST'])
def api_save_models_config():
    """保存模型配置列表。

    设置面板唯一保存入口：整体保存 {models, active_model_id}，
    激活模型以本接口的 active_model_id 字段为准（/api/config/models/active 已废弃）。"""
    try:
        data = request.json
        if not data or "models" not in data:
            return jsonify({"success": False, "message": "缺少 models 字段"}), 400
        _save_models_config(data)
        # 保存成功后主动失效模型列表缓存，确保下次 GET /api/models 立即读到新配置
        invalidate_models_cache()
        return jsonify({"success": True, "message": "配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@chat_bp.route('/api/config/models/active', methods=['POST'])
def api_set_active_model():
    """[DEPRECATED] 设置当前激活模型 id。

    已废弃：前端设置面板不再调用本接口，激活模型统一由
    POST /api/config/models 的 active_model_id 字段整体保存。
    本接口仅保留兼容旧客户端，不影响其他功能；新代码请勿使用。
    """
    try:
        data = request.json
        model_id = data.get("model_id", "")
        config = _load_models_config()
        config["active_model_id"] = model_id
        _save_models_config(config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ── 用户信息与首次启动 API ──
@limiter.exempt
@chat_bp.route('/api/config/user_profile', methods=['GET'])
def api_get_user_profile():
    """查询用户画像。"""
    return jsonify({"success": True, "profile": _load_user_profile()})


@chat_bp.route('/api/config/user_profile', methods=['POST'])
def api_save_user_profile():
    """保存用户画像。"""
    try:
        data = request.json
        if not data or "profile" not in data:
            return jsonify({"success": False, "message": "缺少 profile 字段"}), 400
        _save_user_profile(data["profile"])
        return jsonify({"success": True, "message": "个人信息已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@limiter.exempt
@chat_bp.route('/api/config/first_run', methods=['GET'])
def api_is_first_run():
    """判断是否为首次运行（用户画像文件不存在即视为首次）。"""
    return jsonify({"is_first_run": not os.path.exists(USER_PROFILE_PATH)})


@limiter.exempt
@chat_bp.route('/api/config/model_presets', methods=['GET'])
def api_get_model_presets():
    """查询内置模型预设列表。"""
    return jsonify({"success": True, "presets": MODEL_PRESETS})


# ── 日志查询 API ──
@chat_bp.route('/logs/view')
def log_viewer():
    """提供日志查看页面静态文件。"""
    from flask import current_app
    return current_app.send_static_file('log_viewer.html')


@chat_bp.route('/api/logs/query')
def api_logs_query():
    """按日期/级别/关键词/会话名查询运行日志，并补全会话标题。"""
    lg = get_logger()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    level = request.args.get("level", "")
    keyword = request.args.get("keyword", "")
    session_name = request.args.get("session_name", "")
    limit = int(request.args.get("limit", 200))

    sid = None
    if session_name:
        for s in list_sessions():
            if session_name.lower() in s["title"].lower():
                sid = s["id"]
                break

    results = lg.query(
        date_from=date_from or None, date_to=date_to or None,
        level=level or None, keyword=keyword or None,
        session_id=sid, limit=min(limit, 500),
    )

    if results:
        session_map = {s["id"]: s["title"] for s in list_sessions()}
        for entry in results:
            eid = (entry.get("extra") or {}).get("session_id", "")
            entry["session_name"] = session_map.get(eid, "")

    return jsonify({"success": True, "logs": results})


@chat_bp.route('/api/logs/dates')
def api_logs_available_dates():
    """列出日志目录中可用的日期列表。"""
    from logger import LOG_DIR
    dates = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("agent_*.jsonl"), reverse=True):
            dates.append(f.stem.replace("agent_", ""))
    return jsonify({"success": True, "dates": dates})


@chat_bp.route('/api/logs/llm_call')
def api_logs_llm_call():
    """按会话与轮次查询某次 LLM 调用的详细记录。"""
    session_id = request.args.get("session_id", "")
    turn = int(request.args.get("turn", 0))
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    lg = get_logger()
    file_path = lg.get_llm_call_file(session_id, turn)
    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return jsonify({"success": True, "data": json.load(f)})
    return jsonify({"success": False, "message": "未找到该 LLM 调用记录"}), 404


# ── 会话管理 API ──
@chat_bp.route('/api/sessions', methods=['POST'])
def save_session():
    """保存前端会话（含消息增量去重入库），并设为活跃会话。"""
    from memory.session import load_sessions_db, save_sessions_db
    from memory.stores.sqlite_store import get_chat_messages as sql_get_messages, add_chat_message as sql_add_message
    try:
        data = request.json
        session_id = data.get("id")
        if not session_id:
            return jsonify({"success": False, "message": "缺少会话 id"}), 400
        if not _is_valid_session_id(session_id):
            return jsonify({"success": False, "message": "非法的会话 id"}), 400
        saved_count, total = 0, 0
        if session_id:
            from memory import set_active_session
            set_active_session(session_id)
            db = load_sessions_db()
            if session_id not in db:
                db[session_id] = {
                    "id": session_id,
                    "title": data.get("title", "新会话"),
                    "created": data.get("created_at", datetime.datetime.now().isoformat()),
                }
                save_sessions_db(db)
            messages = data.get("messages", [])
            total = len(messages) if messages else 0
            if messages:
                existing = sql_get_messages(session_id)
                existing_ids = {m.get("msg_id", "") for m in existing if m.get("msg_id")}
                # 用于内容级去重：后端 save_session_message 存的不带 msg_id，
                # 前端 saveSessionToBackend 发来相同内容时会被此规则过滤
                existing_ai_chat = {
                    m.get("content", "") for m in existing
                    if m.get("role") == "assistant" and m.get("msg_type") in ("chat", "text", "final_answer")
                }
                sender_to_role = {"user": "user", "ai": "assistant", "system": "system"}
                for msg in messages:
                    try:
                        role = sender_to_role.get(msg.get("sender", "ai"), "assistant")
                        msg_text = msg.get("text") or ""
                        msg_type = msg.get("msgType", "chat")
                        msg_id = msg.get("msgId", "")
                        if msg_id and msg_id in existing_ids:
                            continue
                        # 内容级去重：防止前端重发后端已存的 AI 回复（覆盖 chat/text/final_answer 三种类型）
                        if not msg_id and role == "assistant" and msg_type in ("chat", "text", "final_answer") and msg_text in existing_ai_chat:
                            continue
                        frontend_ts = msg.get("timestamp", "")
                        if frontend_ts:
                            try:
                                from datetime import datetime, timezone, timedelta
                                dt = datetime.fromisoformat(frontend_ts.replace("Z", "+00:00"))
                                local_dt = dt.astimezone(timezone(timedelta(hours=8)))
                                frontend_ts = local_dt.strftime("%Y-%m-%d %H:%M:%S")
                            except Exception:
                                frontend_ts = frontend_ts.replace("T", " ")[:19]
                        else:
                            frontend_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        sql_add_message(session_id,
                            "user" if msg.get("sender") == "user" else "assistant",
                            msg.get("text", ""),
                            msg_type=msg.get("msgType", "chat"),
                            msg_id=msg_id,
                            timestamp=frontend_ts)
                        saved_count += 1
                    except Exception as e:
                        get_logger().error(f"保存消息失败: {e}", traceback=traceback.format_exc(),
                                           session_id=session_id, msg_id=msg.get("msgId", ""))
        return jsonify({"success": True, "saved": saved_count, "total": total})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


@chat_bp.route('/api/sessions/new', methods=['POST'])
def new_session():
    """创建新会话并返回会话数据。"""
    from memory.session import create_session
    try:
        session_id = create_session(title=None)
        sessions = list_sessions()
        session_data = next((s for s in sessions if s["id"] == session_id), None)
        return jsonify({"success": True, "session": session_data})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


@limiter.exempt
@chat_bp.route('/api/sessions', methods=['GET'])
def get_sessions():
    """获取全部会话列表。"""
    try:
        sessions = list_sessions()
        return jsonify(sessions)
    except Exception as e:
        get_logger().error("获取会话列表错误", error=str(e))
        return jsonify([])


@limiter.exempt
@chat_bp.route('/api/models', methods=['GET'])
def get_models():
    """获取可用模型列表；加载失败时返回本地默认模型兜底。"""
    try:
        available_models = get_available_models()
        if not available_models:
            return jsonify([{
                "provider": "ollama",
                "model": "deepseek-r1:7b",
                "display": "本地 Ollama"
            }])
        return jsonify(available_models)
    except Exception as e:
        get_logger().error("加载模型列表错误", error=str(e))
        return jsonify([{
            "provider": "ollama",
            "model": "deepseek-r1:7b",
            "display": "本地 Ollama"
        }])


@chat_bp.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session_route(session_id):
    """删除指定会话：清理上传文件、会话数据并释放缓存引擎。"""
    from memory import delete_session
    from backend.upload_api import cleanup_session_uploads
    try:
        cleanup_session_uploads(session_id)
        delete_session(session_id)
        # 会话删除后清理缓存的 ReActEngine 实例，避免内存泄漏
        with _session_engines_lock:
            _session_engines.pop(session_id, None)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


@limiter.exempt
@chat_bp.route('/api/sessions/active', methods=['GET'])
def get_active_session():
    """获取当前活跃会话 id。"""
    from memory import get_active_session_id
    session_id = get_active_session_id()
    return jsonify({"session_id": session_id})


@limiter.exempt
@chat_bp.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    """获取指定会话的全部聊天消息（统一字段映射后返回）。"""
    from memory import get_chat_messages
    try:
        messages = get_chat_messages(session_id)
        formatted = []
        for msg in messages:
            sender = "user" if msg["role"] == "user" else "ai"
            msg_type = msg.get("msg_type") or "chat"
            # 将 text 类型统一映射为 chat，保持前后端一致性；空类型也默认 chat
            if msg_type in ("text", "", None):
                msg_type = "chat"
            formatted.append({
                "sender": sender,
                "text": msg["content"],
                "msgType": msg_type,
                "timestamp": msg.get("created_at", ""),
                "msgId": msg.get("msg_id", "")
            })
        return jsonify({"success": True, "messages": formatted})
    except Exception as e:
        get_logger().error("get_session 错误", session_id=session_id, error=str(e),
                           stack=traceback.format_exc())
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


@chat_bp.route('/api/sessions/<session_id>/rename', methods=['POST'])
def api_rename_session(session_id):
    """重命名指定会话。"""
    from memory import rename_session
    data = request.json or {}
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    ok = rename_session(session_id, new_title)
    return jsonify({"success": ok, "message": "已修改" if ok else "会话不存在"})
