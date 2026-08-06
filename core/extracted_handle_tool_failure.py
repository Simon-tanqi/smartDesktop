def handle_tool_failure(engine, rs: RunState, state, tool_name: str, arguments: dict,
                          validated, _validated_data, step: int, full_content: str,
                          tool_call_id: str, user_message: str, remaining_steps
                          ) -> Generator[str, None, tuple]:
    """工具执行失败路径处理（is_valid_success == False）。

    包含：失败分类、签名熔断、结果记录、自适应降级、熔断检查。

    可变状态（conversation_messages / tool_call_history / final_reply_saved）
    通过 rs(RunState) 承载。

    Returns:
        (flow_control, final_reply_saved)
        flow_control: "return"（熔断终止）/ "continue"（跳过成功逻辑，进入下一轮 for tc）
    """
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    final_reply_saved = rs.final_reply_saved

    try:
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
        _cur_sig = tool_call_signature(tool_name, arguments)
        _failure_type = classify_tool_failure(
            _validated_data if isinstance(_validated_data, dict) else {}
        )

        if _failure_type == "fatal":
            # 致命失败：不计数，直接标记结果让 LLM 换方案
            fail_msg += "\n[此工具当前不可用，请使用其他工具完成目标]"
        elif _failure_type == "file_not_found":
            # 文件不存在：不计入任何熔断计数，重置循环检测使 LLM 可换路径重试
            fail_msg += "\n[文件不存在，请检查路径或尝试其他位置]"
            engine._loop_detector._consecutive_exact_repeat = 0
            engine._loop_detector._last_signature = None
        elif _failure_type == "retryable" and _cur_sig == engine._breaker_state._last_tool_call_signature:
            engine._breaker_state._consecutive_fail_count += 1
        elif _failure_type == "empty_result" or _failure_type == "unknown":
            # 空结果/软失败不计入熔断计数
            pass
        else:
            # 同名同参数真失败才累计
            if _cur_sig == engine._breaker_state._last_tool_call_signature:
                engine._breaker_state._consecutive_fail_count += 1
            else:
                engine._breaker_state._consecutive_fail_count = 1
                engine._breaker_state._last_tool_call_signature = _cur_sig

        if _failure_type not in ("file_not_found", "empty_result", "unknown"):
            engine._breaker_state._global_consecutive_failures += 1

        get_logger().warning(
            f"[Step {step}] 工具 {tool_name} 失败/空结果 "
            f"(连续失败: {engine._breaker_state._consecutive_fail_count}, "
            f"全局: {engine._breaker_state._global_consecutive_failures})"
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
        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", fail_msg, step)
        append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
        append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)

        # ── 自适应降级：连续空搜索结果 → 注入回退提示 ──
        if tool_name == "web_search" and _failure_type == "empty_result":
            engine._consecutive_empty_search += 1
            if engine._consecutive_empty_search >= 2:
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
                    f"自适应降级: 连续 {engine._consecutive_empty_search} 次空搜索，已注入回退提示",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
        elif tool_name == "web_search":
            engine._consecutive_empty_search = 0  # 非空结果 → 重置

        # 熔断检查：只有同名同参数真失败才触发熔断
        if engine._breaker_state._consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
            yield from safe_final(self,state,
                f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。\n"
                f"失败原因：{fail_msg}\n请更换方案或提供更多上下文。"
            )
            final_reply_saved = True
            return ("return", final_reply_saved)

        if engine._breaker_state._global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
            yield from safe_final(self,state,
                f"连续 {engine.MAX_GLOBAL_FAILURES} 次工具调用失败，任务终止。"
            )
            final_reply_saved = True
            return ("return", final_reply_saved)

        return ("continue", final_reply_saved)
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.final_reply_saved = final_reply_saved


