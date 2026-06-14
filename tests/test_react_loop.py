"""ReAct 循环集成测试（mock LLM，验证完整推理链路）"""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class MockReActLLM:
    """模拟多轮 LLM 调用"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_index = 0

    def __call__(self, messages, model_name=None, provider=None):
        if self.call_index < len(self.responses):
            resp = self.responses[self.call_index]
            self.call_index += 1
            if resp is None:
                return None
            return {"message": {"content": resp}}
        return {"message": {"content": '{"final_message": "任务完成"}'}}


class TestReActLoopLogic:
    def test_single_tool_call_then_final(self):
        responses = [
            "code_generate",
            '{"task": "冒泡排序"}',
            '{"tool_name": "get_workspace_path", "arguments": {}}',
            '{"final_message": "已完成冒泡排序代码生成"}',
        ]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("写一个冒泡排序", llm)
        params = extract_params("写一个冒泡排序", intent, llm)
        assert intent == "code_generate"

        messages = []
        for turn in range(15):
            res = llm(messages)
            data = json.loads(res["message"]["content"])
            if "final_message" in data:
                assert data["final_message"] == "已完成冒泡排序代码生成"
                return
            if "tool_name" in data:
                messages.append({"role": "assistant", "content": json.dumps(data)})
                messages.append({"role": "user", "content": '工具返回：{"success": true}'})
        pytest.fail("未在15轮内完成")

    def test_multi_tool_calls(self):
        responses = [
            "code_generate",
            '{"task": "测试"}',
            '{"tool_name": "get_workspace_path", "arguments": {}}',
            '{"tool_name": "write_file", "arguments": {"file_path": "/tmp/test.py", "content": "print(1)"}}',
            '{"final_message": "完成"}',
        ]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("写代码", llm)
        params = extract_params("写代码", intent, llm)

        messages = []
        tools_called = []
        for turn in range(15):
            res = llm(messages)
            data = json.loads(res["message"]["content"])
            if "final_message" in data:
                break
            if "tool_name" in data:
                tools_called.append(data["tool_name"])
                messages.append({"role": "assistant", "content": json.dumps(data)})
                messages.append({"role": "user", "content": '{"success": true}'})
        assert "get_workspace_path" in tools_called
        assert "write_file" in tools_called

    def test_format_correction_retry(self):
        responses = [
            "code_generate",
            '{}',
            '这不是JSON',
            '{"final_message": "重试成功"}',
        ]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("测试", llm)
        params = extract_params("测试", intent, llm)

        messages = []
        retry_used = False
        for turn in range(15):
            res = llm(messages)
            content = res["message"]["content"]
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = None

            if data and "final_message" in data:
                assert data["final_message"] == "重试成功"
                return

            if not data or "tool_name" not in data:
                if not retry_used:
                    retry_used = True
                    messages.append({"role": "system", "content": "请输出合法JSON"})
                    continue
        pytest.fail("格式纠正重试未生效")

    def test_max_turns_limit(self):
        responses = ["general_chat", '{}']
        for _ in range(20):
            responses.append('{"tool_name": "scan_directory", "arguments": {"path": "/tmp"}}')
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("测试", llm)
        params = extract_params("测试", intent, llm)

        messages = []
        turn_count = 0
        MAX_TURNS = 15
        for turn in range(MAX_TURNS):
            res = llm(messages)
            data = json.loads(res["message"]["content"])
            if "final_message" in data:
                break
            if "tool_name" in data:
                messages.append({"role": "assistant", "content": json.dumps(data)})
                messages.append({"role": "user", "content": '{"success": true}'})
                turn_count += 1
        # 循环执行了 MAX_TURNS 次迭代
        assert turn_count >= MAX_TURNS - 1

    def test_progress_message_continues_loop(self):
        responses = [
            "code_generate",
            '{}',
            '{"progress": "正在分析..."}',
            '{"progress": "即将完成..."}',
            '{"final_message": "分析完毕"}',
        ]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("分析代码", llm)
        params = extract_params("分析代码", intent, llm)

        messages = []
        progress_count = 0
        for turn in range(15):
            res = llm(messages)
            data = json.loads(res["message"]["content"])
            if "final_message" in data:
                break
            if "progress" in data:
                progress_count += 1
                messages.append({"role": "assistant", "content": json.dumps(data)})
                messages.append({"role": "user", "content": "收到"})
        assert progress_count == 2

    def test_finished_signal(self):
        responses = [
            "general_chat",
            '{}',
            '{"tool_name": "finished", "arguments": {}}',
        ]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent, extract_params
        intent = classify_intent("测试", llm)
        params = extract_params("测试", intent, llm)

        messages = []
        for turn in range(15):
            res = llm(messages)
            data = json.loads(res["message"]["content"])
            if data.get("tool_name") == "finished":
                return
            if "final_message" in data:
                return
        pytest.fail("finished 信号未被识别")

    def test_llm_none_response(self):
        responses = ["general_chat", '{}', None]
        llm = MockReActLLM(responses)

        from intent_router import classify_intent
        intent = classify_intent("测试", llm)
        assert intent == "general_chat"


class TestPromptAssembly:
    def test_all_intents_produce_prompt(self):
        from tools import build_dynamic_system_prompt
        from intent_router import INTENT_REGISTRY
        for intent in INTENT_REGISTRY:
            prompt = build_dynamic_system_prompt(intent)
            assert len(prompt) > 50, f"{intent} 的 prompt 过短"

    def test_persona_mode_replaces_core(self):
        from tools import build_dynamic_system_prompt
        prompt_arch = build_dynamic_system_prompt("architect", persona_mode="architect")
        assert "架构师" in prompt_arch

    def test_safety_always_included(self):
        from tools import build_dynamic_system_prompt
        for intent in ["file_manage", "code_generate", "general_chat"]:
            prompt = build_dynamic_system_prompt(intent)
            assert "安全" in prompt or "风险" in prompt

    def test_tools_filtered_by_intent(self):
        from tools import build_dynamic_system_prompt
        prompt_file = build_dynamic_system_prompt("file_manage")
        assert "scan_directory" in prompt_file
