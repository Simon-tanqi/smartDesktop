"""llm_client.py 单元测试"""
import os
import json
import pytest
from llm_client import (
    _simple_encrypt, _simple_decrypt,
    parse_ai_json, normalize_path_component,
    get_available_models, SECRET_KEY
)


# ─── 配置加解密 ───────────────────────────────────

class TestConfigEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        original = '{"backend": "ollama", "model": "deepseek-r1:7b"}'
        encrypted = _simple_encrypt(original, SECRET_KEY)
        decrypted = _simple_decrypt(encrypted, SECRET_KEY)
        assert decrypted == original

    def test_encrypt_deterministic(self):
        text = "hello world"
        e1 = _simple_encrypt(text, SECRET_KEY)
        e2 = _simple_encrypt(text, SECRET_KEY)
        assert e1 == e2

    def test_decrypt_wrong_key_garbled(self):
        encrypted = _simple_encrypt("secret data", SECRET_KEY)
        wrong_key = "d3Jvbmcta2V5LWhlcmU="
        decrypted = _simple_decrypt(encrypted, wrong_key)
        # 错误密钥解密会得到乱码，不会抛异常（XOR特性）
        assert decrypted != "secret data"

    def test_encrypt_empty_string(self):
        encrypted = _simple_encrypt("", SECRET_KEY)
        decrypted = _simple_decrypt(encrypted, SECRET_KEY)
        assert decrypted == ""

    def test_encrypt_unicode(self):
        text = "中文测试 🎉"
        encrypted = _simple_encrypt(text, SECRET_KEY)
        decrypted = _simple_decrypt(encrypted, SECRET_KEY)
        assert decrypted == text


# ─── JSON 解析 ───────────────────────────────────

class TestParseAiJson:
    def test_parse_valid_json(self):
        content = '{"tool_name": "read_file", "arguments": {"file_path": "/tmp/test.py"}}'
        result = parse_ai_json(content)
        assert result["tool_name"] == "read_file"
        assert result["arguments"]["file_path"] == "/tmp/test.py"

    def test_parse_json_with_think_tags(self):
        content = '<think>让我想想</think>{"final_message": "答案是42"}'
        result = parse_ai_json(content)
        assert result["final_message"] == "答案是42"

    def test_parse_json_in_code_block(self):
        content = '```json\n{"tool_name": "scan_directory", "arguments": {"path": "/tmp"}}\n```'
        result = parse_ai_json(content)
        assert result["tool_name"] == "scan_directory"

    def test_parse_invalid_json_returns_empty_or_none(self):
        content = "这不是JSON，是纯文本回复"
        result = parse_ai_json(content)
        # json_repair 可能返回空字符串或 None，不会抛异常即可
        assert result is None or result == "" or isinstance(result, dict)

    def test_parse_final_message(self):
        content = '{"final_message": "任务已完成"}'
        result = parse_ai_json(content)
        assert result["final_message"] == "任务已完成"

    def test_parse_progress_message(self):
        content = '{"progress": "正在扫描文件..."}'
        result = parse_ai_json(content)
        assert result["progress"] == "正在扫描文件..."

    def test_parse_empty_content(self):
        result = parse_ai_json("")
        # 空字符串 json_repair 可能返回 "" 或 None
        assert result is None or result == "" or isinstance(result, dict)

    def test_parse_nested_json(self):
        content = '{"tool_name": "write_file", "arguments": {"file_path": "/tmp/a.py", "content": "import os"}}'
        result = parse_ai_json(content)
        assert result["arguments"]["content"] == "import os"


# ─── 路径规范化 ───────────────────────────────────

class TestNormalizePath:
    def test_chinese_drive_letter(self):
        assert normalize_path_component("D盘\\test\\file.py") == "D:\\test\\file.py"

    def test_forward_slash_to_backslash(self):
        assert normalize_path_component("D:/test/file.py") == "D:\\test\\file.py"

    def test_add_backslash_after_drive(self):
        assert normalize_path_component("D:test") == "D:\\test"

    def test_already_normalized(self):
        assert normalize_path_component("D:\\test\\file.py") == "D:\\test\\file.py"

    def test_chinese_drive_with_space(self):
        # 正则替换 "D盘 " → "D:"，空格保留在路径中
        result = normalize_path_component("D盘 测试")
        assert result.startswith("D:\\")

    def test_complex_path(self):
        result = normalize_path_component("D盘/My Documents/file.txt")
        assert result == "D:\\My Documents\\file.txt"


# ─── 模型列表 ───────────────────────────────────

class TestAvailableModels:
    def test_returns_list(self):
        models = get_available_models()
        assert isinstance(models, list)
        assert len(models) >= 2

    def test_model_structure(self):
        models = get_available_models()
        for m in models:
            assert "display" in m
            assert "provider" in m
            assert "model" in m

    def test_has_ollama(self):
        models = get_available_models()
        providers = [m["provider"] for m in models]
        assert "ollama" in providers

    def test_has_deepseek(self):
        models = get_available_models()
        providers = [m["provider"] for m in models]
        assert "deepseek" in providers
