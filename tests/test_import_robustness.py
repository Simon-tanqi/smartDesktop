# -*- coding: utf-8 -*-
"""包级导入健壮性测试：防止 tools ↔ core 循环导入回归。

历史缺陷：import tools / from tools import TOOL_REGISTRY 直接 ImportError。
根因：tools/__init__.py 顶层导入 core.tool_data_provider → core/__init__.py
顶层导入 agent_state → agent_state 顶层 from tools import ...（tools 尚未加载完）。
修复：core/__init__.py 全部顶层导出惰性化（__getattr__），打破循环。
"""
import importlib


def test_import_tools_package():
    """tools 包必须可直接导入"""
    import tools
    assert tools is not None


def test_from_tools_import_registry():
    """from tools import TOOL_REGISTRY 必须可用（routing/chat 依赖）"""
    from tools import TOOL_REGISTRY
    assert isinstance(TOOL_REGISTRY, dict)


def test_import_tools_skill_fixation():
    """tools.skill_fixation 必须可直接导入"""
    import tools.skill_fixation
    assert tools.skill_fixation is not None


def test_import_core_agent_state():
    """core.agent_state 必须可直接导入"""
    import core.agent_state
    assert core.agent_state is not None


def test_import_core_react_engine():
    """core.react_engine 必须可直接导入"""
    import core.react_engine
    assert core.react_engine is not None


def test_import_backend_chat():
    """backend.chat 服务入口必须可导入"""
    import backend.chat
    assert backend.chat is not None


def test_core_lazy_exports():
    """core 惰性顶层导出必须可用"""
    from core import (ToolDataProvider, ReActEngine, global_state_manager,
                      AgentTurnState, SSEStreamer, sse_streamer)
    assert ToolDataProvider is not None
    assert ReActEngine is not None
    assert global_state_manager is not None
    assert AgentTurnState is not None
    assert SSEStreamer is not None
    assert sse_streamer is not None


def test_core_unknown_attr_raises():
    """core 未知属性必须抛 AttributeError"""
    import core
    try:
        core.nonexistent_attr_xyz
    except AttributeError:
        pass
    else:
        raise AssertionError("core 未知属性未抛 AttributeError")
