def do_final_cleanup(engine, state, user_message: str, remaining_steps: list,
                      cancel_event, default_model: str, default_provider: str,
                      model_id: str, temperature) -> Generator[str, None, None]:
    """循环结束后的收尾：生成进度摘要、保存checkpoint、yield最终结果。

    从 run() 主循环后提取，通过 yield from 委托。
    """
    if cancel_event is not None and cancel_event.is_set():
        get_logger().info("检测到取消信号，跳过最终进度摘要生成",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        yield from safe_final(self,state, "任务已取消", outcome="cancelled")
        return
        
    if engine.completed_steps:
        steps_summary_lines = []
        for idx, step_data in enumerate(engine.completed_steps, 1):
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
            trace_id=engine.trace_id,
            model_id=model_id,
            temperature=temperature
        )
        progress_summary = progress_result["message"].get("content", "").strip()
    except Exception as e:
        progress_summary = f"任务执行到第{engine.MAX_TURNS}轮终止。已完成{len(engine.completed_steps)}个步骤。"
        
    save_checkpoint(self,user_message, state, remaining_steps, engine.MAX_TURNS, progress=progress_summary)
        
    yield from safe_final(self,state,
        f"[系统] 已达到最大推理步数 {engine.MAX_TURNS}，自动结束。"
        f"已完成的步骤见上文；若任务未完成，请缩小范围后重试。",
        outcome="max_steps")
    return


