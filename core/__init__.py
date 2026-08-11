"""核心运行时模块包：导出对话状态、工具数据提供器与流式输出组件。

- AgentTurnState / AgentExecutionState / SessionDomainState / GlobalStateManager：会话与执行状态管理
- ToolDataProvider：向工具层提供统一的数据访问入口
- SSEStreamer / StreamingFormatter：流式响应输出与格式化
- ReActEngine：通过 __getattr__ 惰性导入，规避 tools → core → react_engine 循环依赖
"""
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
    """模块级惰性属性访问：延迟加载 ReActEngine，避免循环导入"""
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