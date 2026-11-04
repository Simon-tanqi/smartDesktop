"""核心运行时模块包：导出对话状态、工具数据提供器与流式输出组件。

- ToolDataProvider：向工具层提供统一的数据访问入口
- SSEStreamer / StreamingFormatter：流式响应输出与格式化
- AgentTurnState 等状态类：通过 __getattr__ 惰性导入
- ReActEngine：通过 __getattr__ 惰性导入

惰性导入的原因：`core.agent_state` 顶层执行 `from tools import ...`，而 `tools`
又依赖 `core.tool_data_provider`。若包初始化时立即导入 agent_state，会形成
`tools → core → agent_state → tools` 的循环导入，导致 `import tools` 单独失败。
"""
from .tool_data_provider import ToolDataProvider, tool_data_provider
from .streaming import SSEStreamer, StreamingFormatter, sse_streamer, streaming_formatter

# 惰性导入：避免 tools → core.__init__ → agent_state → tools 循环导入
_ReActEngine = None
_LAZY_AGENT_STATE_ATTRS = {
    "AgentTurnState",
    "AgentExecutionState",
    "SessionDomainState",
    "GlobalStateManager",
    "global_state_manager",
}


def __getattr__(name):
    """模块级惰性属性访问：延迟加载 agent_state / react_engine，规避循环导入。"""
    global _ReActEngine
    if name == "ReActEngine":
        if _ReActEngine is None:
            from .react_engine import ReActEngine as _RE
            _ReActEngine = _RE
        return _ReActEngine
    if name in _LAZY_AGENT_STATE_ATTRS:
        from . import agent_state as _agent_state
        return getattr(_agent_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgentTurnState", "AgentExecutionState", "SessionDomainState",
    "GlobalStateManager", "global_state_manager",
    "ToolDataProvider", "tool_data_provider",
    "SSEStreamer", "StreamingFormatter", "sse_streamer", "streaming_formatter",
    "ReActEngine",
]
