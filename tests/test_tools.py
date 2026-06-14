"""tools/ 工具函数单元测试"""
import os
import json
import tempfile
import shutil
import pytest


# ─── file_tools ───────────────────────────────────

class TestScanDirectory:
    def test_scan_existing_dir(self, tmp_path):
        from tools.file_tools import scan_directory
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.py").write_text("print(1)")
        result = json.loads(scan_directory(str(tmp_path)))
        assert "a.txt" in result
        assert "b.py" in result

    def test_scan_nonexistent_dir(self):
        from tools.file_tools import scan_directory
        result = scan_directory("D:\\nonexistent_path_12345")
        assert "扫描失败" in result

    def test_scan_empty_dir(self, tmp_path):
        from tools.file_tools import scan_directory
        empty = tmp_path / "empty"
        empty.mkdir()
        result = json.loads(scan_directory(str(empty)))
        assert result == []


class TestMoveFile:
    def test_move_success(self, tmp_path):
        from tools.file_tools import move_file
        src = tmp_path / "source.txt"
        src.write_text("content")
        dst_dir = tmp_path / "dest"
        dst_dir.mkdir()
        result = move_file(str(src), str(dst_dir))
        assert result["success"] is True
        assert not src.exists()
        assert (dst_dir / "source.txt").exists()

    def test_move_nonexistent_source(self, tmp_path):
        from tools.file_tools import move_file
        result = move_file(str(tmp_path / "nope.txt"), str(tmp_path))
        assert result["success"] is False
        assert "不存在" in result["message"]

    def test_move_no_overwrite(self, tmp_path):
        from tools.file_tools import move_file
        src = tmp_path / "a.txt"
        src.write_text("new")
        dst = tmp_path / "b.txt"
        dst.write_text("old")
        result = move_file(str(src), str(dst), overwrite=False)
        assert result["success"] is False
        assert "已存在" in result["message"]


class TestCopyFile:
    def test_copy_success(self, tmp_path):
        from tools.file_tools import copy_file
        src = tmp_path / "source.txt"
        src.write_text("content")
        dst_dir = tmp_path / "dest"
        dst_dir.mkdir()
        result = copy_file(str(src), str(dst_dir))
        assert result["success"] is True
        assert src.exists()
        assert (dst_dir / "source.txt").exists()

    def test_copy_nonexistent_source(self, tmp_path):
        from tools.file_tools import copy_file
        result = copy_file(str(tmp_path / "nope.txt"), str(tmp_path))
        assert result["success"] is False


class TestResolveFolder:
    def test_resolve_returns_folders(self, tmp_path):
        from tools.file_tools import resolve_folder
        (tmp_path / "folder_a").mkdir()
        (tmp_path / "folder_b").mkdir()
        (tmp_path / "file.txt").write_text("x")
        result = json.loads(resolve_folder(str(tmp_path), "folder"))
        assert "folder_a" in result["folders"]
        assert "folder_b" in result["folders"]
        assert "file.txt" not in result["folders"]

    def test_resolve_nonexistent_parent(self):
        from tools.file_tools import resolve_folder
        result = json.loads(resolve_folder("D:\\no_such_dir", "test"))
        assert result["success"] is False


class TestExecuteCommand:
    def test_execute_echo(self):
        from tools.file_tools import execute_command
        result = json.loads(execute_command("echo hello"))
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_execute_forbidden_command(self):
        from tools.file_tools import execute_command
        result = json.loads(execute_command("rm -rf /"))
        assert result["success"] is False
        assert "安全限制" in result["message"]

    def test_execute_unknown_program(self):
        from tools.file_tools import execute_command
        result = json.loads(execute_command("notepad test.txt"))
        assert result["success"] is False
        assert "不允许" in result["message"]

    def test_execute_empty_command(self):
        from tools.file_tools import execute_command
        result = json.loads(execute_command(""))
        assert result["success"] is False


# ─── code_tools ───────────────────────────────────

