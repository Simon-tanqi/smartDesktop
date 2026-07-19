"""
Agent 调度器：负责协调各核心模块，简化主入口。
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable, Generator, Dict, Any
import threading
import json

from core.react_engine import ReActEngine
from core.agent_state import TaskCheckpoint
from logger import get_logger


def run_agent(
    user_message: str,
    model_choice: Dict[str, Any],
    session_id: Optional[str] = None,
    trace_id: str = "",
    user_id: str = "",
    streaming: bool = True,
    session_name: str = "",
    tool_executor: Optional[ThreadPoolExecutor] = None,
    default_model: Optional[Dict[str, Any]] = None,
    confirmation_handler: Optional[Callable] = None,
    planning_extra: str = "",
    on_success: Optional[Callable] = None,
    resumed_checkpoint: Optional[TaskCheckpoint] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Generator[str, None, None]:
    """Agent 调度入口：协调各核心模块完成对话任务。

    参数:
        user_message: 用户原始输入文本
        model_choice: 模型配置字典，包含 provider/model/temperature/base_url 等
        session_id: 会话ID，用于记忆隔离
        trace_id: 链路追踪ID
        user_id: 用户ID（预留，供引擎获取用户配置）
        streaming: 是否流式输出（当前始终流式，预留开关）
        session_name: 会话名称
        tool_executor: 外部注入的线程池实例，不传则不创建（引擎内部工具调用为同步模式）
        default_model: 默认模型回退配置（预留）
        confirmation_handler: callable(token, timeout) → {"confirmed": bool}，用于高风险操作确认
        planning_extra: 规划提示词附加内容
        on_success: callable(user_message, tool_name, params) 成功回调，任务完成后触发
        resumed_checkpoint: TaskCheckpoint 实例，用于恢复中断的任务
        cancel_event: 可选的取消信号，设置后引擎在下一个检查点终止任务

    生成器 yield 的 event 类型（SSE 格式字符串）:
        - step: 执行步骤进度，{"type": "step", "text": "...", "status": "进行中|完成|error"}
        - reasoning: 思考过程，{"type": "reasoning", "text": "..."}
        - tool_call: 工具调用通知，{"type": "tool_call", "tool_name": "...", "arguments": {...}}
        - tool_result: 工具执行结果，{"type": "tool_result", "tool_name": "...", "result": {...}}
        - confirm: 需要用户确认，{"type": "confirm", "token": "...", "title": "...", ...}
        - final: 最终回复，{"type": "final", "text": "...", "timestamp": "..."}
        - error: 错误信息，{"type": "error", "text": "..."}
        - warning: 警告信息，{"type": "warning", "text": "..."}
    """
    logger = get_logger()
    _local_executor_created = False

    # 线程池策略：优先使用外部注入的；未注入时不自动创建（引擎内部工具调用为同步模式）
    if tool_executor is None:
        tool_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-")
        _local_executor_created = True

    engine = ReActEngine(trace_id=trace_id, session_id=session_id, session_name=session_name)

    last_tool_call = {"tool_name": "", "params": None}
    # v2 workflow: 累积本次运行中所有 tool_call 事件（按顺序）
    tool_call_chain: list[dict] = []
    # 是否流中出现过 error 事件
    run_had_error = False
    # 是否走到了 final 事件
    run_had_final = False

    try:
        for event in engine.run(
            user_message,
            model_choice,
            confirmation_handler=confirmation_handler,
            planning_extra=planning_extra,
            resumed_checkpoint=resumed_checkpoint,
            cancel_event=cancel_event,
        ):
            # 解析工具调用事件，记录最后一次成功的工具调用
            try:
                if isinstance(event, str) and event.startswith("data:"):
                    event_json = event[5:].strip()
                    event_data = json.loads(event_json)
                    ev_type = event_data.get("type")
                    if ev_type == "tool_call":
                        last_tool_call["tool_name"] = event_data.get("tool_name", "")
                        last_tool_call["params"] = event_data.get("arguments", None)
                        tool_call_chain.append({
                            "tool": last_tool_call["tool_name"],
                            "params": last_tool_call["params"],
                        })
                    elif ev_type == "error":
                        run_had_error = True
                    elif ev_type == "final":
                        run_had_final = True
            except Exception:
                pass

            # 在每次 yield 之间检查取消信号，使运行中的任务能尽快终止
            if cancel_event is not None and cancel_event.is_set():
                logger.info("任务已被取消", trace_id=trace_id, session_id=session_id)
                yield f'data: {json.dumps({"type": "final", "text": "任务已取消"}, ensure_ascii=False)}\n\n'
                run_had_final = True
                return
            yield event

        # 任务正常完成，触发成功回调
        # v2: 传入整条 tool_call 链 + 是否完整完成
        if on_success:
            try:
                run_was_complete = run_had_final and not run_had_error
                try:
                    on_success(
                        user_message,
                        last_tool_call["tool_name"],
                        last_tool_call["params"],
                        tool_call_chain=tool_call_chain,
                        run_was_complete=run_was_complete,
                    )
                except TypeError:
                    # 旧版 on_success 不接受新参数（可能由老代码透传）
                    on_success(user_message, last_tool_call["tool_name"], last_tool_call["params"])
            except Exception as e:
                logger.warning(f"on_success 回调执行失败: {e}", trace_id=trace_id, session_id=session_id)

    except GeneratorExit:
        # 调用方提前终止迭代（客户端断开等）
        logger.info("生成器被调用方提前终止", trace_id=trace_id, session_id=session_id)
        raise

    except Exception as e:
        # 异常兜底：记录日志并 yield 错误事件
        import traceback
        logger.error(f"Agent 执行异常: {e}\n{traceback.format_exc()}", trace_id=trace_id, session_id=session_id)
        try:
            yield f'data: {{"type": "error", "text": "任务执行异常，请重试。"}}\n\n'
        except Exception:
            pass

    finally:
        # 资源清理：仅关闭本函数内部创建的线程池
        if _local_executor_created:
            tool_executor.shutdown(wait=False)
