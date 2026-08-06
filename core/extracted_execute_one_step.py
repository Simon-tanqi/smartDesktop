def execute_one_step(engine, rs: RunState, state, tc: dict, step: int,
                      full_content: str, full_reasoning: str,
                      _tl, _sl, cancel_event, confirmation_handler,
                      user_message: str, remaining_steps
                      ) -> Generator[str, None, str]:
    """执行单个 tool_call 的完整流程。

    包含：参数解析、依赖验证、循环检测、安全确认、工具执行（主进程/子进程）、
    结果校验、成功/失败路径分发。

    可变状态（conversation_messages / tool_call_history / layered_cache /
    final_reply_saved）通过 rs(RunState) 承载。

    Returns:
        flow_control: "return"（终止 run）/ "continue"（跳过当前 tc）/ "proceed"（正常完成）
    """
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    layered_cache = rs.layered_cache
    final_reply_saved = rs.final_reply_saved

    try:
        tool_call_id = tc.get("id") or f"call_{uuid.uuid4().hex}"
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
            return "continue"

        get_logger().info(f"LLM决定调用工具: {tool_name}",
                      trace_id=engine.trace_id, session_id=engine.session_id)
        yield engine.streamer.emit_reasoning(f"决定调用工具: {tool_name}")
        
        dep_error = validate_tool_dependency(tool_name)
        if dep_error:
            yield engine.streamer.emit_step(f"工具依赖验证失败: {dep_error}", "error")
            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                args_preview += "..."
            _tool_call_history.append({
                "name": tool_name,
                "args": args_preview,
                "status": "failed",
                "result": f"依赖验证失败: {dep_error[:500]}..." if len(dep_error) > 500 else dep_error
            })
            step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", dep_error, step)
            append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
            append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
            conversation_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": json.dumps({"success": False, "message": f"依赖验证失败: {dep_error}"}, ensure_ascii=False)
            })
            return "continue"
        
        yield engine.streamer.emit_tool_call(tool_name, arguments)

        # ===== 三层递进式循环检测（ToolLoopDetector） =====
        # Layer 1: 精确签名去重（同工具+同参数 → 硬终止）
        # Layer 2: 同名工具频率（同工具不同参数 → 硬终止，搜索类豁免）
        # Layer 3: 全局步数上限（MAX_TURNS 兜底，在主循环 for step 中）
        try:
            engine._loop_detector.check(tool_name, arguments, step)
        except ToolLoopError as _loop_err:
            get_logger().warning(f"[熔断] {_loop_err}", trace_id=engine.trace_id, session_id=engine.session_id)
            yield from safe_final(self,state,
                f"⚠️ {_loop_err}\n\n"
                f"可能原因：工具返回结果不满足需求，或任务超出当前能力范围。\n"
                f"建议：请尝试更具体的描述，或确认是否需要安装额外工具。"
            )
            final_reply_saved = True
            return "return"

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
            
            tool_info = engine.tool_pipeline.get_tool(tool_name, engine.session_id)
            if tool_info:
                tool_func = tool_info["function"]
                timeout = tool_info["timeout"]
            
            if not tool_func:
                yield engine.streamer.emit_step(f"工具 '{tool_name}' 不存在", "error")
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                    args_preview += "..."
                _tool_call_history.append({
                    "name": tool_name,
                    "args": args_preview,
                    "status": "failed",
                    "result": f"工具不存在"
                })
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                    f"工具 '{tool_name}' 不存在，请检查是否已加载对应技能域", step)
                append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
                append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps({"success": False, "message": f"工具 '{tool_name}' 不存在"}, ensure_ascii=False)
                })
                # 幻觉工具拦截强化（P1-6）：未注册工具计入全局连续失败计数，
                # 复用 MAX_GLOBAL_FAILURES 熔断，防止模型持续虚构工具名空转。
                engine._breaker_state._global_consecutive_failures += 1
                return "continue"
            
            # 工具执行前检查取消信号
            if cancel_event is not None and cancel_event.is_set():
                get_logger().info(f"检测到取消信号，终止工具执行: {tool_name}",
                                  trace_id=engine.trace_id, session_id=engine.session_id)
                yield from safe_final(self,state, "任务已取消", outcome="cancelled")
                return "return"

            # ── 安全分级检查（P0/P1 需确认） ──
            risk_msg, challenge_phrase, risk_level = engine.tool_pipeline.check_tool_risk_level(tool_name, arguments)
            if risk_msg:
                yield engine.streamer.emit_reasoning(risk_msg)
                # ── 确认缓存：同一工具+同一关键参数 120s 内已确认过 → 直接放行，不再弹确认框 ──
                cache_key = f"{tool_name}:{json.dumps({k: v for k, v in sorted(arguments.items()) if k in ('file_path', 'path', 'command')}, sort_keys=True, ensure_ascii=False, default=str)}"
                _cached_at = engine._confirmed_cache.get(cache_key)
                if _cached_at is not None and (time.time() - _cached_at) < 120:
                    get_logger().info(f"confirm cache hit: {tool_name}, skip",
                                      trace_id=engine.trace_id, session_id=engine.session_id)
                elif confirmation_handler:
                    token = f"risk_confirm_{tool_name}_{int(__import__('time').time())}"
                    yield engine.streamer.emit_confirm(
                        token,
                        title=f"安全风险确认：{tool_name}（{risk_level}）",
                        command=risk_msg,
                        tool_name=tool_name,
                        details=arguments,
                        challenge_phrase=challenge_phrase,
                    )
                    confirm_result = engine.tool_pipeline.wait_for_confirmation(token, confirmation_handler,
                                                                               timeout=engine.CONFIRMATION_TIMEOUT)
                    confirm_result = confirm_result or {}

                    # ✅ 校验（对齐路径 B）
                    if challenge_phrase:
                        ok = bool(confirm_result.get("confirmed")) and \
                             (confirm_result.get("challenge_response", "").strip() == challenge_phrase)
                    else:
                        ok = bool(confirm_result.get("confirmed"))
                    if not ok:
                        yield engine.streamer.emit_step(f"用户拒绝/挑战校验失败 '{tool_name}'，已跳过", "warning")
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        _tool_call_history.append({
                            "name": tool_name, "args": args_preview,
                            "status": "rejected", "result": "用户拒绝/挑战校验失败"
                        })
                        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                            f"用户拒绝/挑战校验失败 '{tool_name}'", step)
                        append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
                        append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
                        conversation_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": json.dumps({"success": False, "message": "用户拒绝/挑战校验失败"}, ensure_ascii=False)
                        })
                        return "continue"

                    # ✅ 确认通过：按是否带挑战短语注入对应确认参数
                    # 注：已删除向 tools.system_tools._pending_confirmations 的预注册——
                    # system_tools 不存在该属性（AttributeError 被吞），且 execute_command
                    # 已从 Path A 移除，该预注册从不生效，属死代码。
                    if not challenge_phrase:
                        arguments["confirmed"] = True

                    # ✅ 确认通过：写入确认缓存（同工具+同关键参数 120s 内不再重复确认）
                    engine._confirmed_cache[cache_key] = time.time()

            # 注入 session_id 到 load_domain_skills 等需要会话上下文的工具
            if tool_name == "load_domain_skills":
                arguments["session_id"] = engine.session_id
            
            # 需要修改主进程状态的工具：直接在主进程执行（不走子进程）
            _IN_PROCESS_TOOLS = {"load_domain_skills", "ask_user_clarification", "web_download"}
            if tool_name in _IN_PROCESS_TOOLS:
                try:
                    import time as _vt
                    _vt0 = _vt.time()
                    raw_result = tool_func(**arguments)
                    _latency = (_vt.time() - _vt0) * 1000
                    validated = engine.tool_pipeline.execute_with_validation(
                        tool_name, arguments, raw_result, engine.trace_id, _latency
                    )
                    if isinstance(validated, dict) and validated.get("status") in ("low_quality", "empty"):
                        result_data = validated
                    else:
                        result_data = _safe_json_loads(raw_result) if isinstance(raw_result, str) else raw_result
                        if not isinstance(result_data, dict):
                            result_data = {"success": True, "result": raw_result}
                except Exception as _e:
                    result_data = {"success": False, "message": str(_e)}
                
                yield engine.streamer.emit_tool_result(tool_name, result_data)
                
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                _tool_call_history.append({"name": tool_name, "args": args_preview, "status": "success" if result_data.get("success") else "failed", "result": result_data.get("message", "")[:200]})
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
                append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
                # 记录 tool 结果到 conversation_messages
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps(result_data, ensure_ascii=False)
                })
                save_checkpoint(self,user_message, state, remaining_steps, step, tool_name, arguments, progress="完成" if result_data.get("success") else result_data.get("message", ""), messages_param=conversation_messages)
                return "continue"  # 跳过子进程路径，进入下一轮 LLM 调用

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
                                          trace_id=engine.trace_id, session_id=engine.session_id)
                        try:
                            exe_match = re.search(r'"?([^"\s]+\\\.exe)"?', cmd, re.IGNORECASE)
                            if exe_match:
                                exe_path = exe_match.group(1)
                                parent_dir = os.path.dirname(exe_path)
                                program_name = os.path.basename(parent_dir)
                                if not program_name or program_name.lower() in ["uninstall", "bin"]:
                                    program_name = re.sub(r'\.exe$', '', os.path.basename(exe_path), flags=re.IGNORECASE)
                                
                                get_uninstall_func = engine.tool_pipeline.get_tool("get_uninstall_string", engine.session_id)
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
                                                                  trace_id=engine.trace_id, session_id=engine.session_id)
                        except Exception as _auto_e:
                            get_logger().debug(f"自动获取卸载路径失败（不影响主流程）: {_auto_e}",
                                               trace_id=engine.trace_id, session_id=engine.session_id)
                    
                    if prevalidated:
                        arguments["prevalidated_uninstall_paths"] = prevalidated
                except Exception as _e:
                    get_logger().debug(f"注入预验证路径失败（不影响主流程）: {_e}",
                                       trace_id=engine.trace_id, session_id=engine.session_id)

            result_container = engine._manager.dict({"result": None, "error": None})

            worker_process = multiprocessing.Process(target=_tool_worker, args=(result_container, tool_func, arguments))
            worker_process.start()
            
            worker_process.join(timeout=timeout)
            
            if worker_process.is_alive():
                get_logger().warning(f"工具 '{tool_name}' 执行超时，正在终止进程", 
                                  trace_id=engine.trace_id, session_id=engine.session_id)
                worker_process.terminate()
                worker_process.join(timeout=5)
                if worker_process.is_alive():
                    get_logger().error(f"工具 '{tool_name}' 进程终止失败，尝试强制杀死", 
                                    trace_id=engine.trace_id, session_id=engine.session_id)
                    try:
                        worker_process.kill()
                        worker_process.join(timeout=2)
                    except Exception:
                        pass
                yield engine.streamer.emit_step(f"工具 '{tool_name}' 执行超时（{timeout}秒），已终止进程", "failed")
                _timeout_sig = tool_call_signature(tool_name, arguments)
                if _timeout_sig == engine._breaker_state._last_tool_call_signature:
                    engine._breaker_state._consecutive_fail_count += 1
                else:
                    engine._breaker_state._consecutive_fail_count = 1
                    engine._breaker_state._last_tool_call_signature = _timeout_sig
                engine._breaker_state._global_consecutive_failures += 1
                if engine._breaker_state._consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                    yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                    final_reply_saved = True
                    return "return"
                if engine._breaker_state._global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                    yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state._global_consecutive_failures} 次（跨工具），任务已终止。")
                    final_reply_saved = True
                    return "return"
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
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                    f"工具 '{tool_name}' 执行超时（{timeout}秒）", step)
                append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
                append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
                return "continue"

            if result_container["error"]:
                error_msg = translate_error(str(result_container["error"]))
                yield engine.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
                _error_sig = tool_call_signature(tool_name, arguments)
                if _error_sig == engine._breaker_state._last_tool_call_signature:
                    engine._breaker_state._consecutive_fail_count += 1
                else:
                    engine._breaker_state._consecutive_fail_count = 1
                    engine._breaker_state._last_tool_call_signature = _error_sig
                engine._breaker_state._global_consecutive_failures += 1
                if engine._breaker_state._consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                    yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                    final_reply_saved = True
                    return "return"
                if engine._breaker_state._global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                    yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state._global_consecutive_failures} 次（跨工具），任务已终止。")
                    final_reply_saved = True
                    return "return"
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
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", error_msg, step)
                append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
                append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
                save_checkpoint(self,user_message, state, remaining_steps, step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)
                return "continue"
            
            result = result_container["result"]
            # 工具执行已完成（子进程），对结果进行校验
            validated = engine.tool_pipeline.execute_with_validation(
                tool_name, arguments, result, engine.trace_id, latency_ms=0
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
                # ── 阶段方法：工具失败路径处理（_handle_tool_failure） ──
                rs.conversation_messages = conversation_messages
                rs.tool_call_history = _tool_call_history
                rs.final_reply_saved = final_reply_saved
                _flow, final_reply_saved = yield from handle_tool_failure(
                    rs, state, tool_name, arguments, validated, _validated_data,
                    step, full_content, tool_call_id, user_message, remaining_steps
                )
                conversation_messages = rs.conversation_messages
                _tool_call_history = rs.tool_call_history
                if _flow == "return":
                    return "return"
                if _flow == "continue":
                    return "continue"  # ← 跳过后续成功逻辑，进入下一轮

            # ── 阶段方法：工具成功路径处理（_handle_tool_success） ──
            rs.conversation_messages = conversation_messages
            rs.tool_call_history = _tool_call_history
            rs.layered_cache = layered_cache
            rs.final_reply_saved = final_reply_saved
            _flow, final_reply_saved = yield from handle_tool_success(
                rs, state, tool_name, arguments, result, None,
                step, full_content, full_reasoning, _tl, _sl,
                cancel_event, confirmation_handler, tool_func,
                tool_call_id, user_message, remaining_steps
            )
            conversation_messages = rs.conversation_messages
            _tool_call_history = rs.tool_call_history
            layered_cache = rs.layered_cache
            if _flow == "return":
                return "return"
            # _flow == "proceed": 正常完成，for 循环进入下一次迭代

        except Exception as e:
            error_msg = translate_error(str(e))
            yield engine.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
            # 回填工具执行失败结果到 LLM 调用日志
            get_logger().update_tool_result(
                session_id=engine.session_id,
                session_name=engine.session_name,
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
            
            _exc_sig = tool_call_signature(tool_name, arguments)
            if _exc_sig == engine._breaker_state._last_tool_call_signature:
                engine._breaker_state._consecutive_fail_count += 1
            else:
                engine._breaker_state._consecutive_fail_count = 1
                engine._breaker_state._last_tool_call_signature = _exc_sig
            engine._breaker_state._global_consecutive_failures += 1
            
            if engine._breaker_state._consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务已终止。")
                final_reply_saved = True
                return "return"
            
            if engine._breaker_state._global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state._global_consecutive_failures} 次（跨工具），任务已终止以防止无限循环。")
                final_reply_saved = True
                return "return"
            
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

            step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", error_msg, step)

            append_session_message(engine.session_id, "assistant", full_content, trace_id=engine.trace_id, msg_type="tool_call")
            append_session_message(engine.session_id, "tool", step_msg, trace_id=engine.trace_id, msg_type="tool_result", tool_call_id=tool_call_id)
            save_checkpoint(self,user_message, state, remaining_steps, step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)

        return "proceed"
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.layered_cache = layered_cache
        rs.final_reply_saved = final_reply_saved