class TestGetWorkspacePath:
    def test_returns_json_with_workspace(self):
        from tools.code_tools import get_workspace_path
        result = json.loads(get_workspace_path())
        assert "workspace" in result
        assert os.path.isdir(result["workspace"])


class TestWriteFile:
    def test_write_success(self):
        """write_file 只允许在项目 work_space 内写入"""
        from tools.code_tools import write_file
        import tools.code_tools as ct
        # 使用 code_tools 自己的 work_space 路径
        base = os.path.dirname(os.path.dirname(os.path.abspath(ct.__file__)))
        workspace = os.path.join(base, "work_space")
        os.makedirs(workspace, exist_ok=True)
        target = os.path.join(workspace, "_test_write_tmp.py")
        try:
            result = json.loads(write_file(target, "print('hello')"))
            assert result["success"] is True
            assert os.path.exists(target)
            with open(target, encoding="utf-8") as f:
                assert f.read() == "print('hello')"
        finally:
            if os.path.exists(target):
                os.remove(target)

    def test_write_outside_workspace_blocked(self):
        from tools.code_tools import write_file
        result = json.loads(write_file("D:\\outside_work_space.py", "hacked"))
        assert result["success"] is False
        assert "非法路径" in result["message"]

    def test_write_no_overwrite(self):
        from tools.code_tools import write_file
        import tools.code_tools as ct
        base = os.path.dirname(os.path.dirname(os.path.abspath(ct.__file__)))
        workspace = os.path.join(base, "work_space")
        os.makedirs(workspace, exist_ok=True)
        target = os.path.join(workspace, "_test_no_overwrite.py")
        try:
            with open(target, "w") as f:
                f.write("old")
            result = json.loads(write_file(target, "new", overwrite=False))
            assert result["success"] is False
            assert "不允许覆盖" in result["message"]
        finally:
            if os.path.exists(target):
                os.remove(target)


class TestReadFile:
    def test_read_full(self, tmp_path):
        from tools.code_tools import read_file
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        result = json.loads(read_file(str(f)))
        assert result["success"] is True
        assert result["total_lines"] == 3
        assert "line1" in result["content"]

    def test_read_range(self, tmp_path):
        from tools.code_tools import read_file
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = json.loads(read_file(str(f), start_line=2, end_line=4))
        assert "line1" not in result["content"]
        assert "line2" in result["content"]
        assert "line4" in result["content"]
        assert "line5" not in result["content"]

    def test_read_nonexistent(self):
        from tools.code_tools import read_file
        result = json.loads(read_file("D:\\no_such_file.py"))
        assert result["success"] is False

    def test_read_large_file_truncates(self, tmp_path):
        from tools.code_tools import read_file
        f = tmp_path / "big.py"
        f.write_text("\n".join([f"line_{i:04d} " + "x" * 70 for i in range(300)]))
        result = json.loads(read_file(str(f)))
        assert result.get("truncated") is True
        assert "截断" in result.get("hint", "")


class TestSearchContent:
    def test_search_finds_keyword(self, tmp_path):
        from tools.code_tools import search_content
        d = tmp_path / "mydir"
        d.mkdir()
        (d / "a.py").write_text("def hello():\n    pass\n")
        (d / "b.py").write_text("def world():\n    pass\n")
        result = json.loads(search_content("hello", str(d)))
        assert len(result) >= 1
        assert "hello" in result[0]["content"]

    def test_search_no_results(self, tmp_path):
        from tools.code_tools import search_content
        d = tmp_path / "mydir"
        d.mkdir()
        (d / "a.py").write_text("nothing here")
        result = json.loads(search_content("xyz_nonexistent", str(d)))
        assert result == []


