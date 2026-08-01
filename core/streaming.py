import json
import time
import threading
import queue
from typing import Dict, Any, Optional, Callable, Iterator, Generator


class SSEStreamer:
    def __init__(self):
        self._callbacks = {}
        self._on_final: Optional[Callable] = None
    
    def on_event(self, event_type: str, callback: Callable):
        self._callbacks[event_type] = callback
    
    def set_on_final(self, callback: Callable):
        """注册 emit_final 回调：每次 emit_final 时触发，用于日志轮次完成等。"""
        self._on_final = callback
    
    def emit(self, event_type: str, **kwargs) -> str:
        payload: Dict[str, Any] = {"type": event_type}
        payload.update(kwargs)
        try:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except (TypeError, ValueError):
            payload_safe = {"type": event_type, "error": "无法序列化事件数据"}
            return f"data: {json.dumps(payload_safe, ensure_ascii=False)}\n\n"
    
    def emit_step(self, text: str, status: str = "进行中") -> str:
        return self.emit("step", text=text, status=status)
    
    def emit_error(self, text: str) -> str:
        return self.emit("error", text=text)
    
    def emit_final(self, text: str, timestamp: Optional[str] = None) -> str:
        if self._on_final:
            try:
                self._on_final(text, timestamp)
            except Exception:
                pass
        return self.emit("final", text=text, timestamp=timestamp, complete=True)
    
    def emit_tool_call(self, tool_name: str, arguments: Dict[str, Any] = None) -> str:
        return self.emit("tool_call", tool_name=tool_name)
    
    def emit_tool_result(self, tool_name: str, result: Any, args: Dict[str, Any] = None) -> str:
        if isinstance(result, dict):
            status = "success" if result.get("success") else "failed"
            err_msg = result.get("message") or result.get("error") or result.get("output") or ""
            if not isinstance(err_msg, str):
                err_msg = str(err_msg)
            err_msg = err_msg[:150]
        else:
            status = "success"
            err_msg = ""
        payload = {"tool_name": tool_name, "status": status, "args": args or {}}
        if err_msg:
            payload["error"] = err_msg
        return self.emit("tool_result", **payload)
    
    def emit_reasoning(self, text: str) -> str:
        return self.emit("reasoning", text=text)
    
    def emit_confirm(self, confirmation_token: str, title: str = "操作需要确认", 
                      command: str = "", details: dict = None, tool_name: str = "",
                      challenge_phrase: str = "", expires_at: Optional[float] = None) -> str:
        # P3: 确认请求带 5 分钟过期时间戳，前端可据此倒计时自动回传拒绝
        if expires_at is None:
            expires_at = time.time() + 300
        return self.emit(
            "confirm",
            confirmation_token=confirmation_token,
            title=title,
            command=command,
            details=details or {},
            tool_name=tool_name,
            challenge_phrase=challenge_phrase,
            expires_at=expires_at,
        )
    
    def broadcast(self, event_type: str, **kwargs):
        if event_type in self._callbacks:
            try:
                self._callbacks[event_type](**kwargs)
            except Exception:
                pass


class StreamingFormatter:
    @staticmethod
    def format_llm_chunk(chunk: Dict[str, Any]) -> Optional[str]:
        if chunk.get("type") == "chunk":
            if chunk.get("content"):
                return chunk["content"]
            if chunk.get("reasoning"):
                return chunk["reasoning"]
        elif chunk.get("type") == "error":
            err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
            return f"错误: {err_msg}"
        return None
    
    @staticmethod
    def format_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
        if result.get("success"):
            output = result.get("output", "")
            if result.get("is_gui_operation"):
                return f"✅ {tool_name}: GUI程序已启动，需要用户手动操作完成。"
            if not isinstance(output, str):
                output = str(output)
            if len(output) > 200:
                output = output[:200] + "..."
            return f"✅ {tool_name}: {output}"
        else:
            message = result.get("message", "操作失败")
            if not isinstance(message, str):
                message = str(message)
            return f"❌ {tool_name}: {message}"


def format_sse_event(event: Dict[str, Any]) -> Optional[str]:
    """将内部事件字典转换为 SSE 格式字符串。
    
    根据 event_type 分派到不同的 SSE 格式化逻辑，支持 reply / step / final 等事件类型。
    """
    event_type = event.get("type")
    if event_type == "reply":
        # L0 闲聊层回复 → final_answer 格式，确保前端能正常显示
        payload = {
            "type": "final_answer",
            "content": event["content"],
            "layer": event.get("layer", "L0"),
        }
    elif event_type == "step":
        payload = {"type": "step", "text": event.get("text", ""), "status": event.get("status", "进行中")}
    elif event_type == "error":
        payload = {"type": "error", "text": event.get("text", event.get("message", "未知错误"))}
    elif event_type == "final":
        payload = {"type": "final", "text": event.get("text", ""), "timestamp": event.get("timestamp")}
    elif event_type == "plan_created":
        payload = {
            "type": "plan_created",
            "plan_id": event.get("plan_id", ""),
            "steps": event.get("steps", []),
            "goal": event.get("goal", ""),
        }
    elif event_type == "plan_step_start":
        payload = {
            "type": "step_start",
            "index": event.get("index", 0),
            "description": event.get("description", ""),
        }
    elif event_type == "plan_step_done":
        payload = {
            "type": "step_done",
            "index": event.get("index", 0),
            "result": event.get("result", ""),
        }
    elif event_type == "plan_completed":
        payload = {
            "type": "plan_completed",
            "summary": event.get("summary", ""),
        }
    elif event_type == "plan_replanned":
        payload = {
            "type": "plan_replanned",
            "reason": event.get("reason", ""),
            "new_steps": event.get("new_steps", []),
        }
    elif event_type == "upgrade_l2_to_l3":
        payload = {
            "type": "upgrade_l2_to_l3",
            "reason": event.get("reason", ""),
            "target": event.get("target", "L3"),
        }
    elif event_type == "mode":
        payload = {
            "type": "mode",
            "mode": event.get("mode", ""),
        }
    else:
        return None
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── SSE 心跳生产者-消费者模式 ──
# BUG-X-2: 使用 threading + queue 模式，15 秒心跳防止反向代理切断长连接

def stream_response(event_iter: Iterator[str], heartbeat_interval: float = 15.0) -> Generator[str, None, None]:
    """将事件迭代器包装为带 SSE 心跳的生成器。
    
    生产者线程从 event_iter 读取事件并推入队列；
    消费者以 heartbeat_interval 超时从队列取事件，超时时发送 SSE 心跳注释。
    None 哨兵表示生产者结束，消费者退出。
    
    Args:
        event_iter: SSE 事件字符串迭代器（每项为 "data: {...}\\n\\n"）
        heartbeat_interval: 心跳间隔秒数，默认 15 秒
    """
    event_queue: queue.Queue = queue.Queue(maxsize=100)

    def _producer():
        try:
            for event in event_iter:
                while True:
                    try:
                        event_queue.put(event, block=True, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            event_queue.put(None)  # 哨兵

    producer_thread = threading.Thread(target=_producer, daemon=True, name="sse-producer")
    producer_thread.start()

    try:
        while True:
            try:
                item = event_queue.get(timeout=heartbeat_interval)
            except queue.Empty:
                # 超时：发送 SSE 心跳注释，防止 Nginx/负载均衡器切断长连接
                yield ": heartbeat\n\n"
                continue

            if item is None:
                break
            yield item
    finally:
        # 确保生产者线程终止
        event_queue.put(None)


sse_streamer = SSEStreamer()
streaming_formatter = StreamingFormatter()