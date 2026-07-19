import json
from typing import Dict, Any, Optional, Callable


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
        return self.emit("final", text=text, timestamp=timestamp)
    
    def emit_tool_call(self, tool_name: str, arguments: Dict[str, Any] = None) -> str:
        return self.emit("tool_call", tool_name=tool_name)
    
    def emit_tool_result(self, tool_name: str, result: Any) -> str:
        if isinstance(result, dict):
            status = "success" if result.get("success") else "failed"
            err_msg = result.get("message") or result.get("error") or result.get("output") or ""
            if not isinstance(err_msg, str):
                err_msg = str(err_msg)
            err_msg = err_msg[:150]
        else:
            status = "success"
            err_msg = ""
        payload = {"tool_name": tool_name, "status": status}
        if err_msg:
            payload["error"] = err_msg
        return self.emit("tool_result", **payload)
    
    def emit_reasoning(self, text: str) -> str:
        return self.emit("reasoning", text=text)
    
    def emit_confirm(self, confirmation_token: str, title: str = "操作需要确认", 
                      command: str = "", details: dict = None, tool_name: str = "",
                      challenge_phrase: str = "") -> str:
        return self.emit(
            "confirm",
            confirmation_token=confirmation_token,
            title=title,
            command=command,
            details=details or {},
            tool_name=tool_name,
            challenge_phrase=challenge_phrase
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


sse_streamer = SSEStreamer()
streaming_formatter = StreamingFormatter()