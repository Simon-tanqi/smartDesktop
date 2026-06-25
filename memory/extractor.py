"""
事实提取器：本地模型 + 自定义技能支持

v4：
  - 使用 YAML 配置的本地模型（Ollama）
  - 支持自定义技能类别提取
  - 异步执行，不阻塞主流程
"""
import json
import threading
import datetime
from memory.config import EXTRACT_MODEL_CONFIG
from memory.llm import call_llm_with_config
from memory.long_term import append_project, append_skill, append_fact, append_custom_skill
from memory.stores.sqlite_store import log_operation


# ── 提取提示词模板 ──

FACT_EXTRACTION_PROMPT_BASE = """你是一个记忆提取专家。分析用户输入，提取以下信息：

1. 【项目信息】开发/负责/做了某系统等
2. 【经验/技能】擅长/精通/熟悉某技术
3. 【一般事实】名字/年龄/喜好/经历/偏好
4. 【未完成任务】用户提到要做但未完成的事
{extra_categories}

JSON 输出，只包含非空字段：
{{"project": "项目", "knowledge": "技能", "fact": "事实", "unresolved_task": "未完成任务"{extra_fields}}}
如果都不包含，输出：{{"none": true}}
""".strip()

# 自定义技能类别（运行时动态追加）
_custom_skill_categories = []


def register_custom_categories(categories):
    """注册自定义技能类别，供提取器识别。

    categories 示例：[{"key": "stock", "label": "股票关注"}]
    """
    global _custom_skill_categories
    _custom_skill_categories = categories or []


def _build_extraction_prompt():
    """动态构建提取提示词，注入自定义技能类别。"""
    extra_categories = ""
    extra_fields = ""
    if _custom_skill_categories:
        lines = []
        fields = []
        for i, cat in enumerate(_custom_skill_categories):
            idx = i + 5  # 从 5 开始，避免与基础类别冲突
            lines.append(f"{idx}. 【{cat['label']}】{cat.get('description', '')}")
            fields.append(f'"{cat["key"]}": "{cat["label"]}相关"')
        extra_categories = "\n".join(lines)
        extra_fields = ", " + ", ".join(fields)
    return FACT_EXTRACTION_PROMPT_BASE.format(
        extra_categories=f"\n{extra_categories}" if extra_categories else "",
        extra_fields=extra_fields,
    )


_worker_call_counter = 0


def extract_facts_async(content, trace_id="", user_id="", session_id=""):
    """异步事实提取。"""
    global _worker_call_counter
    _worker_call_counter += 1
    call_id = _worker_call_counter

    def _run():
        try:
            prompt = _build_extraction_prompt()
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ]
            ai_response = call_llm_with_config(messages, EXTRACT_MODEL_CONFIG)
            if not ai_response or "message" not in ai_response:
                return

            raw = ai_response["message"]["content"].strip()
            # 清理  response 前缀
            if raw.startswith(" response"):
                raw = raw.split(" response", 1)[-1].strip()
            try:
                analysis = json.loads(raw)
            except json.JSONDecodeError:
                log_operation(trace_id, user_id, session_id,
                              "memory.extract.error", f"JSON parse: {raw[:100]}")
                return

            if "none" in analysis:
                return

            updated = False
            now = datetime.datetime.now()

            if analysis.get("project"):
                proj_text = f"[{now.strftime('%m-%d')}] {analysis['project']}"
                append_project(proj_text)
                updated = True

            if analysis.get("knowledge"):
                append_skill(analysis["knowledge"])
                updated = True

            if analysis.get("fact"):
                append_fact(analysis["fact"])
                updated = True

            if analysis.get("unresolved_task"):
                append_fact(f"[未完成] {analysis['unresolved_task']}")
                updated = True

            # 自定义技能类别
            for cat in _custom_skill_categories:
                key = cat["key"]
                if analysis.get(key):
                    append_custom_skill(key, analysis[key])
                    updated = True

            if updated:
                log_operation(trace_id, user_id, session_id,
                              "memory.extract", f"async: {analysis}")

        except Exception as e:
            # daemon 线程异常不阻塞主程序，仅在非连接类错误时输出到 stderr
            err_msg = str(e)
            if "Max retries exceeded" not in err_msg and "Connection refused" not in err_msg:
                print(f"[Memory] 异步提取失败 (call #{call_id}): {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"mem-extract-{call_id}")
    t.start()


def extract_facts(content, trace_id="", user_id="", session_id=""):
    """同步提取（保留兼容），阻塞主链路。"""
    try:
        prompt = _build_extraction_prompt()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ]
        ai_response = call_llm_with_config(messages, EXTRACT_MODEL_CONFIG)
        if not ai_response or "message" not in ai_response:
            return

        raw = ai_response["message"]["content"].strip()
        if raw.startswith(" response"):
            raw = raw.split(" response", 1)[-1].strip()
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        log_operation(trace_id, user_id, session_id,
                      "memory.extract.error", "JSON parse")
        return

    if "none" in analysis:
        return

    updated = False
    now = datetime.datetime.now()

    if analysis.get("project"):
        append_project(f"[{now.strftime('%m-%d')}] {analysis['project']}")
        updated = True
    if analysis.get("knowledge"):
        append_skill(analysis["knowledge"])
        updated = True
    if analysis.get("fact"):
        append_fact(analysis["fact"])
        updated = True
    if analysis.get("unresolved_task"):
        append_fact(f"[未完成] {analysis['unresolved_task']}")
        updated = True

    for cat in _custom_skill_categories:
        key = cat["key"]
        if analysis.get(key):
            append_custom_skill(key, analysis[key])
            updated = True

    if updated:
        log_operation(trace_id, user_id, session_id, "memory.extract", f"updated: {analysis}")
