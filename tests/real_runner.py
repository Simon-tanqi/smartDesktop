# -*- coding: utf-8 -*-
"""real_runner.py - Tank Smart 真实单题执行器

直接调用 backend.agent.run_agent，真实驱动 Agent（本地 Ollama 真推理 + 工具真执行）。
收集 SSE 事件流，提取工具调用/结果、确认事件、最终回复、错误，落盘到
artifacts/run_{run_id}/T-XX/{input.txt, trace.log, result.json, output/}。

裁判只依据这里收集到的真实反馈（工具结果、生成文件、系统状态），不 mock 任何东西。
"""
import os
import io
import json
import time
import threading
import traceback
import logging

# 云端 DeepSeek（deepseek-v4-flash，API Key 取自环境变量 API_KEY / SMARTDESKTOP_API_KEY）
MODEL_CHOICE = {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "display": "deepseek-v4-flash (cloud)",
    "id": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "temperature": 0.3,
}


def _ensure_target_model_available():
    """测试框架专用：把目标模型（deepseek-v4-flash, 云端 DeepSeek）钉在 get_available_models() 首项。

    引擎 prepare_run_state 校验 model_choice.model 是否在可用模型列表、且 provider 匹配，
    不匹配则回退（禁止静默切云端）。此补丁保证列表恒含目标模型，杜绝误回退。

    同时把目标模型注册到配置层（get_model_from_cache / get_base_url），
    让 stream_llm_response / ai_concat 按 model_id 查到正确的云端 provider 与 base_url。
    API Key 由 llm_client.get_api_key 从环境变量 API_KEY / SMARTDESKTOP_API_KEY 读取。
    """
    import llm_client as _lc

    _orig = getattr(_lc, "_orig_get_available_models", None) or _lc.get_available_models

    def _patched(*args, **kwargs):
        """包装可用模型列表：将目标模型置顶"""
        models = list(_orig(*args, **kwargs))
        entry_idx = None
        for i, m in enumerate(models):
            if m.get("model") == MODEL_CHOICE["model"]:
                entry_idx = i
                break
        merged = dict(MODEL_CHOICE)
        if entry_idx is not None:
            old = models[entry_idx]
            merged.update({k: v for k, v in old.items() if k not in merged})
            # 无论条目原本在哪，都置顶（保证 prepare_run_state fallback 首选本地模型）
            models.pop(entry_idx)
            models.insert(0, merged)
        else:
            models.insert(0, dict(MODEL_CHOICE))
        return models

    _lc.get_available_models = _patched
    _lc._orig_get_available_models = _orig

    # ── 配置层补丁：按 model_id 直接返回本地模型配置 ──
    _CFG_ENTRY = {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash (cloud)",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "temperature": 0.3,
        "base_url": "https://api.deepseek.com",
        "stream": True,
    }

    _orig_cache = getattr(_lc, "_orig_get_model_from_cache", None) or _lc.get_model_from_cache

    def _patched_cache(model_id):
        """按模型 ID 返回目标模型配置"""
        if model_id == MODEL_CHOICE["id"]:
            return dict(_CFG_ENTRY)
        return _orig_cache(model_id)

    _lc.get_model_from_cache = _patched_cache
    _lc._orig_get_model_from_cache = _orig_cache

    _orig_base_url = getattr(_lc, "_orig_get_base_url", None) or _lc.get_base_url

    def _patched_base_url(model_id):
        """返回目标模型的 base_url"""
        if model_id == MODEL_CHOICE["id"]:
            return MODEL_CHOICE["base_url"]
        return _orig_base_url(model_id)

    _lc.get_base_url = _patched_base_url
    _lc._orig_get_base_url = _orig_base_url

    # 清掉 load_config 的模型缓存，确保补丁对后续 get_available_models 生效
    try:
        _lc.invalidate_models_cache()
    except Exception:
        pass


_ensure_target_model_available()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
ARTIFACTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")


def _setup_logging(run_id):
    """配置运行日志输出到 run 目录"""
    log_dir = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, "runner.log"), encoding="utf-8", mode="w"
    )
    fh.setLevel(logging.WARNING)
    logging.basicConfig(handlers=[fh], level=logging.WARNING, force=True)


def parse_sse(chunk):
    """解析 SSE 数据块为 JSON，无效返回 None"""
    if not chunk:
        return None
    s = chunk
    if s.startswith("data: "):
        s = s[len("data: "):]
    elif s.startswith("data:"):
        s = s[5:]
    s = s.strip()
    if not s or s == "[DONE]":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


