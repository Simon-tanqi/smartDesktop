from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os, json, time, datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from llm_client import ai_concat, parse_ai_json, normalize_path_component, get_available_models, _ollama_call_stream, _deepseek_call_stream, get_config, get_api_key, get_base_url
from tools import TOOL_REGISTRY, build_dynamic_system_prompt, match_domains, match_persona, get_skill_prompt
from memory import (get_relevant_memory, log_session_turn, get_session_messages, append_session_message, set_active_session, get_active_session_id, list_sessions, add_chat_message, get_chat_messages, delete_session, get_user_id, generate_trace_id, log_operation, log_trace, build_context_messages, rename_session)
from memory.session import ensure_session_titles, create_session
from utils.error_mapper import translate_error
from logger import get_logger


app = Flask(__name__,template_folder='web',static_folder='web/static')

# ── CORS ──
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# ── Rate Limiting (IP 级，全 API 路由) ──
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["30 per minute", "200 per hour"],
    storage_uri="memory://",
)

# ── 工具执行线程池单例（避免每次工具调用重复新建/销毁）──
import atexit
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-")
atexit.register(_TOOL_EXECUTOR.shutdown, wait=False)

# ---------- 工作空间配置 ----------
workspace_path = os.path.dirname(os.path.abspath(__file__))
# ---------- 模型选择（可从前端传参） ----------
available_models = get_available_models()
default_model = available_models[0] if available_models else {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"} #默认使用本地模型

def get_model_choice(display_name=None):
    for m in available_models:
        if m["display"] == display_name:
            return m
    return default_model

def run_agent(user_message, model_choice, session_id=None, trace_id="", user_id="", streaming=True):
    """ReAct 循环：按 streaming 参数选择产出方式。
    streaming=True：逐步产出 SSE 事件，前端实时展示思考过程。
    streaming=False：内部收集所有步骤后一次性返回（预留，后续前端加设置项）。"""
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    # ── 模型名校验：使用白名单强制拦截异常值 ──
    _known_models = [m["model"] for m in get_available_models()]
    _known_providers = {m["model"]: m["provider"] for m in get_available_models()}
    _need_fallback = False
    if model_name not in _known_models:
        print(f"[DIAG] model_choice 模型名 '{model_name}' 不在已知列表中 ({_known_models})，强制回退默认模型。")
        _need_fallback = True
    elif _known_providers.get(model_name) != provider:
        print(f"[DIAG] model_choice provider='{provider}' 与已知模型 '{model_name}' 的预期 provider='{_known_providers[model_name]}' 不匹配，强制回退。")
        _need_fallback = True
    if _need_fallback:
        model_choice = dict(default_model)
        provider = model_choice["provider"]
        model_name = model_choice["model"]
        print(f"[DIAG] → 已强制回退为 {model_choice}")

    _call_seq = [0]
    _default_model = model_name
    _default_provider = provider

    def llm_nonstream(messages, model_name=None, provider=None):
        """非流式调用，仅用于意图识别和参数提取"""
        idx = _call_seq[0]
        _call_seq[0] += 1
        actual_model = model_name if model_name else _default_model
        actual_provider = provider if provider else _default_provider
        t0 = time.perf_counter()
        result = ai_concat(messages=messages, model_name=actual_model, provider=actual_provider, trace_id=trace_id)
        elapsed = int((time.perf_counter() - t0) * 1000)
        response = result["message"].get("content", "") if result and result.get("message") else ""
        get_logger().log_llm_call(
            trace_id=trace_id, session_id=session_id, turn_index=idx,
            messages=messages, response=response,
            model_name=actual_model, provider=actual_provider,
            elapsed_ms=elapsed,
        )
        return result

    def llm_stream(messages):
        """流式调用，产出思考过程文本 chunk，最后产出完整结果 dict"""
        idx = _call_seq[0]
        _call_seq[0] += 1
        t0 = time.perf_counter()

        if _default_provider == "ollama":
            stream_gen = _ollama_call_stream(messages, _default_model, trace_id=trace_id)
        else:
            stream_gen = _deepseek_call_stream(messages, _default_model,
                get_api_key(), get_base_url(),
                trace_id=trace_id)

        full_content = ""
        full_reasoning = ""
        try:
            for chunk in stream_gen:
                if chunk.get("type") == "chunk":
                    if chunk.get("content"):
                        full_content += chunk["content"]
                    if chunk.get("reasoning"):
                        full_reasoning += chunk["reasoning"]
                        yield chunk["reasoning"]
                elif chunk.get("type") == "result":
                    full_content = chunk.get("content", full_content)
                    full_reasoning = chunk.get("reasoning_content", full_reasoning)
                elif chunk.get("type") == "error":
                    err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
                    yield {"type": "error", "message": translate_error(err_msg)}
                    return
        except Exception as e:
            yield {"type": "error", "message": translate_error(str(e))}
            return

        elapsed = int((time.perf_counter() - t0) * 1000)
        get_logger().log_llm_call(
            trace_id=trace_id, session_id=session_id, turn_index=idx,
            messages=messages, response=full_content,
            model_name=_default_model, provider=_default_provider,
            elapsed_ms=elapsed,
        )
        yield {"content": full_content, "reasoning_content": full_reasoning}

    def emit(event_type, **kwargs):
        payload = {"type": event_type}
        payload.update(kwargs)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # ===== Phase 0: 立即发送初始状态，确保前端第一时间有反馈 =====
    yield emit("step", text="开始分析问题...", status="进行中")
    get_logger().info("Agent启动", trace_id=trace_id, session_id=session_id, user_message=user_message[:100])

    # ===== Phase 1: 本地关键词域匹配（零延迟）=====
    try:
        t0 = time.perf_counter()
        domains = match_domains(user_message)
        persona_mode = match_persona(user_message)
        elapsed_i = int((time.perf_counter() - t0) * 1000)
        log_trace(trace_id, "intent.match_domains", "intent", elapsed_i,
                  session_id=session_id, status="success",
                  input_summary=user_message[:200], output_summary=str(domains))
        get_logger().info("域匹配完成", trace_id=trace_id, session_id=session_id, domains=domains, persona=persona_mode, elapsed_ms=elapsed_i)

        # ===== Phase 2: 构建 System Prompt =====
        # 使用 build_context_messages 获取 SQLite 消息 + 压缩摘要
        history_messages = build_context_messages(session_id)
        # 分离摘要和普通消息：摘要以 system 角色注入 prompt，其余消息用于对话历史
        summary_msgs = [m for m in history_messages if m["role"] == "system" and "【历史对话摘要】" in m.get("content", "")]
        cleaned_history = [m for m in history_messages if not (m["role"] == "system" and "【历史对话摘要】" in m.get("content", ""))]

        # 规范化消息序列：合并连续同角色消息，确保 OpenAI/DeepSeek API 兼容
        def _normalize_messages(msgs):
            """合并连续同角色消息，避免 API 400 错误"""
            if not msgs:
                return []
            result = [dict(msgs[0])]  # shallow copy
            for m in msgs[1:]:
                prev = result[-1]
                if prev["role"] == m["role"]:
                    # 同角色连续出现 → 合并内容
                    prev["content"] = prev["content"] + "\n" + m["content"]
                else:
                    result.append(dict(m))
            return result

        cleaned_history = _normalize_messages(cleaned_history)

        base_system = build_dynamic_system_prompt(domains=domains, persona_mode=persona_mode)

        # 2. 按需注入长期记忆（关键词匹配 + 字符上限）
        relevant_memory = get_relevant_memory(user_message)
        if relevant_memory:
            full_system = base_system + "\n" + relevant_memory
        else:
            full_system = base_system

        # 3. 注入压缩摘要（历史对话回顾）
        if summary_msgs:
            full_system = full_system + "\n" + summary_msgs[0]["content"]

        messages = [{"role": "system", "content": full_system}] + cleaned_history

        # 避免重复追加用户消息（api_chat 已通过 append_session_message 写入历史）
        # 当 cleaned_history 最后一条 user 消息与当前 user_message 一致时跳过
        is_duplicate = False
        for m in reversed(cleaned_history):
            if m["role"] == "user":
                if m["content"].rstrip() == user_message.rstrip():
                    is_duplicate = True
                break
        if not is_duplicate:
            messages.append({"role": "user", "content": user_message})

        log_session_turn("user", user_message, trace_id=trace_id, user_id=user_id, session_id=session_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield emit("error", text=translate_error(str(e)[:500]))
        return

    # ===== Phase 3: ReAct 循环（流式）=====
    collected_steps = []
    MAX_TURNS = 15
    MAX_CONSECUTIVE_PROGRESS = 3  # 连续 progress 轮次上限，防止无限循环
    retry_count = 0
    consecutive_progress = 0
    _skills_injected = set()  # 按需注入的技能 prompt 跟踪，防止重复注入
    _final_saved = False      # 防止多次重复存储 AI 最终回复

    def _save_ai_reply(text):
        """在 yield emit("final") 之后立即持久化 AI 回复到 SQLite。"""
        nonlocal _final_saved
        if not _final_saved and session_id and text:
            _final_saved = True
            append_session_message(session_id, "assistant", text,
                                   trace_id=trace_id, user_id=user_id,
                                   skip_compress=True)

    # ── 工具执行辅助：重试 + 超时 + 缓存 ──
    _cache = {}
    _WRITE_TOOLS = {"execute_command", "modify_registry", "write_file", "edit_file",
                     "replace_in_file", "move_file", "copy_file", "delete_file",
                     "scan_directory"}

    # ── 回路护板：同类工具重复调用检测 ──
    _last_tool_name = ""
    _last_args_norm = ""
    _consecutive_same_tool = 0
    MAX_SAME_TOOL_STREAK = 5      # 连续调用同一工具 ≥5 次 → 拒绝执行本轮
    MAX_EXACT_REPEAT = 2          # 完全相同的参数 ≥2 次 → 拒绝执行本轮
    MAX_FORCED_ABORT = 8          # 同类工具连续调用 ≥8 次 → 强制终止 ReAct 循环

    def _is_retryable(exc):
        import errno
        if isinstance(exc, (ConnectionError, TimeoutError, FutureTimeout)):
            return True
        if isinstance(exc, OSError):
            return exc.errno in (errno.EAGAIN, errno.EBUSY, errno.EINTR)
        if isinstance(exc, PermissionError):
            return True
        return False

    def _try_inject_skill():
        """基础工具失败后按需注入域技能 prompt（每域仅注入一次）"""
        skill_domains = ["file_manage", "system_config"]
        for domain in domains:
            if domain in skill_domains and domain not in _skills_injected:
                skill = get_skill_prompt(domain)
                if skill:
                    messages.append({"role": "system", "content": skill})
                    _skills_injected.add(domain)
                    return True
        return False

    def _execute_tool_with_retry(tool_name, arguments, timeout=60):
        RETRY_DELAYS = [0.5, 1.0, 2.0]
        last_exception = None
        for attempt in range(3):
            try:
                future = _TOOL_EXECUTOR.submit(
                    TOOL_REGISTRY[tool_name]["function"], **arguments
                )
                return future.result(timeout=timeout)
            except FutureTimeout:
                raise TimeoutError(f"工具 {tool_name} 执行超时 ({timeout}s) 且不重试")
            except Exception as e:
                if not _is_retryable(e):
                    raise
                last_exception = e
                if attempt < 2:
                    time.sleep(RETRY_DELAYS[attempt])
        raise last_exception

    for turn in range(MAX_TURNS):
        if turn >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        # 流式 LLM 调用
        llm_gen = llm_stream(messages)
        last_result = None

        for item in llm_gen:
            if isinstance(item, dict) and item.get("type") == "error":
                yield emit("error", text=item.get("message", "AI 请求失败"))
                return
            if item is None:
                yield emit("error", text="AI 请求失败")
                return
            if isinstance(item, str):
                yield emit("thinking", text=item)
            elif isinstance(item, dict):
                last_result = item

        if not last_result:
            yield emit("error", text="AI 请求失败")
            return

        raw_content = last_result["content"]
        final_reasoning = last_result.get("reasoning_content", "").strip()

        # 提取思考摘要做去重存档
        if final_reasoning:
            short_think = final_reasoning[:300] + ("..." if len(final_reasoning) > 300 else "")
        elif raw_content.strip():
            stripped = raw_content.strip()
            if not stripped.startswith("{"):
                brace_pos = stripped.find("{")
                if brace_pos > 20:
                    short_think = stripped[:brace_pos].strip()
                elif brace_pos == -1 and len(stripped) > 20:
                    short_think = stripped[:200]
                else:
                    short_think = ""
            else:
                short_think = ""
        else:
            short_think = ""

        if short_think:
            existing_thinks = [s.get("step", "") for s in collected_steps if s.get("status") == "思考"]
            if short_think not in existing_thinks:
                collected_steps.append({"step": short_think, "status": "思考"})

        ai_content = parse_ai_json(raw_content)

        # 提取 _thinking 字段作为思考过程展示
        if ai_content and isinstance(ai_content, dict) and "_thinking" in ai_content:
            thinking_text = str(ai_content.pop("_thinking")).strip()
            if thinking_text:
                yield emit("thinking", text=thinking_text)

        # progress 类型
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            consecutive_progress += 1
            progress_msg = ai_content["progress"]
            collected_steps.append({"step": progress_msg, "status": "进行中"})

            # 连续 progress 轮次超限，强制收尾
            if consecutive_progress >= MAX_CONSECUTIVE_PROGRESS:
                yield emit("step", text=progress_msg, status="进行中")
                final_text = f"任务执行中（连续 {consecutive_progress} 轮状态更新），已中止自动循环。可以发送新指令继续。"
                yield emit("final", text=final_text,
                    timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                _save_ai_reply(final_text)
                return

            yield emit("step", text=progress_msg, status="进行中")
            # Token优化：progress 消息仅通知前端，不再注入 messages 上下文（每轮省 ~60 token）
            # LLM 无需"知道自己发了 progress"，下一步由前端驱动的 user 消息自然触发
            continue
        else:
            consecutive_progress = 0

        # final_message
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]
            yield emit("final", text=final_msg, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(final_msg)
            return

        # 纯文本回退（最多 3 次渐进式重试）
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback = raw_content
            if domains == ["general_chat"]:
                yield emit("final", text=fallback, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                _save_ai_reply(fallback)
                return
            RETRY_PROMPTS = [
                "你的回复不符合格式要求。必须严格输出 {\"tool_name\": \"...\", \"arguments\": {...}} 或 {\"final_message\": \"...\"}。",
                "仍然无效。输出的 JSON 必须可被 json.loads 解析。不要输出多余文本，只输出一个 JSON 对象。\n正确示例：\n{\"tool_name\": \"read_file\", \"arguments\": {\"file_path\": \"D:\\\\test.txt\"}}",
                "最后一次尝试。如果仍然失败，将直接返回原始内容。",
            ]
            if retry_count < len(RETRY_PROMPTS):
                messages.append({"role": "system", "content": RETRY_PROMPTS[retry_count]})
                retry_count += 1
                continue
            yield emit("final", text=fallback, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(fallback)
            return

        # 工具调用
        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})
        _is_tool_error = False  # ← 初始化错误标记

        if tool_name == "finished":
            yield emit("final", text="任务已完成", timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply("任务已完成")
            return

        # ── 硬性回路护板：执行前拦截（不再依赖 LLM 自觉）──
        args_norm = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        is_exact_repeat = (tool_name == _last_tool_name and args_norm == _last_args_norm)
        is_same_tool = (tool_name == _last_tool_name)

        if is_same_tool:
            _consecutive_same_tool += 1
        else:
            _consecutive_same_tool = 1
        _last_tool_name = tool_name
        _last_args_norm = args_norm

        # 硬性终止：全局同类工具重复 ≥ MAX_FORCED_ABORT → 强制结束 ReAct 循环
        if _consecutive_same_tool >= MAX_FORCED_ABORT:
            abort_text = f"已连续 {_consecutive_same_tool} 次调用 {tool_name}，达到系统硬性上限，已中止本轮思考。请重新描述你的需求。"
            yield emit("final", text=abort_text,
                timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(abort_text)
            return

        # 硬性拦截：连续同一工具 ≥ MAX_SAME_TOOL_STREAK → 拒绝执行本轮
        if _consecutive_same_tool >= MAX_SAME_TOOL_STREAK and is_same_tool:
            if tool_name == "execute_command":
                hint = (
                    f"请改用 write_file 将你的脚本写入 .py 文件（保存到 temp 目录），"
                    f"然后用 execute_command 执行 `python 脚本路径.py`。不要继续在内联命令中死磕。"
                )
            else:
                hint = "请立即换一种完全不同的工具或方案。"
            guard = (
                f"【回路护板-拦截】你已连续 {_consecutive_same_tool} 次调用 {tool_name}，"
                f"该调用已被系统拒绝。{hint}"
            )
            messages.append({"role": "system", "content": guard})
            _try_inject_skill()
            collected_steps.append({"step": f"拦截同工具连续调用 {tool_name}", "status": "完成"})
            yield emit("step", text=f"拦截同工具连续调用 {tool_name}", status="完成")
            _consecutive_same_tool = 0
            continue

        # 硬性拦截：完全相同的参数 ≥ MAX_EXACT_REPEAT → 拒绝执行本轮
        if _consecutive_same_tool >= MAX_EXACT_REPEAT and is_exact_repeat:
            if tool_name == "execute_command":
                hint = (
                    f"请改用 write_file 将你的脚本写入 .py 文件（保存到 temp 目录），"
                    f"然后用 execute_command 执行 `python 脚本路径.py`。不要继续在内联命令中死磕。"
                )
            else:
                hint = "请立即换一种完全不同的参数或方案。"
            guard = (
                f"【回路护板-拦截】你已连续 {_consecutive_same_tool} 次调用 {tool_name} 且参数完全相同，"
                f"该调用已被系统拒绝。{hint}"
            )
            messages.append({"role": "system", "content": guard})
            _try_inject_skill()
            collected_steps.append({"step": f"拦截完全相同调用 {tool_name}", "status": "完成"})
            yield emit("step", text=f"拦截完全相同调用 {tool_name}", status="完成")
            _consecutive_same_tool = 0
            continue

        if tool_name in TOOL_REGISTRY:
            tool_info = TOOL_REGISTRY[tool_name]
            path_params = tool_info.get("path_params", [])
            tool_timeout = tool_info.get("timeout", 60)
            for p in path_params:
                if p in arguments:
                    arguments[p] = normalize_path_component(arguments[p])

            # 缓存检查
            cache_key = (tool_name, json.dumps(arguments, sort_keys=True, default=str))
            if cache_key in _cache:
                result = _cache[cache_key]
                result_str = str(result)
                result_len = len(result_str)
                max_len = 4000 if tool_name in ("read_file", "search_content", "get_project_summary") else 2000
                if result_len > max_len:
                    if tool_name in ("read_file", "search_content", "get_project_summary"):
                        # Token优化：保留头尾结构而非简单截断，让 LLM 能同时看到开头和结尾
                        head_size = int(max_len * 0.6)
                        tail_size = int(max_len * 0.25)
                        result_str = (
                            result_str[:head_size]
                            + f"\n\n··· 中间省略 {result_len - head_size - tail_size} 字符 ···\n\n"
                            + result_str[-tail_size:]
                        )
                    else:
                        result_str = result_str[:max_len] + f"... (已截断，完整长度 {result_len} 字符)"
                collected_steps.append({"step": f"执行 {tool_name} (缓存命中)", "status": "完成", "detail": result_str[:200]})
                yield emit("step", text=f"执行 {tool_name} (缓存命中)", status="完成")
            else:
                t_tool = time.perf_counter()
                _is_tool_error = False
                # 工具执行前发送等待态，让前端显示剩余超时时间
                yield emit("step", text=f"执行 {tool_name} (最长 {tool_timeout}秒)...", status="等待中",
                           timeout=tool_timeout, tool_name=tool_name)
                try:
                    result = _execute_tool_with_retry(tool_name, arguments, timeout=tool_timeout)
                    elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                    result_str = str(result)
                    # ❌ 工具返回 success=false 时不缓存，避免重试永远命中同一个错误
                    try:
                        _parsed = json.loads(result) if isinstance(result, str) else result
                        if isinstance(_parsed, dict) and _parsed.get("success") is False:
                            _is_tool_error = True
                    except Exception:
                        pass
                    if not _is_tool_error:
                        _cache[cache_key] = result
                    if tool_name in _WRITE_TOOLS:
                        _cache = {k: v for k, v in _cache.items() if k[0] not in _WRITE_TOOLS}
                    result_len = len(result_str)
                    max_len = 4000 if tool_name in ("read_file", "search_content", "get_project_summary") else 2000
                    if result_len > max_len:
                        if tool_name in ("read_file", "search_content", "get_project_summary"):
                            head_size = int(max_len * 0.6)
                            tail_size = int(max_len * 0.25)
                            result_str = (
                                result_str[:head_size]
                                + f"\n\n··· 中间省略 {result_len - head_size - tail_size} 字符 ···\n\n"
                                + result_str[-tail_size:]
                            )
                        else:
                            result_str = result_str[:max_len] + f"... (已截断，完整长度 {result_len} 字符)"
                    # ❌ 工具返回 success=false 时，给大模型更明确的错误提示，敦促其修改方案
                    if _is_tool_error:
                        _err_short = result_str[:120].replace('\n', ' ')
                        collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": result_str[:200]})
                        yield emit("step", text=f"执行 {tool_name} 失败: {_err_short}", status="错误")
                    else:
                        collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
                        yield emit("step", text=f"执行 {tool_name}", status="完成")
                    log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                              session_id=session_id, status="error" if _is_tool_error else "success",
                              input_summary=str(arguments)[:200],
                              output_summary=result_str[:200])
                    get_logger().info("工具调用" + ("失败" if _is_tool_error else "成功"), trace_id=trace_id, session_id=session_id,
                                      tool=tool_name, elapsed_ms=elapsed_t)
                except Exception as e:
                    elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                    _err_msg = str(e)
                    result_str = f"工具执行错误: {_err_msg}"
                    collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": _err_msg})
                    # 给前端的错误提示：翻译后友好展示
                    yield emit("step", text=f"执行 {tool_name} 失败: {translate_error(_err_msg)[:100]}", status="错误")
                    log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                              session_id=session_id, status="error",
                              input_summary=str(arguments)[:200],
                              error=str(e)[:500])
                    get_logger().warning("工具调用失败", trace_id=trace_id, session_id=session_id,
                                         tool=tool_name, error=str(e)[:200], elapsed_ms=elapsed_t)
                    # 超时处理：扩展至所有工具，不只 execute_command
                    is_timeout = "超时" in _err_msg or "timeout" in _err_msg.lower()
                    if is_timeout:
                        timeout_tips = {
                            "execute_command": "如果是批量文件操作，请改用 run_python 工具编写 Python 脚本；否则请减少单次命令复杂度。",
                            "shell_executor": "如果是批量文件操作，请改用 Python 脚本；否则请简化命令或分批执行。",
                            "run_python": "请优化代码逻辑减少执行时间，或增大 timeout 参数。",
                            "web_fetch": "网页加载超时，可能是目标网站响应慢，请稍后重试或换用其他来源。",
                            "web_search": "搜索超时，请尝试缩小搜索范围或换用更具体的关键词。",
                        }
                        tip = timeout_tips.get(tool_name,
                            f"工具执行超时({tool_timeout}秒)。建议：减少单次数据量或优化执行逻辑。")
                        result_str += f"\n\n【系统提示】{tip}"
                        # 同时给前端发送用户友好的超时提示
                        yield emit("step",
                            text=f"{tool_name} 执行超时，建议：减少处理量或简化任务。",
                            status="警告")
                        _try_inject_skill()
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)
        # ✅ 工具返回 success=false 时，给大模型更明确的错误反馈和修改建议
        if _is_tool_error:
            tool_result_msg = (
                f"❌ 工具 {tool_name} 执行失败！返回结果：{result_str}\n\n"
                f"【系统提示】请仔细阅读错误信息，修改你的工具调用参数或换一种方案。\n"
                f"不要用相同参数重复调用，否则会被回路护板拦截。\n"
                f"常见修复方法：\n"
                f"- 如果是语法错误，检查代码中的引号、括号、缩进\n"
                f"- 如果是超时，优化代码逻辑减少执行时间，或增大 timeout 参数\n"
                f"- 如果是模块不存在，使用 pip install 安装或换一种实现方式\n"
                f"- 如果是文件路径错误，使用 scan_directory 先确认路径是否存在"
            )
        else:
            tool_result_msg = f"工具 {tool_name} 返回：{result_str}"
        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})

    yield emit("final", text="任务未完成，已保存进度。可以继续。", timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _save_ai_reply("任务未完成，已保存进度。可以继续。")


# ---------- Flask 路由 ----------
@app.route('/api/sessions', methods=['POST'])
def save_session():
    try:
        data = request.json
        session_id = data.get("id")
        if session_id:
            set_active_session(session_id)
            # 确保会话在 JSON DB 中存在（本地创建 fallback 路径不会写入 JSON DB）
            from memory.session import load_sessions_db, save_sessions_db
            db = load_sessions_db()
            if session_id not in db:
                db[session_id] = {
                    "id": session_id,
                    "title": data.get("title", "新会话"),
                    "created": data.get("created_at", datetime.datetime.now().isoformat()),
                }
                save_sessions_db(db)
            # 同步保存前端传递的消息到 SQLite（处理前端缓存中的消息）
            messages = data.get("messages", [])
            if messages:
                from memory.stores.sqlite_store import get_chat_messages, add_chat_message
                existing = get_chat_messages(session_id)
                # 精确去重：优先使用 msgId（UUID），回退到 (role, msg_type, content) 完整匹配
                existing_ids = {m.get("msg_id", "") for m in existing if m.get("msg_id")}
                existing_keys = {(m["role"], m.get("msg_type", "chat"), m["content"]) for m in existing if not m.get("msg_id")}
                sender_to_role = {"user": "user", "ai": "assistant", "system": "system"}
                for msg in messages:
                    role = sender_to_role.get(msg.get("sender", "ai"), "assistant")
                    msg_id = msg.get("msgId", "")
                    # 精确去重：有 msgId 时优先用它判断
                    if msg_id and msg_id in existing_ids:
                        continue
                    # 回退到完整内容三元组去重（兼容无 msgId 的旧消息，避免 content[:80] 截断碰撞）
                    if not msg_id:
                        key = (role, msg.get("msgType", "chat"), (msg.get("text") or ""))
                        if key in existing_keys:
                            continue
                    # 使用前端携带的真实时间戳，而非 SQL CURRENT_TIMESTAMP
                    frontend_ts = msg.get("timestamp", "")
                    if frontend_ts:
                        # 前端 ISO 格式 "2026-06-15T15:11:10.000Z" → SQLite 兼容 "2026-06-15 23:11:10"
                        try:
                            from datetime import datetime, timezone, timedelta
                            dt = datetime.fromisoformat(frontend_ts.replace("Z", "+00:00"))
                            local_dt = dt.astimezone(timezone(timedelta(hours=8)))
                            frontend_ts = local_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            frontend_ts = frontend_ts.replace("T", " ")[:19]
                    add_chat_message(session_id,
                        "user" if msg.get("sender") == "user" else "assistant",
                        msg.get("text", ""),
                        msg_type=msg.get("msgType", "chat"),
                        msg_id=msg_id,
                        timestamp=frontend_ts)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/api/sessions/new', methods=['POST'])
def new_session():
    """创建新会话，由后端统一分配 ID 和名称"""
    try:
        session_id = create_session(title=None)
        sessions = list_sessions()
        session_data = next((s for s in sessions if s["id"] == session_id), None)
        return jsonify({"success": True, "session": session_data})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

# 2. 修复 /api/sessions GET 方法（实际获取会话列表，而非返回空）
@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    try:
        sessions = list_sessions()   # 调用 memory 中已有的函数
        return jsonify(sessions)
    except Exception as e:
        print(f"[API] 获取会话列表错误: {e}")
        return jsonify([])

# 3. 修复 /api/models 路由（捕获异常）
@app.route('/api/models', methods=['GET'])
def get_models():
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
        print(f"[API] 加载模型列表错误: {e}")
        # 兜底返回默认模型
        return jsonify([{
            "provider": "ollama",
            "model": "deepseek-r1:7b",
            "display": "本地 Ollama"
        }])
@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session_route(session_id):
    try:
        delete_session(session_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/api/sessions/active', methods=['GET'])
def get_active_session():
    session_id = get_active_session_id()   # 若无活跃会话会自动创建
    return jsonify({"session_id": session_id})

@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    try:
        messages = get_chat_messages(session_id)
        formatted = []
        for msg in messages:
            sender = "user" if msg["role"] == "user" else "ai"
            formatted.append({
                "sender": sender,
                "text": msg["content"],
                "msgType": msg.get("msg_type", "chat"),
                "timestamp": msg.get("created_at", ""),
                "msgId": msg.get("msg_id", "")
            })
        return jsonify({"success": True, "messages": formatted})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/')
def index():
    # 因为 template_folder 已经指向 'web'，这里直接写文件名即可
    return render_template('index.html')

@app.after_request
def add_no_cache(response):
    """禁止浏览器缓存 HTML/JS 静态资源，确保前端始终拉取最新版本。"""
    if request.path == '/' or request.path.endswith('.html') or request.path.endswith('.js'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route("/api/chat", methods=["POST"])
def api_chat():
    trace_id = generate_trace_id()
    user_id = get_user_id()
    try:
        data = request.json
        user_input = data.get("message", "").strip()
        session_id = data.get("session_id")
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"}), 400

        # 存储用户消息到记忆（含事实提取、压缩检查、SQLite 写入）
        append_session_message(session_id, "user", user_input, skip_compress=False,
                               trace_id=trace_id, user_id=user_id)

        model_choice = data.get("model_choice")
        if not model_choice or not isinstance(model_choice, dict):
            model_display = data.get("model", None)
            model_choice = get_model_choice(model_display)

        print(f"[DIAG /chat] session={session_id} model_choice={model_choice} msg_len={len(user_input)}", flush=True)

        return Response(
            stream_with_context(run_agent(
                user_message=user_input,
                model_choice=model_choice,
                session_id=session_id,
                trace_id=trace_id,
                user_id=user_id,
                streaming=True
            )),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    except Exception as e:
        log_operation(trace_id, user_id, session_id if 'session_id' in locals() else "",
                      "error", str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": translate_error(str(e))}), 500



# ---------- 日志查询 API ----------
@app.route('/logs/view')
def log_viewer():
    return app.send_static_file('log_viewer.html')


@app.route('/api/logs/query')
def api_logs_query():
    lg = get_logger()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    level = request.args.get("level", "")
    keyword = request.args.get("keyword", "")
    session_name = request.args.get("session_name", "")
    limit = int(request.args.get("limit", 200))

    # 会话名称 → session_id 映射
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

    # 为每条日志附加 session_name
    if results:
        session_map = {s["id"]: s["title"] for s in list_sessions()}
        for entry in results:
            eid = (entry.get("extra") or {}).get("session_id", "")
            entry["session_name"] = session_map.get(eid, "")

    return jsonify({"success": True, "logs": results})


@app.route('/api/logs/dates')
def api_logs_available_dates():
    from logger import LOG_DIR
    dates = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("agent_*.jsonl"), reverse=True):
            dates.append(f.stem.replace("agent_", ""))
    return jsonify({"success": True, "dates": dates})


@app.route('/api/logs/llm_call')
def api_logs_llm_call():
    trace_id = request.args.get("trace_id", "")
    turn = int(request.args.get("turn", 0))
    if not trace_id:
        return jsonify({"success": False, "message": "缺少 trace_id"}), 400
    lg = get_logger()
    file_path = lg.get_llm_call_file(trace_id, turn)
    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return jsonify({"success": True, "data": json.load(f)})
    return jsonify({"success": False, "message": "未找到该 LLM 调用记录"}), 404


@app.route('/api/sessions/<session_id>/rename', methods=['POST'])
def api_rename_session(session_id):
    data = request.json or {}
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    ok = rename_session(session_id, new_title)
    return jsonify({"success": ok, "message": "已修改" if ok else "会话不存在"})


# ---------- 启动 ----------
if __name__ == '__main__':
    ensure_session_titles()
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)