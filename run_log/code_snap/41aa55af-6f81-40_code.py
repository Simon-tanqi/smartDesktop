def run_agent(user_input: str, model_choice: dict, session_id: str, continue_turns: int = 0):
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    save_ai_log(f"Agent启动 | 会话ID: {session_id} | 用户输入: {user_input} | 模型: {provider}/{model_name} | 继续轮次: {continue_turns}")

    def llm(messages, model_name=None, provider=None):
        actual_model = model_name if model_name else model_name
        actual_provider = provider if provider else provider
        return ai_concat(messages, model_name=actual_model, provider=actual_provider)

    # ========== 第一阶段：意图识别（仅首次非继续） ==========
    if continue_turns == 0:
        intent_type = classify_intent(user_input, llm, model_name=model_name, provider=provider)
        save_ai_log(f"意图分类结果: {intent_type}")  # 新增日志

        # 提取参数（对于 general_chat 或无法提取的意图，返回空字典）
        params = extract_params(user_input, intent_type, llm, model_name=model_name, provider=provider) or {}
        save_ai_log(f"参数提取结果: {json.dumps(params, ensure_ascii=False)}")  # 新增日志
    else:
        # 继续任务：跳过分类，直接从会话加载上下文
        intent_type = "continue"
        params = {}
        save_ai_log("跳过意图识别，继续任务")  # 新增日志

    # ========== 第二阶段：构建增强上下文 ==========
    # 长期记忆
    long_term = load_long_term()
    memory_str = ""
    if long_term.get("name"):
        memory_str += f"用户姓名：{long_term['name']}。"
    if long_term.get("hobbies"):
        memory_str += f"爱好：{', '.join(long_term['hobbies'])}。"
    if long_term.get("long_term_goal"):
        memory_str += f"长期目标：{long_term['long_term_goal']}。"
    if memory_str:
        memory_str = "【长期记忆】\n" + memory_str + "\n"

    # 压缩记忆（最近3天）
    compressed_str = get_recent_compressed(3)
    if compressed_str:
        compressed_str = compressed_str + "\n"

    # 构建用户输入部分
    if continue_turns == 0:
        planning_context = (
            f"{memory_str}"
            f"{compressed_str}"
            f"原始需求：{user_input}\n\n"
            f"意图类型：{intent_type} ({INTENT_REGISTRY.get(intent_type, '')})\n"
            f"提取的参数：{json.dumps(params, ensure_ascii=False) if params else '无'}\n\n"
            f"【重要】请完整理解用户的最终目标，不要只执行第一步。"
            f"如果需要多个步骤（例如先扫描再总结），请自动规划完整序列。"
            f"当所有步骤完成后，用 final_message 总结结果。"
        )
    else:
        planning_context = (
            f"继续完成任务。（从第 {continue_turns} 轮开始）\n"
            f"{memory_str}"
            f"{compressed_str}"
        )

    # 动态系统提示：通用 + 技能专用
    base_system = build_dynamic_system_prompt()
    skill_prompt = SKILL_PROMPTS.get(intent_type, "") if intent_type != "continue" else ""
    full_system = base_system + (skill_prompt or "")

    # 获取会话历史（去除旧的 system 消息）
    history_messages = get_session_messages(session_id)
    cleaned_history = [m for m in history_messages if m["role"] != "system"]

    # 组装初始 messages
    messages = [
        {"role": "system", "content": full_system},
        *cleaned_history,
        {"role": "user", "content": planning_context}
    ]

    # 记录用户输入到会话存储和日志
    if continue_turns == 0:
        append_session_message(session_id, "user", user_input)
    log_session_turn("user", user_input)

    # ========== ReAct 循环 ==========
    MAX_TURNS = 15
    start_turn = continue_turns if continue_turns > 0 else 0

    for turn in range(start_turn, MAX_TURNS):
        print(f"\n[第 {turn + 1} 轮思考]")

        # 接近上限提醒
        if turn >= MAX_TURNS - 2:
            messages.append({
                "role": "system",
                "content": "你即将达到思考轮次上限，请尽快完成当前阶段，并用 progress 或 final_message 汇报。"
            })

        # 调用大模型
        res = ai_concat(messages, model_name=model_name, provider=provider)
        save_ai_log(f"[执行的提示词] {messages}")
        if not res:
            return {"success": False, "message": "AI 请求失败"}

        ai_content = parse_ai_json(res["message"]["content"])
        print(f"AI 原始输出: {res['message']['content'][:200]}...")  # 调试用

        # 处理 progress（阶段性汇报，不终止循环）
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            progress_msg = ai_content["progress"]
            print(f"[系统] 进度: {progress_msg}")
            save_ai_log(f"[进度] {progress_msg}")
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            messages.append({"role": "assistant", "content": json.dumps(ai_content, ensure_ascii=False)})
            continue

        # 处理 final_message（任务完成）
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            save_ai_log(f"[最终回复] {final_msg}")
            return {"success": True, "message": final_msg}

        # 不是 JSON 或没有 tool_name，当作最终自然语言回复
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback_msg = res["message"]["content"]
            append_session_message(session_id, "assistant", fallback_msg)
            return {"success": True, "message": fallback_msg}

        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})

        # finished 信号
        if tool_name == "finished":
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            return {"success": True, "message": "任务已完成"}

        # 执行工具
        if tool_name in TOOL_REGISTRY:
            save_ai_log(f"调用工具: {tool_name} | 参数: {json.dumps(arguments, ensure_ascii=False)}")  # 新增

            # 路径自动规范化
            path_params = TOOL_REGISTRY[tool_name].get("path_params", [])
            for param in path_params:
                if param in arguments:
                    arguments[param] = normalize_path_component(arguments[param])

            try:
                result = TOOL_REGISTRY[tool_name]["function"](**arguments)
                save_ai_log(f"工具 {tool_name} 执行成功 | 结果: {json.dumps(result, ensure_ascii=False)}")  # 新增

            except Exception as e:
                result = f"工具执行错误: {str(e)}"
                save_ai_log(f"工具 {tool_name} 执行失败 | 异常: {str(e)}")  # 新增
        else:
            result = f"工具 '{tool_name}' 不存在"
            save_ai_log(f"工具不存在: {tool_name}")  # 新增

        # 保存工具调用和结果到会话
        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)
        tool_result_msg = f"工具 {tool_name} 返回：{json.dumps(result, ensure_ascii=False)}"
        append_session_message(session_id, "assistant", tool_call_msg)
        append_session_message(session_id, "user", tool_result_msg)

        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})

        # 每轮更新会话 messages 快照（用于崩溃恢复）
        save_session_messages(session_id, messages)
        update_last_turn(session_id, turn)

    # 达到上限
    save_ai_log(f"任务未完成 | 达到最大轮次({MAX_TURNS}) | 会话ID: {session_id}")  # 新增
    update_last_turn(session_id, MAX_TURNS)
    save_session_messages(session_id, messages)
    return {
        "success": False,
        "message": f"任务未完成，已保存进度（第 {MAX_TURNS} 轮）。输入“继续”可接着做。",
        "continue_possible": True
    }