PATH_KEYS = ("file_path", "path", "output_path", "filename", "output_file", "file", "save_path")


def collect_created_files(tool_calls):
    """从工具调用参数中收集真实存在的输出文件"""
    files = []
    for t in tool_calls:
        args = t.get("args") or {}
        if not isinstance(args, dict):
            continue
        for k in PATH_KEYS:
            v = args.get(k)
            if not isinstance(v, str) or not v.strip():
                continue
            p = v.strip()
            if not os.path.isabs(p):
                p = os.path.join(PROJECT_ROOT, p)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                files.append(p)
    return sorted(set(files))


def run_single_test(tc, run_id, gate, retry_index=0):
    """真实执行单题，返回 (run_result, test_dir)。"""
    _setup_logging(run_id)
    run_dir = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id)
    test_dir = os.path.join(run_dir, tc.id)
    out_dir = os.path.join(test_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    # input.txt
    with io.open(os.path.join(test_dir, "input.txt"), "w", encoding="utf-8") as f:
        f.write(tc.user_input + "\n")
    # trace.log
    trace_path = os.path.join(test_dir, "trace.log")

    session_id = "tanksmart_%s_%s_r%d" % (run_id, tc.id, retry_index)
    cancel = threading.Event()
    events = []
    tool_calls = []
    final_text = ""
    errors = []
    confirms = []
    warnings = []

    # 重置网关的 pending（每题独立会话）
    gate.pending = {}
    gate.danger_attempts = []

    start = time.time()
    gen = None
    try:
        from backend.agent import run_agent
        gen = run_agent(
            user_message=tc.user_input,
            model_choice=MODEL_CHOICE,
            session_id=session_id,
            user_id="tanksmart",
            streaming=True,
            confirmation_handler=gate.handler,
            cancel_event=cancel,
        )
        for chunk in gen:
            ev = parse_sse(chunk)
            if not ev:
                continue
            etype = ev.get("type")
            events.append(ev)
            with io.open(trace_path, "a", encoding="utf-8") as tf:
                tf.write("[%s] %s\n" % (etype, json.dumps(ev, ensure_ascii=False)))
            if etype == "tool_call":
                tool_calls.append({
                    "tool_name": ev.get("tool_name"),
                    "args": ev.get("arguments"),
                    "result": None,
                    "status": "called",
                })
            elif etype == "tool_result":
                tn = ev.get("tool_name")
                res = ev.get("result")
                paired = False
                for t in reversed(tool_calls):
                    if t["tool_name"] == tn and t["result"] is None:
                        t["args"] = ev.get("args", t["args"])
                        t["result"] = res
                        ok = isinstance(res, dict) and bool(res.get("success"))
                        t["status"] = "success" if ok else "failed"
                        paired = True
                        break
                if not paired:
                    tool_calls.append({
                        "tool_name": tn, "args": ev.get("args"),
                        "result": res, "status": "result",
                    })
            elif etype == "confirm":
                confirms.append(ev)
                gate.on_confirm_event(ev)
            elif etype == "final":
                final_text = ev.get("text", "")
            elif etype == "error":
                errors.append(ev.get("text", ""))
            elif etype == "warning":
                warnings.append(ev.get("text", ""))
    except Exception:
        errors.append("RUNNER_EXCEPTION:\n" + traceback.format_exc())

    duration_ms = int((time.time() - start) * 1000)
    files_created = collect_created_files(tool_calls)

    run_result = {
        "test_id": tc.id,
        "started_at": start,
        "category": tc.category,
        "title": tc.title,
        "input": tc.user_input,
        "status": "pending",
        "duration_ms": duration_ms,
        "retry_count": retry_index,
        "final_text": final_text,
        "errors": errors,
        "warnings": warnings,
        "tool_calls": tool_calls,
        "confirms": confirms,
        "files_created": files_created,
        "danger_attempts": list(gate.danger_attempts),
        "expected_result": tc.expected_result,
        "verification": tc.verification,
        "prerequisites": tc.prerequisites,
    }
    return run_result, test_dir


def write_result(test_dir, run_result):
    """将单题运行结果写入 result.json"""
    with io.open(os.path.join(test_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(run_result, f, ensure_ascii=False, indent=2)
