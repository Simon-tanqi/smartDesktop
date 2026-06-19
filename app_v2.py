"""
app.py - AI Agent 主应用文件（v2 优化版）

架构概述：
    基于 Flask 的 ReAct Agent，支持 Ollama/DeepSeek 双模型、SSE 流式推送。
    请求流程：意图识别 → 参数提取 → 构建 Prompt → ReAct 循环 → 工具执行

v2 优化点：
    1. consecutive_progress 收到非 progress 响应时重置为 0（修复核心缺陷）
    2. 缓存清除仅移除写工具相关条目（提升读缓存命中率）
    3. llm_stream 添加异常捕获，向前端传递具体错误信息
    4. save_session 去重查询移到循环外（修复 N+1 查询性能问题）
    5. 截断长度记录原始值（修复 read_file 截断提示中长度显示错误）
"""

# ==================== 标准库导入 ====================
import os          # 文件路径操作
import json        # JSON 序列化/反序列化
import time        # 计时（perf_counter 精确计时）
import datetime    # 日期时间（时间戳格式化）

# Flask 框架
from flask import (
    Flask,              # Web 应用实例
    render_template,    # 渲染 HTML 模板
    request,            # 获取 HTTP 请求数据
    jsonify,            # Python 对象 → JSON 响应
    Response,           # 自定义响应（用于 SSE 流式输出）
    stream_with_context # 流式响应中保持请求上下文
)

# 线程池（工具函数异步执行 + 超时控制）
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

# ==================== 自定义模块导入 ====================
from llm_client import (
    ai_concat,              # 非流式 LLM 调用
    parse_ai_json,          # 从 LLM 输出解析 JSON
    normalize_path_component,# 路径规范化（防路径遍历）
    get_available_models,   # 获取可用模型列表
    _ollama_call_stream,    # Ollama 流式调用
    _deepseek_call_stream,  # DeepSeek 流式调用
    AI_CONFIG               # AI 配置常量
)
from intent_router import classify_intent, extract_params  # 意图分类 + 参数提取
from tools import TOOL_REGISTRY, build_dynamic_system_prompt  # 工具注册表 + Prompt 构建
from memory import (
    get_relevant_memory,    # 长期记忆检索
    log_session_turn,       # 记录会话轮次
    get_session_messages,   # 获取会话消息历史
    append_session_message, # 追加消息到会话
    set_active_session,     # 设置活跃会话
    get_active_session_id,  # 获取活跃会话 ID
    list_sessions,          # 列出所有会话
    add_chat_message,       # 写入聊天消息到 SQLite
    get_chat_messages,      # 读取聊天消息
    delete_session,         # 删除会话
    get_user_id,            # 获取用户 ID
    generate_trace_id,      # 生成追踪 ID
    log_operation,          # 操作日志
    log_trace               # 追踪日志（含耗时）
)

# ==================== Flask 应用初始化 ====================
app = Flask(__name__, template_folder='web', static_folder='web/static')
workspace_path = os.path.dirname(os.path.abspath(__file__))

# 启动时加载可用模型列表
available_models = get_available_models()
default_model = available_models[0] if available_models else {
    "provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"
}


# ==================== 模型选择 ====================

def get_model_choice(display_name=None):
    """根据显示名称查找模型配置，未匹配则返回默认模型"""
    for m in available_models:
        if m["display"] == display_name:
            return m
    return default_model


# ==================== 核心 Agent 引擎 ====================

