"""intent_router.py 单元测试"""
import pytest
from intent_router import (
    classify_intent, extract_params,
    INTENT_REGISTRY, PERSONA_MODES, SKILL_PROMPTS
)


# ─── 意图注册表 ───────────────────────────────────

class TestIntentRegistry:
    def test_has_required_intents(self):
        required = ["file_manage", "code_generate", "general_chat", "db_query"]
        for intent in required:
            assert intent in INTENT_REGISTRY

    def test_persona_modes_subset_of_registry(self):
        for mode in PERSONA_MODES:
            assert mode in INTENT_REGISTRY

    def test_registry_values_are_descriptions(self):
        for k, v in INTENT_REGISTRY.items():
            assert isinstance(v, str)
            assert len(v) > 0

    def test_total_intent_count(self):
        assert len(INTENT_REGISTRY) >= 10


# ─── 意图分类（mock LLM） ──────────────────────

class MockLLM:
    def __init__(self, response_text):
        self.response_text = response_text
        self.call_count = 0

    def __call__(self, messages, model_name=None, provider=None):
        self.call_count += 1
        return {"message": {"content": self.response_text}}


class TestClassifyIntent:
    def test_classify_file_manage(self):
        llm = MockLLM("file_manage")
        result = classify_intent("把D盘的文件移动到E盘", llm)
        assert result == "file_manage"

    def test_classify_code_generate(self):
        llm = MockLLM("code_generate")
        result = classify_intent("帮我写一个冒泡排序", llm)
        assert result == "code_generate"

    def test_classify_general_chat(self):
        llm = MockLLM("general_chat")
        result = classify_intent("你好", llm)
        assert result == "general_chat"

    def test_classify_db_query(self):
        llm = MockLLM("db_query")
        result = classify_intent("查询用户表的所有数据", llm)
        assert result == "db_query"

    def test_classify_architect(self):
        llm = MockLLM("architect")
        result = classify_intent("帮我设计一个微服务架构", llm)
        assert result == "architect"

    def test_classify_returns_lowercase(self):
        llm = MockLLM("Code_Generate")
        result = classify_intent("写代码", llm)
        assert result == "code_generate"

    def test_llm_returns_unknown_fallback_to_general(self):
        llm = MockLLM("unknown_intent_type")
        result = classify_intent("随便说点什么", llm)
        assert result == "general_chat"

    def test_llm_returns_none_fallback(self):
        """classify_intent 中 res 为 None 时应降级为 general_chat"""
        class NoneLLM:
            def __call__(self, messages, model_name=None, provider=None):
                return None
        result = classify_intent("测试", NoneLLM())
        assert result == "general_chat"

    def test_classify_multiple_intents_picks_first(self):
        llm = MockLLM("file_manage and code_generate")
        result = classify_intent("帮我整理文件并写代码", llm)
        assert result == "file_manage"


# ─── 参数提取 ───────────────────────────────────

class MockLLMForParams:
    def __init__(self, response_text):
        self.response_text = response_text

    def __call__(self, messages, model_name=None, provider=None):
        return {"message": {"content": self.response_text}}


class TestExtractParams:
    def test_extract_file_manage_params(self):
        llm = MockLLMForParams('{"source": "D:\\\\test", "destination": "E:\\\\backup"}')
        params = extract_params("把D盘test移到E盘backup", "file_manage", llm)
        assert params["source"] == "D:\\test"
        assert params["destination"] == "E:\\backup"

    def test_extract_code_generate_params(self):
        llm = MockLLMForParams('{"language": "python", "task": "冒泡排序"}')
        params = extract_params("用Python写冒泡排序", "code_generate", llm)
        assert params["language"] == "python"

    def test_extract_general_chat_returns_empty(self):
        params = extract_params("你好", "general_chat", None)
        assert params == {}

    def test_extract_returns_empty_on_bad_json(self):
        llm = MockLLMForParams("这不是JSON")
        params = extract_params("测试", "file_manage", llm)
        assert params == {}

    def test_extract_handles_think_tags(self):
        llm = MockLLMForParams('<think>让我分析一下</think>{"source": "/tmp", "target": "/backup"}')
        params = extract_params("备份文件", "file_manage", llm)
        assert params.get("source") == "/tmp"

    def test_extract_returns_empty_for_none_response(self):
        """extract_params 中 res 为 None 时应返回空字典"""
        class NoneLLM:
            def __call__(self, messages, model_name=None, provider=None):
                return None
        params = extract_params("测试", "file_manage", NoneLLM())
        assert params == {}


# ─── SKILL_PROMPTS ───────────────────────────────

class TestSkillPrompts:
    def test_core_intents_have_skill_prompt(self):
        """核心操作意图必须有 SKILL_PROMPTS"""
        core_intents = ["file_manage", "code_generate", "db_query", "general_chat"]
        for intent in core_intents:
            assert intent in SKILL_PROMPTS, f"{intent} 缺少 SKILL_PROMPTS"

    def test_skill_prompts_are_strings_or_none(self):
        for k, v in SKILL_PROMPTS.items():
            assert v is None or isinstance(v, str)

    def test_code_generate_has_path_rules(self):
        prompt = SKILL_PROMPTS.get("code_generate", "")
        assert "get_workspace_path" in prompt
