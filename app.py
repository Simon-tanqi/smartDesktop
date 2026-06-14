from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import os, sys, json, uuid, time, datetime, re
from llm_client import ai_concat, parse_ai_json, normalize_path_component, get_available_models, _ollama_call_stream, _deepseek_call_stream, AI_CONFIG
from intent_router import classify_intent, extract_params
from tools import TOOL_REGISTRY, build_dynamic_system_prompt
from memory import (load_long_term, get_recent_compressed, log_session_turn, get_session_messages, append_session_message, set_active_session, get_active_session_id, list_sessions, add_chat_message, get_chat_messages, delete_session, get_user_id, generate_trace_id, log_operation, log_trace)


app = Flask(__name__,template_folder='web',static_folder='web/static')
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


def run_agent_with_steps(user_message, model_choice, session_id=None, trace_id="", user_id="", continue_turns=0):
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    # 提示词快照：内存累积，对话结束后一次性写入 JSON
    _trace_data = []
    _call_seq = [0]

    _default_model = model_choice["model"]
    _default_provider = model_choice["provider"]

    def llm(messages, model_name=None, provider=None):
        idx = _call_seq[0]
        _call_seq[0] += 1

        actual_model = model_name if model_name else _default_model
        actual_provider = provider if provider else _default_provider

        if idx == 0:
            label = "intent_classify"
        elif idx == 1:
            label = "param_extract"
        else:
            label = f"turn{idx - 2}"

        trace_entry = {
            "index": idx,
            "label": label,
            "model": f"{actual_provider}/{actual_model}",
            "messages": messages
        }
        _trace_data.append(trace_entry)

        result = ai_concat(messages=messages, model_name=actual_model, provider=actual_provider, trace_id=trace_id)
        if result and isinstance(result.get("message"), dict):
            trace_entry["response"] = result["message"].get("content", "")
        else:
            trace_entry["response"] = ""
        return result

    def _dump_prompt_trace():
        if not _trace_data:
            return
        try:
            uid = session_id if session_id else f"unknown_{trace_id[-12:]}"
            dump_dir = os.path.join(os.path.dirname(__file__), "logs", "prompts", "Tank", uid)
            os.makedirs(dump_dir, exist_ok=True)
            dump_path = os.path.join(dump_dir, "llm_full_prompt.json")
            dump_data = {}
            for item in _trace_data:
                key = f"{item['index']:02d}_{item['label']}"
                dump_data[key] = item
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 提示词日志写入失败: {e}")

    # 第一阶段：意图识别与参数提取
    t0 = time.perf_counter()
    intent_type = classify_intent(user_message, llm, model_name=model_name, provider=provider)
    elapsed_i = int((time.perf_counter() - t0) * 1000)
    log_trace(trace_id, "intent.classify", "intent", elapsed_i,
              session_id=session_id, status="success",
              input_summary=user_message[:200], output_summary=intent_type)

    t0 = time.perf_counter()
    params = extract_params(user_message, intent_type, llm, model_name=model_name, provider=provider) or {}
    elapsed_p = int((time.perf_counter() - t0) * 1000)
    log_trace(trace_id, "intent.extract_params", "intent", elapsed_p,
              session_id=session_id, status="success",
              input_summary=f"intent={intent_type} | {user_message[:150]}",
              output_summary=str(params)[:200])

    # 加载历史消息（短期记忆）
    history_messages = get_session_messages(session_id)
    cleaned_history = [m for m in history_messages if m["role"] != "system"]

    # ----- 构建 system prompt（包含长期记忆和压缩摘要）-----
    # 1. 基础系统提示
    from intent_router import PERSONA_MODES
    persona_mode = intent_type if intent_type in PERSONA_MODES else None
    base_system = build_dynamic_system_prompt(
        intent_type=intent_type,
        persona_mode=persona_mode,
    )

    # 2. 长期记忆（手动格式化，基于 load_long_term 返回的结构）
    long_term = load_long_term()
    memory_lines = []

    # 用户基本信息
    user_info = long_term.get("user_info", {})
    if user_info.get("name"):
        memory_lines.append(f"用户姓名：{user_info['name']}")
    if user_info.get("job_title"):
        memory_lines.append(f"职位：{user_info['job_title']}")
    if user_info.get("company"):
        memory_lines.append(f"公司：{user_info['company']}")

    # 核心知识/技能
    core_knowledge = long_term.get("core_knowledge", [])
    if core_knowledge:
        memory_lines.append(f"掌握的技能：{'; '.join(core_knowledge)}")

    # 项目经验（只展示最近3个）
    projects = long_term.get("projects", [])
    if projects:
        memory_lines.append(f"参与的项目：{'; '.join(projects[-3:])}")

    # 偏好
    preferences = long_term.get("preferences", [])
    if preferences:
        memory_lines.append(f"偏好：{'; '.join(preferences)}")

    # 一般事实（只展示最近3个）
    facts = long_term.get("facts", [])
    if facts:
        memory_lines.append(f"其他事实：{'; '.join(f for f in facts[-3:] if f)}")

    memory_str = "【长期记忆】\n" + "\n".join(memory_lines) + "\n" if memory_lines else ""

    # 3. 压缩摘要（历史对话摘要）
    compressed_str = get_recent_compressed(3)   # 返回带标题的字符串，可能为空
    if compressed_str:
        compressed_str = compressed_str + "\n"

    full_system = base_system + "\n" + memory_str + compressed_str
    # ----- 构建初始消息列表 -----
    messages = [{"role": "system", "content": full_system}] + cleaned_history
    # 将用户当前消息加入（注意：不在这里存储到数据库，只用于推理）
    messages.append({"role": "user", "content": user_message})

    # 日志记录（结构化，带 trace 上下文）
    log_session_turn("user", user_message, trace_id=trace_id, user_id=user_id, session_id=session_id)

    collected_steps = []
    MAX_TURNS = 15
    start_turn = continue_turns   # 保留断点续传逻辑（暂未使用）
    retry_used = False            # 纯文本重试标志（仅一次）

    for turn in range(start_turn, MAX_TURNS):
        if turn >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        res = llm(messages)
        if not res:
            _dump_prompt_trace()
            return collected_steps, "AI 请求失败"

        # --- 提取思考过程（在 JSON 解析之前） ---
        raw_content = res["message"]["content"]
        thinking_text = ""
        # 方式1：DeepSeek-R1 的 reasoning_content
        if res["message"].get("reasoning_content", "").strip():
            thinking_text = res["message"]["reasoning_content"].strip()
        # 方式2：<think> 标签（DeepSeek-R1 通过 Ollama 时的格式）
        if not thinking_text:
            think_matches = re.findall(r'<think>(.*?)</think>', raw_content, re.DOTALL)
            if think_matches:
                thinking_text = "\n".join(m.strip() for m in think_matches if m.strip())
        # 方式3：非 JSON 前置文本（普通模型没有 think 标签时）
        if not thinking_text and raw_content.strip():
            stripped = raw_content.strip()
            if not stripped.startswith("{"):
                # 取第一个 { 之前的内容作为思考
                brace_pos = stripped.find("{")
                if brace_pos > 20:
                    thinking_text = stripped[:brace_pos].strip()
                elif brace_pos == -1 and len(stripped) > 20:
                    thinking_text = stripped[:200]

        if thinking_text:
            short_think = thinking_text[:300] + ("..." if len(thinking_text) > 300 else "")
            # 避免重复：相同思考内容不重复添加
            existing_thinks = [s.get("step", "") for s in collected_steps if s.get("status") == "思考"]
            if short_think not in existing_thinks:
                collected_steps.append({"step": short_think, "status": "思考"})
        # --- 思考过程提取完毕 ---

        ai_content = parse_ai_json(res["message"]["content"])

        # 处理进度更新
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            progress_msg = ai_content["progress"]
            collected_steps.append({"step": progress_msg, "status": "进行中"})
            continue

        # 处理最终回复（final_message）
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]
            collected_steps.append({"step": "完成", "status": "完成", "detail": final_msg})
            _dump_prompt_trace()
            return collected_steps, final_msg

        # 纯文本回退：parse_ai_json 未能提取合法 JSON
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback = res["message"]["content"]

            # general_chat 意图：纯文本直接返回
            if intent_type == "general_chat":
                collected_steps.append({"step": "AI 回复", "status": "完成", "detail": fallback})
                _dump_prompt_trace()
                return collected_steps, fallback

            # 其他意图：仅重试一次，仍失败则作为 final_message 返回
            if not retry_used:
                retry_used = True
                messages.append({"role": "system", "content": (
                    "你的回复不符合格式要求。"
                    "必须严格输出 {\"tool_name\": \"...\", \"arguments\": {...}} 或 {\"final_message\": \"...\"}。"
                )})
                continue
            collected_steps.append({"step": "完成", "status": "完成", "detail": fallback})
            _dump_prompt_trace()
            return collected_steps, fallback

        # 处理工具调用
        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})

        if tool_name == "finished":
            collected_steps.append({"step": "任务结束", "status": "完成"})
            _dump_prompt_trace()
            return collected_steps, "任务已完成"

        # 执行工具
        if tool_name in TOOL_REGISTRY:
            path_params = TOOL_REGISTRY[tool_name].get("path_params", [])
            for p in path_params:
                if p in arguments:
                    arguments[p] = normalize_path_component(arguments[p])
            t_tool = time.perf_counter()
            try:
                result = TOOL_REGISTRY[tool_name]["function"](**arguments)
                elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                result_str = str(result)
                # 智能截断：读文件类工具保留更多上下文，避免 LLM 因信息不足盲目重试
                result_len = len(result_str)
                max_len = 4000 if tool_name in ("read_file", "search_content", "get_project_summary") else 2000
                if result_len > max_len:
                    if tool_name == "read_file":
                        result_str = result_str[:max_len] + (
                            f"\n\n⚠️ 结果已截断（完整长度 {result_len} 字符）。"
                            f"如需定位特定代码，请使用 search_content 先搜索关键词获取行号，"
                            f"再用 read_file(start_line=行号-5, end_line=行号+30) 局部读取。"
                            f"严禁继续通过缩小 read_file 范围来搜索——你只会反复读到文件开头区域。"
                        )
                    else:
                        result_str = result_str[:max_len] + f"... (已截断，完整长度 {result_len} 字符)"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
                log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                          session_id=session_id, status="success",
                          input_summary=str(arguments)[:200],
                          output_summary=result_str[:200])
            except Exception as e:
                elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                result_str = f"工具执行错误: {str(e)}"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": str(e)})
                log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                          session_id=session_id, status="error",
                          input_summary=str(arguments)[:200],
                          error=str(e)[:500])
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        # 将工具调用和结果加入到消息历史中（用于继续推理）
        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)
        tool_result_msg = f"工具 {tool_name} 返回：{result_str}"
        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})

    # 达到最大轮次，未完成
    _dump_prompt_trace()
    return collected_steps, "任务未完成，已保存进度。可以继续。"


