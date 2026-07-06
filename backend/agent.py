"""
ReAct 循环引擎：流式 Agent 推理与工具执行。
从 app.py 中提取，降低主文件复杂度。
"""
import os, json, time, datetime, re as _re
import errno
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from llm_client import (
    ai_concat, parse_ai_json, normalize_path_component,
    get_available_models, _ollama_call_stream, _deepseek_call_stream,
    get_api_key, get_base_url,
)
from tools import (
    TOOL_REGISTRY, build_dynamic_system_prompt, build_tools_param,
    match_domains, match_persona, get_skill_prompt, get_session_domains,
)
from tools import skill_learning  # 经验内化模块
from memory import (
    get_relevant_memory, log_session_turn,
    append_session_message, log_trace, build_context_messages,
)
from logger import get_logger
from utils.error_mapper import translate_error


def run_agent(
    user_message,
    model_choice,
    session_id=None,
    trace_id="",
    user_id="",
    streaming=True,
    session_name="",
    tool_executor=None,
    default_model=None,
    confirmation_handler=None,
    planning_extra="",
    on_success=None,
):
    """ReAct 循环：按 streaming 参数选择产出方式。
    streaming=True：逐步产出 SSE 事件，前端实时展示思考过程。
    streaming=False：内部收集所有步骤后一次性返回（预留，后续前端加设置项）。

    外部依赖注入参数：
    - tool_executor: ThreadPoolExecutor 实例，不传则自动创建
    - default_model: 默认模型回退配置
    - confirmation_handler: callable(token, timeout) → {"confirmed": bool}，用于 execute_command 确认
    """
    provider = model_choice["provider"]
    model_name = model_choice["model"]
    _temperature = model_choice.get("temperature", 0.3)
    _base_url = model_choice.get("base_url", "https://api.deepseek.com")

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
        if default_model is None:
            _available = get_available_models()
            default_model = _available[0] if _available else {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}
        model_choice = dict(default_model)
        provider = model_choice["provider"]
        model_name = model_choice["model"]
        print(f"[DIAG] → 已强制回退为 {model_choice}")

    _call_seq = [0]
    _default_model = model_name
    _default_provider = provider

    # 外部未注入则自动创建线程池
    if tool_executor is None:
        tool_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-")

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
            elapsed_ms=elapsed, session_name=session_name,
            reasoning="",
        )
        return result

    def llm_stream(messages, system_prompt="", user_input="", session_id=None):
        """流式调用，产出思考过程文本 chunk，最后产出完整结果 dict"""
        idx = _call_seq[0]
        _call_seq[0] += 1
        t0 = time.perf_counter()

        # 构建工具参数 - 基于匹配的域和已加载的域
        tools_param = None
        if session_id:
            loaded_domains = get_session_domains(session_id)
            # 合并关键词匹配的域和已加载的域
            all_domains = set(domains) | set(loaded_domains)
            tools_param = build_tools_param(loaded_domains=list(all_domains))

        if _default_provider == "ollama":
            stream_gen = _ollama_call_stream(messages, _default_model, trace_id=trace_id, tools=tools_param)
        else:
            stream_gen = _deepseek_call_stream(messages, _default_model,
                get_api_key(), _base_url,
                trace_id=trace_id, tools=tools_param)

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
            trace_id=trace_id, session_id=session_id,
            turn=state.turn, child_turn=state.child_turn,
            user_task=state.user_task,
            response=full_content,
            model_name=_default_model, provider=_default_provider,
            elapsed_ms=elapsed, session_name=session_name,
            reasoning=full_reasoning,
            system_prompt=system_prompt,
            user_input=user_input,
            messages=messages,
            user_task_time=state.user_task_time,
        )
        state.child_turn += 1
        yield {"content": full_content, "reasoning_content": full_reasoning}

    def emit(event_type, **kwargs):
        payload = {"type": event_type}
        payload.update(kwargs)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # ===== Phase 0: 立即发送初始状态，确保前端第一时间有反馈 =====
    yield emit("step", text="开始分析问题...", status="进行中")
    get_logger().info("Agent启动", trace_id=trace_id, session_id=session_id, user_message=user_message[:100])

    user_message_with_memory = user_message

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

        # ===== Phase 1.5: 检测经验内化意图 =====
        if skill_learning.is_skill_learning_request(user_message):
            yield emit("step", text="检测到经验内化请求，正在提取任务处理步骤...", status="进行中")
            get_logger().info("经验内化触发", trace_id=trace_id, session_id=session_id)

            try:
                # 1. 获取对话历史
                history_messages = build_context_messages(session_id)
                
                # 2. 提取任务处理步骤
                task_info = skill_learning.extract_task_steps_from_history(history_messages)
                
                # 3. 建议技能名称（使用非流式 LLM 调用）
                def _llm_wrapper(messages):
                    return llm_nonstream(messages)
                
                skill_name = skill_learning.suggest_skill_name(task_info, _llm_wrapper)
                
                # 4. 生成 Skill 内容
                skill_content = skill_learning.generate_skill_with_llm(task_info, _llm_wrapper)
                
                # 5. 保存 Skill（默认版本 1.0）
                save_result = skill_learning.save_skill(skill_name, skill_content, version="1.0")
                
                if save_result["success"]:
                    # 6. 从生成的 Skill 内容中提取关键词
                    extracted_keywords = skill_learning._extract_keywords_from_skill(skill_content)
                    
                    # 如果提取失败，使用任务描述作为备选
                    if not extracted_keywords:
                        extracted_keywords = [task_info['task_description'][:30]]
                    
                    # 7. 注册到 agent_tools.yaml（使用提取的关键词）
                    register_result = skill_learning.register_skill_to_yaml(
                        skill_name,
                        description=f"自动学习的技能：{task_info['task_description'][:50]}",
                        keywords=extracted_keywords,
                        version="1.0"
                    )
                    
                    success_msg = f"✅ 经验内化完成！\n\n技能名称：**{skill_name}**\n版本：**{save_result['version']}**\n保存路径：`{save_result['file_path']}`\n"
                    
                    if register_result["success"]:
                        success_msg += "\n✅ 技能已自动注册，下次对话即可使用！"
                    else:
                        success_msg += f"\n⚠️ 技能已保存，但注册失败：{register_result['message']}"
                    
                    yield emit("final", text=success_msg,
                        timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    _save_ai_reply(success_msg)
                    get_logger().info("经验内化成功", trace_id=trace_id, session_id=session_id, 
                                      skill_name=skill_name, file_path=save_result['file_path'])
                else:
                    yield emit("error", text=f"❌ 经验内化失败：{save_result['message']}")
                    _save_ai_reply(f"经验内化失败：{save_result['message']}")
                    get_logger().error("经验内化失败", trace_id=trace_id, session_id=session_id,
                                       error=save_result['message'])
            except Exception as e:
                import traceback
                traceback.print_exc()
                yield emit("error", text=f"❌ 经验内化失败：{translate_error(str(e))}")
                _save_ai_reply(f"经验内化失败：{translate_error(str(e))}")
                get_logger().error("经验内化异常", trace_id=trace_id, session_id=session_id, error=str(e))
            
            return

        # ===== Phase 2: 构建消息序列和工具配置 =====
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

        # 2. 按需检索长期记忆（关键词匹配 + 字符上限）
        relevant_memory = get_relevant_memory(user_message)

        # 3. 收集压缩摘要（历史对话回顾）
        summary_text = ""
        if summary_msgs:
            summary_text = summary_msgs[0]["content"]

        # 4. 使用 LLM 生成历史对话摘要（提取工具调用、结果、状态）
        historical_summary = ""
        chat_user_count = len([m for m in cleaned_history if m.get("role") == "user" and m.get("msg_type") == "chat"])
        if chat_user_count > 3 and cleaned_history:
            chat_user_indices = [i for i, m in enumerate(cleaned_history) 
                                  if m.get("role") == "user" and m.get("msg_type") == "chat"]
            if len(chat_user_indices) > 3:
                cutoff_idx = chat_user_indices[-3]
                early_messages = cleaned_history[:cutoff_idx]
                
                early_context = "\n\n".join([f"{m['role']}: {m['content']}" for m in early_messages])
                if early_context:
                    summary_prompt = f"""请根据以下历史对话，生成一段简洁的自然语言摘要。
重点提取：每轮用户意图、工具调用、执行结果（成功/失败）、当前状态。
不要输出JSON，只用自然语言描述。

历史对话：
{early_context[:3000]}

摘要："""
                    
                    try:
                        t0 = time.perf_counter()
                        summary_result = ai_concat(messages=[{"role": "user", "content": summary_prompt}], 
                                                   model_name=_default_model, provider=_default_provider, 
                                                   trace_id=trace_id)
                        elapsed = int((time.perf_counter() - t0) * 1000)
                        get_logger().info("LLM摘要生成完成", trace_id=trace_id, session_id=session_id, elapsed_ms=elapsed)
                        historical_summary = summary_result["message"].get("content", "").strip()
                        if historical_summary:
                            historical_summary = f"【历史对话摘要】\n{historical_summary}"
                    except Exception as e:
                        get_logger().warning("LLM摘要生成失败", trace_id=trace_id, session_id=session_id, error=str(e))

        # 5. 组装外部记忆块（长期记忆 + 压缩摘要 + 历史摘要 + 复杂度规划），统一注入用户消息
        extra_memory_parts = []
        if relevant_memory:
            extra_memory_parts.append(relevant_memory)
        if historical_summary:
            extra_memory_parts.append(historical_summary)
        elif summary_text:
            extra_memory_parts.append(summary_text)
        if planning_extra:
            extra_memory_parts.append(planning_extra)
        extra_memory = "\n\n".join(extra_memory_parts) if extra_memory_parts else ""

        # 构建静态系统提示词（不包含工具信息）
        full_system = build_dynamic_system_prompt(
            domains=domains, persona_mode=persona_mode
        )

        # 构建用户上下文（包含记忆信息但不包含工具列表）
        user_context = build_user_context(
            domains=domains,
            extra_memory=extra_memory,
            plan_mode=(planning_extra != ""),
            persona_mode=persona_mode,
            include_tools=False  # 工具通过tools参数传递，不在消息中
        )

        user_message_with_context = user_message
        if user_context:
            user_message_with_context = f"{user_context}\n\n{user_message}"

        messages = [{"role": "system", "content": full_system}] + cleaned_history

        # 避免重复追加用户消息
        is_duplicate = False
        for m in reversed(cleaned_history):
            if m["role"] == "user":
                if m["content"].rstrip() == user_message_with_context.rstrip():
                    is_duplicate = True
                break
        if not is_duplicate:
            messages.append({"role": "user", "content": user_message_with_context})

        log_session_turn("user", user_message, trace_id=trace_id, user_id=user_id, session_id=session_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield emit("error", text=translate_error(str(e)[:500]))
        _save_ai_reply(f"错误：{translate_error(str(e)[:500])}")
        return

    # ===== Phase 3: ReAct 循环（流式）=====
    collected_steps = []
    MAX_TURNS = 15
    MAX_CONSECUTIVE_PROGRESS = 3  # 连续 progress 轮次上限，防止无限循环
    retry_count = 0
    consecutive_progress = 0
    _skills_injected = set()  # 按需注入的技能 prompt 跟踪，防止重复注入
    _final_saved = False      # 防止多次重复存储 AI 最终回复
    _pending_images = []      # (img_md, file_path) 截图结果，在 final 出口统一注入

    class _State:
        turn = 0
        child_turn = 0
        user_task = user_message
        user_task_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = _State()

    if session_id:
        llm_file = get_logger().get_llm_call_file(session_id=session_id, session_name=session_name)
        if llm_file:
            try:
                with open(llm_file, 'r', encoding='utf-8') as f:
                    llm_data = json.load(f)
                turns = llm_data.get('turns', [])
                if turns:
                    state.turn = max(t.get('turn', 0) for t in turns) + 1
            except Exception:
                pass

    def _save_ai_reply(text):
        """在 yield emit("final") 之后立即持久化 AI 回复到 SQLite，并记录日志。"""
        nonlocal _final_saved
        if not _final_saved and session_id and text:
            _final_saved = True
            append_session_message(session_id, "assistant", text,
                                   trace_id=trace_id, user_id=user_id,
                                   skip_compress=True)
            get_logger().log_turn_complete(session_id=session_id, session_name=session_name,
                                           turn=state.turn, ai_reply=text)

    def _inject_result_images(text):
        """将本轮任务中产生的截图 Markdown 图片链接和文件路径注入最终回复。"""
        nonlocal _pending_images
        if not _pending_images:
            return text
        lines = []
        for img_md, file_path in _pending_images:
            lines.append(img_md)
            lines.append(f"文件路径: `{file_path}`")
        _pending_images = []
        return text + "\n\n" + "\n\n".join(lines)

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

    # ── 熔断：同一工具连续失败跟踪（基于工具返回 success:false / error / 超时）──
    _consecutive_tool_failures = {}   # {tool_name: consecutive_failure_count}
    MAX_CONSECUTIVE_FAILURES = 3      # 同一工具连续失败≥3次 → 熔断
    _failure_breaker_msg_injected = False  # 熔断消息已注入后，下一轮仍调用工具则硬性拦截

    # ── 工业级鲁棒性：渐进式熔断升级 ──
    _breaker_hits = 0              # 当前会话熔断命中次数，成功调用时清零
    _last_error_category = ""       # 最近一次错误的分类（dependency/logic/syntax/io/unknown）
    _last_exception_type = ""       # 最近一次异常类型名

    def _reset_breaker_state():
        """工具执行成功后重置所有熔断状态。"""
        nonlocal _breaker_hits, _last_error_category, _last_exception_type, _consecutive_same_tool, _consecutive_tool_failures, _failure_breaker_msg_injected
        _breaker_hits = 0
        _last_error_category = ""
        _last_exception_type = ""
        _consecutive_same_tool = 0
        _consecutive_tool_failures = {}
        _failure_breaker_msg_injected = False

    def _try_extract_error_info(result_str: str):
        """从工具返回的 result_str 中提取结构化错误信息。"""
        nonlocal _last_error_category, _last_exception_type
        try:
            parsed = json.loads(result_str)
            if isinstance(parsed, dict) and parsed.get("success") is False:
                cat = parsed.get("error_category", "")
                exc = parsed.get("exception_type", "")
                if cat:
                    _last_error_category = cat
                if exc:
                    _last_exception_type = exc
        except Exception:
            # result_str 不是 JSON，尝试从文本中匹配
            for exc_name in ["TypeError", "AttributeError", "NameError", "ImportError",
                             "ModuleNotFoundError", "FileNotFoundError", "PermissionError",
                             "ValueError", "KeyError", "SyntaxError", "TimeoutError"]:
                if exc_name in result_str:
                    _last_exception_type = exc_name
                    break

    def _is_retryable(exc):
        if isinstance(exc, (ConnectionError, TimeoutError, FutureTimeout)):
            return True
        if isinstance(exc, OSError):
            return exc.errno in (errno.EAGAIN, errno.EBUSY, errno.EINTR)
        if isinstance(exc, PermissionError):
            return True
        return False

    def _try_inject_skill():
        """基础工具失败后按需注入域技能 prompt（每域仅注入一次）"""
        skill_domains = ["system_config"]
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
            future = None
            try:
                future = tool_executor.submit(
                    TOOL_REGISTRY[tool_name]["function"], **arguments
                )
                return future.result(timeout=timeout)
            except FutureTimeout:
                if future:
                    future.cancel()  # 防止僵尸线程永久占用 worker
                raise TimeoutError(f"工具 {tool_name} 执行超时 ({timeout}s) 且不重试")
            except Exception as e:
                if not _is_retryable(e):
                    raise
                last_exception = e
                if attempt < 2:
                    time.sleep(RETRY_DELAYS[attempt])
        raise last_exception

    for react_iter in range(MAX_TURNS):
        if react_iter >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        last_user_msg = user_message_with_memory
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", last_user_msg)
                break

        llm_gen = llm_stream(messages, system_prompt=full_system, user_input=last_user_msg, session_id=session_id)
        last_result = None

        for item in llm_gen:
            if isinstance(item, dict) and item.get("type") == "error":
                yield emit("error", text=item.get("message", "AI 请求失败"))
                _save_ai_reply(f"AI 请求失败：{item.get('message', '')}")
                return
            if item is None:
                yield emit("error", text="AI 请求失败")
                _save_ai_reply("AI 请求失败")
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

        # DeepSeek 推理模型可能把 JSON 放在 reasoning_content 而非 content 中。
        parse_source = raw_content
        if not raw_content.strip() and final_reasoning:
            parse_source = final_reasoning
        ai_content = parse_ai_json(parse_source)

        # 归一化：兼容 LLM 误用 "tool_call" 代替 "tool_name"
        if ai_content and isinstance(ai_content, dict) and "tool_call" in ai_content and "tool_name" not in ai_content:
            ai_content["tool_name"] = ai_content.pop("tool_call")
            # 如果有 "command"/"params" 等旧格式 key，合并到 arguments
            if "arguments" not in ai_content:
                ai_content["arguments"] = {}
            for legacy_key in ("command", "params", "args"):
                if legacy_key in ai_content:
                    ai_content["arguments"][legacy_key] = ai_content.pop(legacy_key)

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

            if consecutive_progress >= MAX_CONSECUTIVE_PROGRESS:
                yield emit("step", text=progress_msg, status="进行中")
                final_text = f"任务执行中（连续 {consecutive_progress} 轮状态更新），已中止自动循环。可以发送新指令继续。"
                final_text = _inject_result_images(final_text)
                yield emit("final", text=final_text,
                    timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                _save_ai_reply(final_text)
                return

            yield emit("step", text=progress_msg, status="进行中")
            continue
        else:
            consecutive_progress = 0

        # final_message
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]
            final_msg = _inject_result_images(final_msg)
            yield emit("final", text=final_msg, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(final_msg)
            if on_success:
                on_success(user_message)
            return

        # 纯文本回退（最多 3 次渐进式重试）
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback = raw_content
            # 剥离 <think> 标签
            if isinstance(fallback, str):
                fallback_clean = _re.sub(r'<think>.*?</think>', '', fallback, flags=_re.DOTALL).strip()
                fallback = fallback_clean if fallback_clean else ""
            if domains == ["general_chat"]:
                if not fallback or not fallback.strip():
                    fallback = "抱歉，当前模型未返回有效内容。请尝试重新描述你的需求，或换个角度提问。"
                fallback = _inject_result_images(fallback)
                yield emit("final", text=fallback, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                _save_ai_reply(fallback)
                if on_success:
                    on_success(user_message)
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
            if not fallback or not fallback.strip():
                fallback = "抱歉，当前模型未返回有效内容。请尝试重新描述你的需求，或换个角度提问。"
            fallback = _inject_result_images(fallback)
            yield emit("final", text=fallback, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(fallback)
            if on_success:
                on_success(user_message)
            return

        # 工具调用
        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})
        _is_tool_error = False
        
        # 特殊处理：load_domain_skills工具自动注入session_id
        if tool_name == "load_domain_skills" and session_id:
            if "session_id" not in arguments:
                arguments["session_id"] = session_id

        if tool_name == "finished":
            done_text = _inject_result_images("任务已完成")
            yield emit("final", text=done_text, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(done_text)
            if on_success:
                on_success(user_message)
            return

        # ── 熔断硬拦截：上一轮已注入熔断指令，LLM 无视并继续调用工具 → 强制终止 ──
        if _failure_breaker_msg_injected:
            abort_text = "已触发熔断保护：上一轮已要求停止工具调用，但你仍尝试调用工具。本轮对话已终止。请重新描述需求。"
            abort_text = _inject_result_images(abort_text)
            yield emit("final", text=abort_text,
                timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(abort_text)
            _failure_breaker_msg_injected = False
            return

        # ── 硬性回路护板：执行前拦截 ──
        args_norm = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        is_exact_repeat = (tool_name == _last_tool_name and args_norm == _last_args_norm)
        is_same_tool = (tool_name == _last_tool_name)

        if is_same_tool:
            _consecutive_same_tool += 1
        else:
            _consecutive_same_tool = 1
        _last_tool_name = tool_name
        _last_args_norm = args_norm

        if _consecutive_same_tool >= MAX_FORCED_ABORT:
            abort_text = f"已连续 {_consecutive_same_tool} 次调用 {tool_name}，达到系统硬性上限，已中止本轮思考。请重新描述你的需求。"
            abort_text = _inject_result_images(abort_text)
            yield emit("final", text=abort_text,
                timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_ai_reply(abort_text)
            return

        if _consecutive_same_tool >= MAX_SAME_TOOL_STREAK and is_same_tool:
            _breaker_hits += 1
            _try_inject_skill()

            # ── 工业级鲁棒性：渐进式熔断 + 认输出口 ──
            if _breaker_hits == 1:
                guard = (
                    f"【熔断 L1】{tool_name} 已连续失败 {_consecutive_same_tool} 次。"
                    f"请仔细阅读上方错误信息，修改参数或换方案。不要用相同参数重试。"
                )
            elif _breaker_hits == 2:
                guard = (
                    f"【熔断 L2】{tool_name} 已连续失败 {_consecutive_same_tool} 次。"
                    f"请立即完全放弃当前方案，换一种截然不同的方法。"
                    f"如果判断已无法完成，直接告知用户：尝试了 {tool_name} 多次均失败，失败原因为 [从上方错误信息中提取]，建议手动操作或换方案。"
                )
            else:
                guard = (
                    f"【熔断 L3 - 强制降级】{tool_name} 已连续失败 {_consecutive_same_tool} 次。"
                    f"禁止继续使用 {tool_name}。请告知用户任务无法完成并说明原因。"
                )

            messages.append({"role": "system", "content": guard})
            collected_steps.append({"step": f"熔断 L{_breaker_hits}: 拦截 {tool_name}", "status": "完成"})
            yield emit("step", text=f"熔断 L{_breaker_hits}: 拦截 {tool_name}", status="完成")
            _consecutive_same_tool = 0
            continue

        if _consecutive_same_tool >= MAX_EXACT_REPEAT and is_exact_repeat:
            _breaker_hits += 1
            skill_injected = _try_inject_skill()

            if _breaker_hits == 1:
                guard = (
                    f"【熔断 L1】你已连续 {_consecutive_same_tool} 次调用 {tool_name} 且参数完全相同。"
                    f"该调用已被拒绝。请更换参数或换一种方案。"
                )
            else:
                guard = (
                    f"【熔断 L{_breaker_hits}】重复调用同一工具同一参数已达 {_consecutive_same_tool} 次。"
                    f"请立即完全停止当前方案，切换工具或人工介入。"
                )
            messages.append({"role": "system", "content": guard})
            collected_steps.append({"step": f"熔断 L{_breaker_hits}: 拦截重复调用 {tool_name}", "status": "完成"})
            yield emit("step", text=f"熔断 L{_breaker_hits}: 拦截重复调用 {tool_name}", status="完成")
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
                yield emit("step", text=f"执行 {tool_name} (最长 {tool_timeout}秒)...", status="等待中",
                           timeout=tool_timeout, tool_name=tool_name)
                try:
                    result = _execute_tool_with_retry(tool_name, arguments, timeout=tool_timeout)
                    # ── 确认拦截：任何工具返回 confirmation_required 时弹窗确认 ──
                    try:
                        _r = json.loads(result) if isinstance(result, str) else result
                    except Exception:
                        _r = {}
                    if isinstance(_r, dict) and _r.get("confirmation_required"):
                        _token = _r.get("confirmation_token", "")
                        _msg = _r.get("message", "")
                        _title = _r.get("title", "操作确认")
                        _details = _r.get("details", {})
                        _cmd = _r.get("command", "")  # execute_command 兼容
                        yield emit("confirm",
                                   tool_name=tool_name,
                                   command=_cmd,
                                   title=_title,
                                   message=_msg,
                                   details=_details,
                                   confirmation_token=_token)
                        if confirmation_handler:
                            confirm_result = confirmation_handler(_token, timeout=300)
                            if confirm_result.get("confirmed"):
                                confirmed_args = dict(arguments)
                                confirmed_args["confirmed"] = True
                                confirmed_args["confirmation_token"] = _token
                                result = _execute_tool_with_retry(tool_name, confirmed_args, timeout=tool_timeout)
                            else:
                                result = json.dumps({"success": False, "message": "用户取消操作"}, ensure_ascii=False)
                                _is_tool_error = True
                        else:
                            result = _r
                    # ── 确认拦截结束 ──
                    elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                    result_str = str(result)
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
                    # ── 工业级鲁棒性：错误信息不截断，LLM 必须看到完整 traceback 才能诊断 ──
                    if not _is_tool_error:
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
                    if _is_tool_error:
                        _try_extract_error_info(result_str)
                        _err_short = result_str[:120].replace('\n', ' ')
                        collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": result_str[:200]})
                        yield emit("step", text=f"执行 {tool_name} 失败: {_err_short}", status="错误")
                    else:
                        collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
                        yield emit("step", text=f"执行 {tool_name}", status="完成")
                        _reset_breaker_state()
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
                    yield emit("step", text=f"执行 {tool_name} 失败: {translate_error(_err_msg)[:100]}", status="错误")
                    log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                              session_id=session_id, status="error",
                              input_summary=str(arguments)[:200],
                              error=str(e)[:500])
                    get_logger().warning("工具调用失败", trace_id=trace_id, session_id=session_id,
                                         tool=tool_name, error=str(e)[:200], elapsed_ms=elapsed_t)
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
                        yield emit("step",
                            text=f"{tool_name} 执行超时，建议：减少处理量或简化任务。",
                            status="警告")
                        _try_inject_skill()
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        # ── 熔断：同一工具连续失败跟踪（含异常/超时/返回 success:false）──
        _is_failure = _is_tool_error or result_str.startswith("工具执行错误:")
        if _is_failure:
            for t in list(_consecutive_tool_failures.keys()):
                if t != tool_name:
                    del _consecutive_tool_failures[t]
            _consecutive_tool_failures[tool_name] = _consecutive_tool_failures.get(tool_name, 0) + 1
            if _consecutive_tool_failures.get(tool_name, 0) >= MAX_CONSECUTIVE_FAILURES:
                guard_msg = (
                    "【系统指令 - 最高优先级】同一工具连续失败3次，任务已无法推进。"
                    "你必须立即输出 final_message 告知用户无法完成并说明原因（包含失败工具名和错误摘要），"
                    "严禁调用任何工具。"
                )
                messages.append({"role": "system", "content": guard_msg})
                collected_steps.append(
                    {"step": f"熔断: {tool_name} 连续失败{MAX_CONSECUTIVE_FAILURES}次", "status": "完成"})
                yield emit("step", text=f"熔断: {tool_name} 连续失败{MAX_CONSECUTIVE_FAILURES}次", status="完成")
                _consecutive_tool_failures[tool_name] = 0
                _failure_breaker_msg_injected = True
                continue
        else:
            _consecutive_tool_failures = {}

        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)

        # ── 截图结果自动收集：绕过 LLM 不可靠性，由系统直接注入 final ──
        if tool_name == "take_screenshot":
            try:
                _r = json.loads(result_str) if isinstance(result_str, str) else result_str
                if isinstance(_r, dict) and _r.get("success"):
                    _img_md = _r.get("img_md", "")
                    _fp = _r.get("file_path", "")
                    if _img_md:
                        _pending_images.append((_img_md, _fp))
            except Exception:
                pass

        if _is_tool_error:
            # ── 工业级鲁棒性：错误分类感知反馈 ──
            _fix_guides = {
                "dependency": "缺失依赖包，请先 pip install。",
                "logic": "代码逻辑错误，检查 API 调用方式和参数类型。",
                "syntax": "代码语法错误，检查引号、括号、缩进。",
                "io": "IO 错误，检查文件路径和权限。",
                "unknown": "请仔细阅读上方错误信息。",
            }
            extra_guide = _fix_guides.get(_last_error_category, _fix_guides["unknown"])

            tool_result_msg = (
                f"❌ 工具 {tool_name} 执行失败！返回结果：{result_str}\n\n"
                f"【系统诊断】{extra_guide}\n"
                f"不要用相同参数重复调用，否则会被熔断拦截。"
            )
        else:
            tool_result_msg = f"工具 {tool_name} 返回：{result_str}"
        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})
        append_session_message(session_id, "assistant", tool_call_msg,
                               trace_id=trace_id, user_id=user_id,
                               is_temporary=False, msg_type="tool_call")
        append_session_message(session_id, "user", tool_result_msg,
                               trace_id=trace_id, user_id=user_id,
                               is_temporary=False, msg_type="tool_result")
        get_logger().update_tool_result(
            session_id=session_id, session_name=session_name,
            turn=state.turn, child_turn=state.child_turn - 1,
            tool_result=result_str
        )

    loop_done = _inject_result_images("任务未完成，已保存进度。可以继续。")
    yield emit("final", text=loop_done, timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _save_ai_reply(loop_done)
