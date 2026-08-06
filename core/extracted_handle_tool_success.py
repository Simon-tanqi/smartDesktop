def handle_tool_success(engine, rs: RunState, state, tool_name: str, arguments: dict,
                          result, result_data, step: int, full_content: str,
                          full_reasoning: str, _tl, _sl, cancel_event,
                          confirmation_handler, tool_func, tool_call_id: str,
                          user_message: str, remaining_steps
                          ) -> Generator[str, None, tuple]:
    """工具执行成功路径处理。

    包含：重置熔断、结果解析、confirmation_required 处理、成功记录、进展评估。

    可变状态（conversation_messages / tool_call_history / layered_cache /
    final_reply_saved）通过 rs(RunState) 承载。

    Returns:
        (flow_control, final_reply_saved)
        flow_control: "return"（GUI/FUTILE/确认失败等终止）/"proceed"（正常完成，for tc 下一次）
    """
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    layered_cache = rs.layered_cache
    final_reply_saved = rs.final_reply_saved

    try:
        # ===== 以下是真正的成功路径 =====
        # 重置熔断计数：只要一次调用成功就清零
        engine._breaker_state._consecutive_fail_count = 0
        engine._breaker_state._last_tool_call_signature = ""

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
                      trace_id=engine.trace_id, session_id=engine.session_id)

        get_logger().debug(f"confirmation_required={result_data.get('confirmation_required')}, confirmation_handler={confirmation_handler}",
                      trace_id=engine.trace_id, session_id=engine.session_id)
        if result_data.get("confirmation_required"):
            get_logger().debug(f"confirmation_required is True", trace_id=engine.trace_id, session_id=engine.session_id)
            get_logger().debug(f"confirmation_handler: {confirmation_handler}", trace_id=engine.trace_id, session_id=engine.session_id)
            get_logger().debug(f"confirmation_token: {result_data.get('confirmation_token')}", trace_id=engine.trace_id, session_id=engine.session_id)
            if confirmation_handler:
                token = result_data.get("confirmation_token") or result_data.get("challenge_token")
                challenge_phrase = result_data.get("challenge_phrase")

                if token:
                    get_logger().debug(f"Got token, emitting confirm event", trace_id=engine.trace_id, session_id=engine.session_id)
                    title = result_data.get("message", "操作需要确认")
                    command = arguments.get("command", "")
                    details = result_data.get("details", {})
                    yield engine.streamer.emit_confirm(token, title=title, command=command,
                                                      details=details, tool_name=tool_name,
                                                      challenge_phrase=challenge_phrase)
                    get_logger().debug(f"Waiting for confirmation...", trace_id=engine.trace_id, session_id=engine.session_id)
                    confirmation_result = engine.tool_pipeline.wait_for_confirmation(token, confirmation_handler, timeout=engine.CONFIRMATION_TIMEOUT)
                    get_logger().debug(f"Confirmation result: {confirmation_result}", trace_id=engine.trace_id, session_id=engine.session_id)
                    if confirmation_result.get("confirmed"):
                        if cancel_event is not None and cancel_event.is_set():
                            yield from safe_final(self,state, "任务已取消", outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

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
                            yield engine.streamer.emit_step(f"工具执行失败: {e}", "failed")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                        result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                        yield engine.streamer.emit_tool_result(tool_name, result_data)
                        if result_data.get("is_gui_operation"):
                            yield from safe_final(self,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                        if not result_data.get("success"):
                            yield from safe_final(self,state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                    else:
                        if confirmation_result.get("timeout"):
                            yield from safe_final(self,state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                        else:
                            yield from safe_final(self,state, "用户拒绝了操作，任务已取消", outcome="cancelled")
                        final_reply_saved = True
                        return ("return", final_reply_saved)
                elif challenge_phrase:
                    challenge_token = result_data.get("challenge_token")
                    title = result_data.get("message", "高危操作需要确认")
                    command = arguments.get("command", "")
                    details = {"挑战短语": challenge_phrase}
                    yield engine.streamer.emit_confirm(challenge_token, title=title, command=command,
                                                      details=details, tool_name=tool_name,
                                                      challenge_phrase=challenge_phrase)
                    confirmation_result = engine.tool_pipeline.wait_for_confirmation(challenge_token, confirmation_handler, timeout=engine.CONFIRMATION_TIMEOUT)
                    if confirmation_result.get("confirmed") and confirmation_result.get("challenge_response"):
                        if cancel_event is not None and cancel_event.is_set():
                            yield from safe_final(self,state, "任务已取消", outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        confirmed_args = {**arguments, "challenge_token": challenge_token, "challenge_response": confirmation_result["challenge_response"]}
                        # 注：不再预注册 challenge token——execute_command 工具侧已在返回
                        # confirmation_required 前自行 create_challenge 存入 confirmation_manager，
                        # 引擎此处重复预注册到 _st._pending_confirmations（属性不存在）从不生效。
                        try:
                            confirm_result = tool_func(**confirmed_args)
                        except Exception as e:
                            yield engine.streamer.emit_step(f"工具执行失败: {e}", "failed")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                        result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                        yield engine.streamer.emit_tool_result(tool_name, result_data)
                        if result_data.get("is_gui_operation"):
                            yield from safe_final(self,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                        if not result_data.get("success"):
                            yield from safe_final(self,state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                    else:
                        if confirmation_result.get("timeout"):
                            yield from safe_final(self,state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                        else:
                            yield from safe_final(self,state, "用户拒绝了操作或挑战短语验证失败，任务已取消", outcome="cancelled")
                        final_reply_saved = True
                        return ("return", final_reply_saved)
                else:
                    yield engine.streamer.emit_tool_result(tool_name, result_data)
            else:
                yield engine.streamer.emit_error("安全确认机制未配置，高危操作已被拒绝，请联系管理员启用确认功能。")
                final_reply_saved = True
                return ("return", final_reply_saved)
        else:
            get_logger().info(f"工具 '{tool_name}' 执行成功，发送工具结果事件",
                          trace_id=engine.trace_id, session_id=engine.session_id)
            yield engine.streamer.emit_tool_result(tool_name, result_data)

        engine.completed_steps.append({
            "step": step,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result_data,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        get_logger().info(f"步骤已记录，completed_steps数量: {len(engine.completed_steps)}",
                      trace_id=engine.trace_id, session_id=engine.session_id)

        # 重置熔断计数：工具执行成功
        engine._breaker_state._consecutive_fail_count = 0
        engine._breaker_state._last_tool_call_signature = ""
        engine._breaker_state._global_consecutive_failures = 0

        # 回填工具执行结果到 LLM 调用日志
        _tool_result_str = json.dumps(result_data, ensure_ascii=False) if not isinstance(result_data, str) else result_data
        get_logger().update_tool_result(
            session_id=engine.session_id,
            session_name=engine.session_name,
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
        result_preview = _truncate_result(_tool_result_str, max_length=engine.MAX_RESULT_CHARS)
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

        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, _tool_result_str, "success", "", step)

        if result_data.get("is_gui_operation"):
            yield from safe_final(self,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
            final_reply_saved = True
            return ("return", final_reply_saved)

        append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
        append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)

        save_checkpoint(self,user_message, state, remaining_steps, step, tool_name, arguments, messages_param=conversation_messages)

        # ── Progress 进展评估 ──
        if PROGRESS_EVALUATOR_ENABLED:
            verdict, reason = engine.progress.decide(
                tool_name, arguments, result_data,
                changed=result_data.get("changed"),
                polling=result_data.get("polling"))
        else:
            verdict, reason = Verdict.PROGRESS, "feature_disabled"

        if verdict == Verdict.PROGRESS:
            pass  # 有用功，照常继续
        elif verdict == Verdict.STALLED:
            conversation_messages = strip_temp_system_messages(conversation_messages)
            conversation_messages.append({"role":"system","content":
                f"[进展控制器] {reason}。上一步结果摘要：{str(result_data)[:200]}。"
                f"禁止重复上一步；请更换参数/工具，或直接给出最终答复。"})
            yield engine.streamer.emit_step(f"进展停滞：{reason}", "warning")
        elif verdict == Verdict.FUTILE:
            final = engine.tool_pipeline.build_futile_final(tool_name, arguments, reason, result_data)
            yield from safe_final(self,state, final)
            return ("return", final_reply_saved)

        return ("proceed", final_reply_saved)
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.layered_cache = layered_cache
        rs.final_reply_saved = final_reply_saved