def run_agent_stream(user_message, model_choice, session_id=None, trace_id="", user_id=""):
    """流式 ReAct 循环：逐步产出 SSE 事件，让前端实时展示思考过程"""
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    _trace_data = []
    _call_seq = [0]
    _default_model = model_name
    _default_provider = provider

    def llm_nonstream(messages, _model_name=None, _provider=None):
        """非流式调用，仅用于意图识别和参数提取"""
        idx = _call_seq[0]
        _call_seq[0] += 1
        actual_model = _model_name if _model_name else _default_model
        actual_provider = _provider if _provider else _default_provider
        label = "intent_classify" if idx == 0 else "param_extract"
        trace_entry = {"index": idx, "label": label, "model": f"{actual_provider}/{actual_model}", "messages": messages}
        _trace_data.append(trace_entry)
        result = ai_concat(messages=messages, model_name=actual_model, provider=actual_provider, trace_id=trace_id)
        trace_entry["response"] = result["message"].get("content", "") if result and result.get("message") else ""
        return result

    def llm_stream(messages):
        """流式调用，产出思考过程文本 chunk，最后产出完整结果 dict"""
        idx = _call_seq[0]
        _call_seq[0] += 1
        label = f"turn{idx - 2}"
        trace_entry = {"index": idx, "label": label, "model": f"{_default_provider}/{_default_model}", "messages": messages}
        _trace_data.append(trace_entry)

        if _default_provider == "ollama":
            stream_gen = _ollama_call_stream(messages, _default_model, trace_id=trace_id)
        else:
            stream_gen = _deepseek_call_stream(messages, _default_model,
                AI_CONFIG.get("api_key", ""), AI_CONFIG.get("base_url", "https://api.deepseek.com"),
                trace_id=trace_id)

        full_content = ""
        full_reasoning = ""
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
                yield None
                return

        trace_entry["response"] = full_content
        yield {"content": full_content, "reasoning_content": full_reasoning}

    def _dump_prompt_trace():
        if not _trace_data:
            return
        try:
            uid = session_id if session_id else f"unknown_{trace_id[-12:]}"
            dump_dir = os.path.join(os.path.dirname(__file__), "logs", "prompts", "Tank", uid)
            os.makedirs(dump_dir, exist_ok=True)
            dump_path = os.path.join(dump_dir, "llm_full_prompt.json")
            dump_data = {}
            for item in _trace_data:
                key = f"{item['index']:02d}_{item['label']}"
                dump_data[key] = item
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 提示词日志写入失败: {e}")

    def emit(event_type, **kwargs):
        payload = {"type": event_type}
        payload.update(kwargs)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # ===== Phase 0: 立即发送初始状态，确保前端第一时间有反馈 =====
    yield emit("step", text="开始分析问题...", status="进行中")

    # ===== Phase 1: 意图识别与参数提取（非流式）=====
    try:
        t0 = time.perf_counter()
        intent_type = classify_intent(user_message, llm_nonstream, model_name=model_name, provider=provider)
        elapsed_i = int((time.perf_counter() - t0) * 1000)
        log_trace(trace_id, "intent.classify", "intent", elapsed_i,
                  session_id=session_id, status="success",
                  input_summary=user_message[:200], output_summary=intent_type)

        t0 = time.perf_counter()
        params = extract_params(user_message, intent_type, llm_nonstream, model_name=model_name, provider=provider) or {}
        elapsed_p = int((time.perf_counter() - t0) * 1000)
        log_trace(trace_id, "intent.extract_params", "intent", elapsed_p,
                  session_id=session_id, status="success",
                  input_summary=f"intent={intent_type} | {user_message[:150]}",
                  output_summary=str(params)[:200])

        # ===== Phase 2: 构建 System Prompt =====
        history_messages = get_session_messages(session_id)
        cleaned_history = [m for m in history_messages if m["role"] != "system"]

        from intent_router import PERSONA_MODES
        persona_mode = intent_type if intent_type in PERSONA_MODES else None
        base_system = build_dynamic_system_prompt(intent_type=intent_type, persona_mode=persona_mode)

        long_term = load_long_term()
        memory_lines = []
        user_info = long_term.get("user_info", {})
        if user_info.get("name"): memory_lines.append(f"用户姓名：{user_info['name']}")
        if user_info.get("job_title"): memory_lines.append(f"职位：{user_info['job_title']}")
        if user_info.get("company"): memory_lines.append(f"公司：{user_info['company']}")
        core_knowledge = long_term.get("core_knowledge", [])
        if core_knowledge: memory_lines.append(f"掌握的技能：{'; '.join(core_knowledge)}")
        projects = long_term.get("projects", [])
        if projects: memory_lines.append(f"参与的项目：{'; '.join(projects[-3:])}")
        preferences = long_term.get("preferences", [])
        if preferences: memory_lines.append(f"偏好：{'; '.join(preferences)}")
        facts = long_term.get("facts", [])
        if facts: memory_lines.append(f"其他事实：{'; '.join(f for f in facts[-3:] if f)}")
        memory_str = "【长期记忆】\n" + "\n".join(memory_lines) + "\n" if memory_lines else ""
        compressed_str = get_recent_compressed(3)
        if compressed_str: compressed_str = compressed_str + "\n"
        full_system = base_system + "\n" + memory_str + compressed_str

        messages = [{"role": "system", "content": full_system}] + cleaned_history
        messages.append({"role": "user", "content": user_message})

        log_session_turn("user", user_message, trace_id=trace_id, user_id=user_id, session_id=session_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield emit("error", text=f"启动失败: {str(e)[:500]}")
        return

    # ===== Phase 3: ReAct 循环（流式）=====
    collected_steps = []
    MAX_TURNS = 15
    retry_used = False

    for turn in range(MAX_TURNS):
        if turn >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        # 流式 LLM 调用
        llm_gen = llm_stream(messages)
        last_result = None

        for item in llm_gen:
            if item is None:
                _dump_prompt_trace()
                yield emit("error", text="AI 请求失败")
                return
            if isinstance(item, str):
                yield emit("thinking", text=item)
            elif isinstance(item, dict):
                last_result = item

        if not last_result:
            _dump_prompt_trace()
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

        # progress 类型
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            progress_msg = ai_content["progress"]
            collected_steps.append({"step": progress_msg, "status": "进行中"})
            yield emit("step", text=progress_msg, status="进行中")
            continue

        # final_message
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]

            # 防幻觉门禁：final_message 中出现工具调用描述但本轮未实际执行工具
            tools_executed = any(s.get("status") == "完成" for s in collected_steps)
            if not tools_executed:
                # 检测 final_msg 中是否嵌入了工具调用 JSON
                has_embedded_tool = bool(re.search(
                    r'"tool_name"\s*:\s*"(write_file|read_file|replace_in_file|copy_file|move_file|scan_directory|search_content|execute_command)"',
                    final_msg
                ))
                # 检测 final_msg 中是否出现"写入/修改/创建文件"等操作完成描述但无工具记录
                action_keywords = r'(已写入|已修改|已创建|已复制|已移动|已删除|已保存|已生成|写入了|修改了|创建了|用\s*write_file|用\s*read_file)'
                has_action_claim = re.search(action_keywords, final_msg)
                if has_embedded_tool or has_action_claim:
                    messages.append({"role": "system", "content": (
                        "你在 final_message 中描述了文件操作但本轮并未实际执行任何工具。"
                        "禁止在 final_message 中输出工具调用 JSON 或假装已完成操作。"
                        "规则：先用 {\"tool_name\":\"...\",\"arguments\":{...}} 执行工具，"
                        "收到工具返回结果后再用 {\"final_message\":\"...\"} 总结。"
                    )})
                    continue

            _dump_prompt_trace()
            # 存储到记忆
            append_session_message(session_id, "assistant", final_msg, skip_compress=True,
                                   trace_id=trace_id, user_id=user_id)
            for step in collected_steps:
                step_text = step.get("step", "")
                if step.get("status") == "完成":
                    continue
                if step_text:
                    add_chat_message(session_id, "assistant", step_text, msg_type="think",
                                     trace_id=trace_id, user_id=user_id)
            add_chat_message(session_id, "assistant", final_msg, msg_type="chat",
                             trace_id=trace_id, user_id=user_id)
            yield emit("final", text=final_msg)
            return

        # 纯文本回退（仅重试一次）
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback = raw_content
            if intent_type == "general_chat":
                append_session_message(session_id, "assistant", fallback, skip_compress=True,
                                       trace_id=trace_id, user_id=user_id)
                add_chat_message(session_id, "assistant", fallback, msg_type="chat",
                                 trace_id=trace_id, user_id=user_id)
                _dump_prompt_trace()
                yield emit("final", text=fallback)
                return
            if not retry_used:
                retry_used = True
                messages.append({"role": "system", "content": (
                    "你的回复不符合格式要求。"
                    "必须严格输出 {\"tool_name\": \"...\", \"arguments\": {...}} 或 {\"final_message\": \"...\"}。"
                )})
                continue
            append_session_message(session_id, "assistant", fallback, skip_compress=True,
                                   trace_id=trace_id, user_id=user_id)
            add_chat_message(session_id, "assistant", fallback, msg_type="chat",
                             trace_id=trace_id, user_id=user_id)
            _dump_prompt_trace()
            yield emit("final", text=fallback)
            return

        # 工具调用
        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})

        if tool_name == "finished":
            _dump_prompt_trace()
            yield emit("final", text="任务已完成")
            return

        if tool_name in TOOL_REGISTRY:
            path_params = TOOL_REGISTRY[tool_name].get("path_params", [])
            for p in path_params:
                if p in arguments:
                    arguments[p] = normalize_path_component(arguments[p])
            t_tool = time.perf_counter()
            try:
                result = TOOL_REGISTRY[tool_name]["function"](**arguments)
                elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                result_str = str(result)
                result_len = len(result_str)
                max_len = 4000 if tool_name in ("read_file", "search_content", "get_project_summary") else 2000
                if result_len > max_len:
                    if tool_name == "read_file":
                        result_str = result_str[:max_len] + (
                            f"\n\n⚠️ 结果已截断（完整长度 {result_len} 字符）。"
                            f"如需定位特定代码，请使用 search_content 先搜索关键词获取行号，"
                            f"再用 read_file(start_line=行号-5, end_line=行号+30) 局部读取。"
                            f"严禁继续通过缩小 read_file 范围来搜索——你只会反复读到文件开头区域。"
                        )
                    else:
                        result_str = result_str[:max_len] + f"... (已截断，完整长度 {result_len} 字符)"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
                yield emit("step", text=f"执行 {tool_name}", status="完成")
                log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                          session_id=session_id, status="success",
                          input_summary=str(arguments)[:200],
                          output_summary=result_str[:200])
            except Exception as e:
                elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                result_str = f"工具执行错误: {str(e)}"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": str(e)})
                yield emit("step", text=f"执行 {tool_name} 失败", status="错误")
                log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                          session_id=session_id, status="error",
                          input_summary=str(arguments)[:200],
                          error=str(e)[:500])
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)
        tool_result_msg = f"工具 {tool_name} 返回：{result_str}"
        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})

    _dump_prompt_trace()
    yield emit("final", text="任务未完成，已保存进度。可以继续。")