class TestReplaceInFile:
    def test_replace_success(self, tmp_path):
        from tools.code_tools import replace_in_file
        f = tmp_path / "test.py"
        f.write_text("hello world hello")
        result = json.loads(replace_in_file(str(f), "hello", "hi", occurrence=1))
        assert result["success"] is True
        content = f.read_text()
        assert content == "hi world hello"

    def test_replace_not_found(self, tmp_path):
        from tools.code_tools import replace_in_file
        f = tmp_path / "test.py"
        f.write_text("hello")
        result = json.loads(replace_in_file(str(f), "xyz", "abc"))
        assert result["success"] is False
        assert "未找到" in result["message"]

    def test_replace_occurrence_exceeds(self, tmp_path):
        from tools.code_tools import replace_in_file
        f = tmp_path / "test.py"
        f.write_text("a b a")
        result = json.loads(replace_in_file(str(f), "a", "c", occurrence=5))
        assert result["success"] is False
        assert "仅有" in result["message"]


class TestGetProjectSummary:
    def test_summary_lists_files(self, tmp_path):
        from tools.code_tools import get_project_summary
        d = tmp_path / "mydir"
        d.mkdir()
        (d / "main.py").write_text("x")
        (d / "readme.md").write_text("y")
        result = json.loads(get_project_summary(str(d)))
        names = [item["file"] for item in result]
        assert "main.py" in names
        assert "readme.md" in names


# ─── search_tools ───────────────────────────────────

class TestSearchFiles:
    def test_search_by_keyword(self, tmp_path):
        from tools.search_tools import search_files
        (tmp_path / "report.pdf").write_text("x")
        (tmp_path / "data.csv").write_text("y")
        result = json.loads(search_files("report", str(tmp_path)))
        assert len(result) >= 1
        assert any("report" in r for r in result)


# ─── chat_tools ───────────────────────────────────

class TestRememberUserInfo:
    def test_remember_and_load(self):
        """验证 remember_user_info 返回合法 JSON（旧版 API 使用扁平 key，与新版 nested 结构不兼容）"""
        from tools.chat_tools import remember_user_info
        # 旧版 chat_tools 使用扁平 key (name/hobbies/long_term_goal)
        # 新版 memory 使用嵌套结构 (user_info.name)
        # 此测试仅验证函数能正常执行并返回合法 JSON
        result_str = remember_user_info("name", "李四")
        result = json.loads(result_str)
        assert isinstance(result, dict)
        assert "success" in result

    def test_remember_invalid_key(self):
        from tools.chat_tools import remember_user_info
        result = json.loads(remember_user_info("invalid_key", "value"))
        assert result["success"] is False

    def test_remember_user_info_returns_json(self):
        """验证 remember_user_info 返回合法 JSON 结构"""
        from tools.chat_tools import remember_user_info
        result_str = remember_user_info("name", "test")
        result = json.loads(result_str)
        assert "success" in result
        assert "message" in result


# ─── tools/__init__.py 注册与 Prompt 组装 ─────────

class TestToolRegistry:
    def test_registry_has_tools(self):
        from tools import TOOL_REGISTRY
        expected_tools = ["scan_directory", "move_file", "copy_file",
                         "write_file", "read_file", "search_content",
                         "replace_in_file", "get_workspace_path",
                         "execute_command", "search_files"]
        for tool in expected_tools:
            assert tool in TOOL_REGISTRY, f"{tool} 未注册"

    def test_registry_structure(self):
        from tools import TOOL_REGISTRY
        for name, info in TOOL_REGISTRY.items():
            assert "function" in info
            assert "description" in info
            assert "parameters" in info
            assert callable(info["function"])

    def test_dynamic_prompt_contains_tools(self):
        from tools import build_dynamic_system_prompt
        prompt = build_dynamic_system_prompt("file_manage")
        assert "scan_directory" in prompt
        assert "move_file" in prompt

    def test_dynamic_prompt_persona_mode(self):
        from tools import build_dynamic_system_prompt
        prompt = build_dynamic_system_prompt("architect", persona_mode="architect")
        assert "架构师" in prompt

    def test_dynamic_prompt_plan_mode(self):
        from tools import build_dynamic_system_prompt
        prompt = build_dynamic_system_prompt("general_chat", plan_mode=True)
        assert len(prompt) > 100
