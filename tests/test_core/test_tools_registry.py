# -*- coding: utf-8 -*-
"""tools/__init__.py 工具注册中心单元测试。

覆盖：TOOL_REGISTRY 注册、域加载状态、schema 构建、动态提示词拼接。
不触发 Windows 专属依赖（tools 各子模块顶层导入均为标准库 + 跨平台包）。
"""
import json

import pytest

import tools as tools_pkg
from tools import (
    TOOL_REGISTRY,
    add_domain_to_session,
    build_dynamic_system_prompt,
    build_tools_param,
    clear_session_domains,
    get_session_domains,
    get_skill_prompt,
    match_domains,
    match_persona,
    path_params,
    register_tool,
    remove_domain_from_session,
    tool_load_schema,
    _ensure_tool_registration,
    _find_tool_in_registry,
)


class TestRegisterTool:
    def test_direct_decoration_registers(self):
        @register_tool
        def _ut_direct_tool():
            """direct tool doc"""
            return "ok"

        try:
            entry = TOOL_REGISTRY["_ut_direct_tool"]
            assert entry["description"] == "direct tool doc"
            assert entry["timeout"] == 60
        finally:
            TOOL_REGISTRY.pop("_ut_direct_tool", None)

    def test_with_args_decoration_registers_timeout(self):
        @register_tool(timeout=120)
        def _ut_arg_tool():
            """arg tool doc"""
            return "ok"

        try:
            assert TOOL_REGISTRY["_ut_arg_tool"]["timeout"] == 120
        finally:
            TOOL_REGISTRY.pop("_ut_arg_tool", None)

    def test_register_existing_builtin(self):
        assert "scan_directory" in TOOL_REGISTRY
        assert "web_search" in TOOL_REGISTRY

    def test_path_params_marker(self):
        @path_params("target_path", "dest")
        def _ut_path_tool(target_path, dest):
            """path tool"""
            return (target_path, dest)

        assert getattr(_ut_path_tool, "_path_params", None) == ["target_path", "dest"]


class TestSessionDomains:
    def test_add_and_get(self):
        sid = "ut-session-domains-1"
        clear_session_domains(sid)
        add_domain_to_session(sid, "excel_analyze")
        assert get_session_domains(sid) == {"excel_analyze"}

    def test_add_multiple_and_get_copy(self):
        sid = "ut-session-domains-2"
        clear_session_domains(sid)
        add_domain_to_session(sid, "a")
        add_domain_to_session(sid, "b")
        got = get_session_domains(sid)
        got.add("c")  # 修改副本不影响内部状态
        assert get_session_domains(sid) == {"a", "b"}

    def test_remove_domain(self):
        sid = "ut-session-domains-3"
        clear_session_domains(sid)
        add_domain_to_session(sid, "a")
        add_domain_to_session(sid, "b")
        remove_domain_from_session(sid, "a")
        assert get_session_domains(sid) == {"b"}

    def test_unknown_session_returns_empty(self):
        assert get_session_domains("ut-no-such-session") == set()


class TestRegistrationState:
    def test_all_registered_after_init(self):
        _ensure_tool_registration()
        assert all(not info.get("_needs_registration") for info in TOOL_REGISTRY.values())

    def test_find_tool_in_registry_schema(self):
        tool = _find_tool_in_registry("scan_directory")
        assert tool is not None
        assert tool.get("desc")
        # schema 按 key 查找返回、本身不含 name 字段；校验关键字段齐全
        assert tool.get("function")
        assert tool.get("tier") is not None

    def test_tool_load_schema_returns_json(self):
        raw = tool_load_schema("ut-session-schema", "scan_directory")
        schema = json.loads(raw)
        assert isinstance(schema, dict)
        assert len(schema) > 0

    def test_tool_load_schema_unknown(self):
        raw = tool_load_schema("ut-session-schema", "ut_no_such_tool_xyz")
        assert "未找到" in raw


class TestDomainMatching:
    def test_match_domains_returns_list(self):
        result = match_domains("帮我做一个Excel表格")
        assert isinstance(result, list)
        assert any("excel" in d for d in result)

    def test_match_domains_plain_text(self):
        result = match_domains("今天天气怎么样")
        assert isinstance(result, list)

    def test_match_persona(self):
        result = match_persona("你好")
        assert isinstance(result, str) or result is None


class TestSkillPrompt:
    def test_office_word_skill_exists(self):
        text = get_skill_prompt("office_word")
        assert isinstance(text, str) and len(text) > 0

    def test_unknown_skill_empty(self):
        assert get_skill_prompt("ut_no_such_skill_xyz") == ""


class TestSchemaBuild:
    def test_build_tools_param_nonempty(self):
        params = build_tools_param()
        assert isinstance(params, list)
        assert len(params) > 0
        assert "type" in params[0]

    def test_build_dynamic_system_prompt_contains_core_and_tools(self):
        prompt = build_dynamic_system_prompt([])
        assert isinstance(prompt, str)
        assert len(prompt) > 100
        # 核心提示词与工具清单均被拼入
        assert "工具" in prompt or "工具清单" in prompt

    def test_build_dynamic_system_prompt_with_domain(self):
        prompt = build_dynamic_system_prompt(["excel_analyze"])
        assert len(prompt) > 100