# ---------- Flask 路由 ----------
# 1. 补充 /api/sessions 的 POST 方法（接收前端保存的会话）
@app.route('/api/sessions', methods=['POST'])
def save_session():
    try:
        data = request.json
        session_id = data.get("id")
        if session_id:
            # 只需将该会话设为活跃即可，memory 模块后续会自动创建消息存储
            set_active_session(session_id)
            # 可选：如果有标题，可单独存储（需扩展 memory 模块，暂忽略）
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

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
        return jsonify({"success": False, "message": str(e)}), 500

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
                "msgType": msg.get("msg_type", "chat")
            })
        return jsonify({"success": True, "messages": formatted})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/')
def index():
    # 因为 template_folder 已经指向 'web'，这里直接写文件名即可
    return render_template('index.html')

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

        # 存储用户消息到记忆
        append_session_message(session_id, "user", user_input, skip_compress=False,
                               trace_id=trace_id, user_id=user_id)
        add_chat_message(session_id, "user", user_input, trace_id=trace_id, user_id=user_id)

        model_choice = data.get("model_choice")
        if not model_choice or not isinstance(model_choice, dict):
            model_display = data.get("model", None)
            model_choice = get_model_choice(model_display)

        return Response(
            stream_with_context(run_agent_stream(
                user_message=user_input,
                model_choice=model_choice,
                session_id=session_id,
                trace_id=trace_id,
                user_id=user_id
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
        return jsonify({"success": False, "message": str(e)}), 500


# ---------- 启动 ----------
if __name__ == '__main__':
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)