def run_agent(user_message, model_choice, session_id=None, trace_id="", user_id="", streaming=True):
    """
    ReAct 循环主函数。

    流程:
        Phase 0: 发送初始状态 SSE
        Phase 1: 意图分类 + 参数提取（非流式 LLM）
        Phase 2: 构建 System Prompt + 加载历史 + 注入记忆
        Phase 3: 流式 ReAct 循环（最多 15 轮）
    """
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    # ---------- 内部状态 ----------
    _trace_data = []       # LLM 调用追踪（调试用）
    _call_seq = [0]        # 调用序号（列表以便闭包修改）
    _default_model = model_name
    _default_provider = provider

    # =====================================================================
    # llm_nonstream: 非流式 LLM 调用（意图识别 / 参数提取）
    # =====================================================================
    def llm_nonstream(messages, model_name=None, provider=None):
        """
        非流式调用，直接返回完整响应。
        用于 Phase 1 的意图分类和参数提取。
        """
        idx = _call_seq[0]
        _call_seq[0] += 1
        actual_model = model_name or _default_model
        actual_provider = provider or _default_provider

        # 记录追踪数据
        trace_entry = {
            "index": idx,
            "label": "intent_classify" if idx == 0 else "param_extract",
            "model": f"{actual_provider}/{actual_model}",
            "messages": messages
        }
        _trace_data.append(trace_entry)

        # 执行非流式调用
        result = ai_concat(
            messages=messages, model_name=actual_model,
            provider=actual_provider, trace_id=trace_id
        )

        # 提取响应文本
        trace_entry["response"] = (
            result["message"].get("content", "")
            if result and result.get("message") else ""
        )
        return result

    # =====================================================================
    # llm_stream: 流式 LLM 调用（generator）
    # =====================================================================
    def llm_stream(messages):
        """
        流式调用，产出推理文本 chunk 和最终结果 dict。

        Yields:
            str  - 推理过程文本片段（实时推送前端）
            dict - {"content": ..., "reasoning_content": ...} 或 {"type": "error", "message": ...}
        """
        idx = _call_seq[0]
        _call_seq[0] += 1

        trace_entry = {
            "index": idx,
            "label": f"turn{idx - 2}",
            "model": f"{_default_provider}/{_default_model}",
            "messages": messages
        }
        _trace_data.append(trace_entry)

        # 根据 provider 选择流式调用
        if _default_provider == "ollama":
            stream_gen = _ollama_call_stream(messages, _default_model, trace_id=trace_id)
        else:
            stream_gen = _deepseek_call_stream(
                messages, _default_model,
                AI_CONFIG.get("api_key", ""),
                AI_CONFIG.get("base_url", "https://api.deepseek.com"),
                trace_id=trace_id
            )

        full_content = ""
        full_reasoning = ""

        try:
            for chunk in stream_gen:
                if chunk.get("type") == "chunk":
                    # 中间 chunk：累积内容，实时 yield 推理文本
                    if chunk.get("content"):
                        full_content += chunk["content"]
                    if chunk.get("reasoning"):
                        full_reasoning += chunk["reasoning"]
                        yield chunk["reasoning"]
                elif chunk.get("type") == "result":
                    # 最终结果 chunk
                    full_content = chunk.get("content", full_content)
                    full_reasoning = chunk.get("reasoning_content", full_reasoning)
                elif chunk.get("type") == "error":
                    yield {"type": "error", "message": chunk.get("message", "未知错误")}
                    return
        except Exception as e:
            # 捕获流式调用异常，向前端传递错误信息
            yield {"type": "error", "message": f"流式调用异常: {str(e)}"}
            return

        trace_entry["response"] = full_content
        yield {"content": full_content, "reasoning_content": full_reasoning}

    # =====================================================================
    # _dump_prompt_trace: 导出 LLM 调用记录到 JSON（调试用）
    # =====================================================================
    def _dump_prompt_trace():
        """将本次请求的所有 LLM 调用记录导出到 logs/prompts/Tank/{uid}/llm_full_prompt.json"""
        if not _trace_data:
            return
        try:
            uid = session_id or f"unknown_{trace_id[-12:]}"
            dump_dir = os.path.join(os.path.dirname(__file__), "logs", "prompts", "Tank", uid)
            os.makedirs(dump_dir, exist_ok=True)
            dump_path = os.path.join(dump_dir, "llm_full_prompt.json")
            dump_data = {f"{i['index']:02d}_{i['label']}": i for i in _trace_data}
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 提示词日志写入失败: {e}")

    # =====================================================================
    # emit: 构造 SSE 事件字符串
    # =====================================================================
    def emit(event_type, **kwargs):
        """构造 SSE 格式: "data: {JSON}\\n\\n" """
        return f"data: {json.dumps({'type': event_type, **kwargs}, ensure_ascii=False)}\n\n"

    # =====================================================================
    # Phase 0: 初始状态反馈
    # =====================================================================
    yield emit("step", text="开始分析问题...", status="进行中")

    # =====================================================================
    # Phase 1 & 2: 意图识别 + 参数提取 + 构建 Prompt
    # =====================================================================
    try:
        # --- Phase 1a: 意图分类 ---
        t0 = time.perf_counter()
        intent_type = classify_intent(
            user_message, llm_nonstream,
            model_name=model_name, provider=provider
        )
        log_trace(trace_id, "intent.classify", "intent",
                  int((time.perf_counter() - t0) * 1000),
                  session_id=session_id, status="success",
                  input_summary=user_message[:200], output_summary=intent_type)

        # --- Phase 1b: 参数提取 ---
        t0 = time.perf_counter()
        params = extract_params(
            user_message, intent_type, llm_nonstream,
            model_name=model_name, provider=provider
        ) or {}
        log_trace(trace_id, "intent.extract_params", "intent",
                  int((time.perf_counter() - t0) * 1000),
                  session_id=session_id, status="success",
                  input_summary=f"intent={intent_type} | {user_message[:150]}",
                  output_summary=str(params)[:200])

        # --- Phase 2: 构建 System Prompt ---
        # 2.1 加载历史消息，过滤 system 角色
        history_messages = get_session_messages(session_id)
        cleaned_history = [m for m in history_messages if m["role"] != "system"]

        # 2.2 合并连续同角色消息（API 要求角色交替）
        if cleaned_history:
            merged = [dict(cleaned_history[0])]
            for m in cleaned_history[1:]:
                if merged[-1]["role"] == m["role"]:
                    merged[-1]["content"] += "\n" + m["content"]
                else:
                    merged.append(dict(m))
            cleaned_history = merged

        # 2.3 构建 System Prompt（含 persona + 长期记忆）
        from intent_router import PERSONA_MODES
        persona_mode = intent_type if intent_type in PERSONA_MODES else None
        base_system = build_dynamic_system_prompt(
            intent_type=intent_type, persona_mode=persona_mode
        )
        relevant_memory = get_relevant_memory(user_message)
        messages = [
            {"role": "system", "content": base_system + ("\n" + relevant_memory if relevant_memory else "")}
        ] + cleaned_history

        # 2.4 去重：避免重复追加当前用户消息
        if not cleaned_history or cleaned_history[-1]["content"].rstrip() != user_message.rstrip():
            messages.append({"role": "user", "content": user_message})

        log_session_turn("user", user_message, trace_id=trace_id, user_id=user_id, session_id=session_id)

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield emit("error", text=f"启动失败: {str(e)[:500]}")
        return

    # =====================================================================
    # Phase 3: ReAct 循环
    # =====================================================================
    collected_steps = []
    MAX_TURNS = 15               # 最大循环轮次
    MAX_CONSECUTIVE_PROGRESS = 3  # 连续 progress 上限
    retry_count = 0               # 格式重试计数
    consecutive_progress = 0      # 连续 progress 计数（收到非 progress 时重置）

    _cache = {}  # 工具调用缓存 {cache_key: result}
    _WRITE_TOOLS = {
        "execute_command", "modify_registry", "write_file", "edit_file",
        "replace_in_file", "move_file", "copy_file", "delete_file", "scan_directory"
    }

    def _is_retryable(exc):
        """判断异常是否可重试（网络/超时/权限类）"""
        import errno
        if isinstance(exc, (ConnectionError, TimeoutError, FutureTimeout, PermissionError)):
            return True
        if isinstance(exc, OSError):
            return exc.errno in (errno.EAGAIN, errno.EBUSY, errno.EINTR)
        return False

    def _execute_tool_with_retry(tool_name, arguments, timeout=60):
        """带重试的工具执行（最多 3 次，延迟递增 0.5/1.0/2.0s）"""
        last_exception = None
        for attempt, delay in enumerate([0.5, 1.0, 2.0]):
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    return executor.submit(
                        TOOL_REGISTRY[tool_name]["function"], **arguments
                    ).result(timeout=timeout)
            except FutureTimeout:
                raise TimeoutError(f"工具 {tool_name} 执行超时 ({timeout}s)")
            except Exception as e:
                if not _is_retryable(e):
                    raise
                last_exception = e
                if attempt < 2:
                    time.sleep(delay)
        raise last_exception

    # ---------- ReAct 主循环 ----------
    for turn in range(MAX_TURNS):
        # 接近上限时提醒 LLM 收尾
        if turn >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        # --- 流式调用 LLM ---
        last_result = None
        for item in llm_stream(messages):
            if isinstance(item, dict) and item.get("type") == "error":
                _dump_prompt_trace()
                yield emit("error", text=item.get("message", "AI 请求失败"))
                return
            if item is None:
                _dump_prompt_trace()
                yield emit("error", text="AI 请求失败")
                return
            if isinstance(item, str):
                yield emit("thinking", text=item)  # 实时推送推理过程
            elif isinstance(item, dict):
                last_result = item

        if not last_result:
            _dump_prompt_trace()
            yield emit("error", text="AI 请求失败")
            return

        raw_content = last_result["content"]
        final_reasoning = last_result.get("reasoning_content", "").strip()

        # --- 提取思考摘要（去重存档） ---
        if final_reasoning:
            short_think = final_reasoning[:300] + ("..." if len(final_reasoning) > 300 else "")
        elif raw_content.strip() and not raw_content.strip().startswith("{"):
            brace_pos = raw_content.strip().find("{")
            if brace_pos > 20:
                short_think = raw_content.strip()[:brace_pos].strip()
            elif brace_pos == -1 and len(raw_content.strip()) > 20:
                short_think = raw_content.strip()[:200]
            else:
                short_think = ""
        else:
            short_think = ""

        if short_think and short_think not in [
            s.get("step", "") for s in collected_steps if s.get("status") == "思考"
        ]:
            collected_steps.append({"step": short_think, "status": "思考"})

        # --- 解析 LLM 输出 ---
        ai_content = parse_ai_json(raw_content)

        # --- 处理 progress 响应 ---
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            consecutive_progress += 1
            progress_msg = ai_content["progress"]
            collected_steps.append({"step": progress_msg, "status": "进行中"})

            if consecutive_progress >= MAX_CONSECUTIVE_PROGRESS:
                _dump_prompt_trace()
                yield emit("step", text=progress_msg, status="进行中")
                yield emit("final",
                    text=f"任务执行中（连续 {consecutive_progress} 轮状态更新），已中止自动循环。可以发送新指令继续。",
                    timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                return

            yield emit("step", text=progress_msg, status="进行中")
            messages.append({"role": "assistant", "content": json.dumps(ai_content, ensure_ascii=False)})
            messages.append({"role": "user", "content": "进度已记录，继续下一步。"})
            continue
        else:
            # ===== 核心修复：非 progress 响应时重置计数器 =====
            consecutive_progress = 0

        # --- 处理 final_message ---
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            _dump_prompt_trace()
            yield emit("final", text=ai_content["final_message"],
                       timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return

        # --- 处理无效格式（渐进式重试） ---
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            if intent_type == "general_chat":
                _dump_prompt_trace()
                yield emit("final", text=raw_content,
                           timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
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
            _dump_prompt_trace()
            yield emit("final", text=raw_content,
                       timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return

        # --- 工具调用 ---
        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})

        if tool_name == "finished":
            _dump_prompt_trace()
            yield emit("final", text="任务已完成",
                       timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return

        if tool_name in TOOL_REGISTRY:
            tool_info = TOOL_REGISTRY[tool_name]

            # 路径参数规范化
            for p in tool_info.get("path_params", []):
                if p in arguments:
                    arguments[p] = normalize_path_component(arguments[p])

            # 缓存检查
            cache_key = (tool_name, json.dumps(arguments, sort_keys=True, default=str))
            if cache_key in _cache:
                result_str = str(_cache[cache_key])
                collected_steps.append({"step": f"执行 {tool_name} (缓存命中)", "status": "完成", "detail": result_str[:200]})
                yield emit("step", text=f"执行 {tool_name} (缓存命中)", status="完成")
            else:
                t_tool = time.perf_counter()
                try:
                    result = _execute_tool_with_retry(
                        tool_name, arguments, timeout=tool_info.get("timeout", 60)
                    )
                    elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                    result_str = str(result)
                    _cache[cache_key] = result

                    # ===== 优化：仅清除写工具相关缓存 =====
                    if tool_name in _WRITE_TOOLS:
                        _cache = {k: v for k, v in _cache.items() if k[0] not in _WRITE_TOOLS}

                    # ===== 修复：记录原始长度后再截断 =====
                    result_len = len(result_str)
                    max_len = 4000 if tool_name in ("read_file", "search_content", "get_project_summary") else 2000
                    if result_len > max_len:
                        if tool_name == "read_file":
                            result_str = result_str[:max_len] + (
                                f"\n\n⚠️ 结果已截断（完整长度 {result_len} 字符）。"
                                f"如需定位特定代码，请使用 search_content 先搜索关键词获取行号，"
                                f"再用 read_file(start_line=行号-5, end_line=行号+30) 局部读取。"
                            )
                        else:
                            result_str = result_str[:max_len] + f"... (已截断，完整长度 {result_len} 字符)"

                    collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
                    yield emit("step", text=f"执行 {tool_name}", status="完成")
                    log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                              session_id=session_id, status="success",
                              input_summary=str(arguments)[:200], output_summary=result_str[:200])
                except Exception as e:
                    elapsed_t = int((time.perf_counter() - t_tool) * 1000)
                    result_str = f"工具执行错误: {str(e)}"
                    collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": str(e)})
                    yield emit("step", text=f"执行 {tool_name} 失败", status="错误")
                    log_trace(trace_id, f"tool.{tool_name}", "tool", elapsed_t,
                              session_id=session_id, status="error",
                              input_summary=str(arguments)[:200], error=str(e)[:500])
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        # 将工具调用和结果追加到消息历史
        messages.append({"role": "assistant", "content": json.dumps(ai_content, ensure_ascii=False)})
        messages.append({"role": "user", "content": f"工具 {tool_name} 返回：{result_str}"})

    # 循环结束：超过最大轮次
    _dump_prompt_trace()
    yield emit("final", text="任务未完成，已保存进度。可以继续。",
               timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


# ==================== Flask 路由 ====================

@app.route('/api/sessions', methods=['POST'])
def save_session():
    """接收前端会话数据并同步到 SQLite（含去重 + 时间戳转换）"""
    try:
        data = request.json
        session_id = data.get("id")
        if session_id:
            set_active_session(session_id)
            # ===== 修复：去重查询移到循环外（避免 N+1 查询） =====
            existing = get_chat_messages(session_id)
            existing_keys = {(m["role"], m["content"][:80]) for m in existing}
            sender_to_role = {"user": "user", "ai": "assistant", "system": "system"}
            for msg in data.get("messages", []):
                role = sender_to_role.get(msg.get("sender", "ai"), "assistant")
                key = (role, (msg.get("text") or "")[:80])
                if key not in existing_keys:
                    ts = msg.get("timestamp", "")
                    if ts:
                        try:
                            from datetime import datetime, timezone, timedelta
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            ts = ts.replace("T", " ")[:19]
                    add_chat_message(session_id,
                        "user" if msg.get("sender") == "user" else "assistant",
                        msg.get("text", ""), msg_type=msg.get("msgType", "chat"), timestamp=ts)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    """获取所有会话列表"""
    try:
        return jsonify(list_sessions())
    except Exception as e:
        print(f"[API] 获取会话列表错误: {e}")
        return jsonify([])


@app.route('/api/models', methods=['GET'])
def get_models():
    """获取可用模型列表（含兜底默认模型）"""
    try:
        models = get_available_models()
        return jsonify(models) if models else jsonify([
            {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}
        ])
    except Exception as e:
        print(f"[API] 加载模型列表错误: {e}")
        return jsonify([
            {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}
        ])


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session_route(session_id):
    """删除指定会话"""
    try:
        delete_session(session_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/sessions/active', methods=['GET'])
def get_active_session():
    """获取当前活跃会话 ID（若无则自动创建）"""
    return jsonify({"session_id": get_active_session_id()})


@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    """获取指定会话的聊天消息"""
    try:
        messages = get_chat_messages(session_id)
        return jsonify({"success": True, "messages": [
            {"sender": "user" if m["role"] == "user" else "ai",
             "text": m["content"],
             "msgType": m.get("msg_type", "chat"),
             "timestamp": m.get("created_at", "")}
            for m in messages
        ]})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/')
def index():
    """渲染首页"""
    return render_template('index.html')


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """聊天主接口 - SSE 流式响应"""
    trace_id = generate_trace_id()
    user_id = get_user_id()
    try:
        data = request.json
        user_input = data.get("message", "").strip()
        session_id = data.get("session_id")
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"}), 400

        # 存储用户消息（含事实提取、压缩检查、SQLite 写入）
        append_session_message(session_id, "user", user_input,
                               skip_compress=False, trace_id=trace_id, user_id=user_id)

        # 解析模型选择
        model_choice = data.get("model_choice")
        if not model_choice or not isinstance(model_choice, dict):
            model_choice = get_model_choice(data.get("model", None))

        # 返回 SSE 流式响应
        return Response(
            stream_with_context(run_agent(
                user_message=user_input, model_choice=model_choice,
                session_id=session_id, trace_id=trace_id,
                user_id=user_id, streaming=True
            )),
            mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
        )
    except Exception as e:
        log_operation(trace_id, user_id,
                      session_id if 'session_id' in locals() else "",
                      "error", str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


# ==================== 启动 ====================

if __name__ == '__main__':
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)
