from .agent_state import (
    AgentTurnState, AgentExecutionState, SessionDomainState,
    GlobalStateManager, global_state_manager
)
from .tool_data_provider import ToolDataProvider, tool_data_provider
from .streaming import SSEStreamer, StreamingFormatter, sse_streamer, streaming_formatter

# 惰性导入 ReActEngine：避免 tools → core.__init__ → react_engine → tools 循环导入
# 使用时通过 from core.react_engine import ReActEngine 导入
_ReActEngine = None

def __getattr__(name):
    global _ReActEngine
    if name == "ReActEngine":
        if _ReActEngine is None:
            from .react_engine import ReActEngine as _RE
            _ReActEngine = _RE
        return _ReActEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgentTurnState", "AgentExecutionState", "SessionDomainState",
    "GlobalStateManager", "global_state_manager",
    "ToolDataProvider", "tool_data_provider",
    "SSEStreamer", "StreamingFormatter", "sse_streamer", "streaming_formatter",
    "ReActEngine",
]