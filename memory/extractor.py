"""
事实提取器：从用户消息中异步提取项目/技能/事实，写入 Markdown 长期记忆。

核心改动：
  - 异步执行（daemon 线程），不阻塞主流程
  - 写入 Markdown 文件（追加式），不再全量读写 JSON
"""
import json
import threading
import datetime
from memory.llm import call_llm
from memory.long_term import append_project, append_skill, append_fact
from memory.stores.sqlite_store import log_operation

FACT_EXTRACTION_PROMPT = """
你是一个记忆提取专家。请分析用户的输入，判断并提取以下信息：
1. 是否包含【项目信息】（如开发、负责、做了XX系统）？
2. 是否包含【经验/技能】（如擅长、精通、熟悉XX技术）？
3. 是否包含【一般事实】（如名字、年龄、喜好、经历）？

如果包含，请用 JSON 格式输出，只包含非空的字段。
格式：
{"project": "项目描述", "knowledge": "技能描述", "fact": "事实描述"}
如果都不包含，输出：{"none": true}
""".strip()

_worker_call_counter = 0


def extract_facts_async(content, trace_id="", user_id="", session_id="", model=None):
    """
    异步事实提取：后台线程执行，不阻塞主调用链。

    线程在后台完成 LLM 调用 → 写入 Markdown → 操作日志。
    失败时静默记录 print，不影响主流程。
    """
    global _worker_call_counter
    _worker_call_counter += 1
    call_id = _worker_call_counter

    def _run():
        try:
            system_prompt = {"role": "system", "content": FACT_EXTRACTION_PROMPT}
            user_prompt = {"role": "user", "content": content}

            ai_response = call_llm([system_prompt, user_prompt], model=model)

            if not ai_response or "message" not in ai_response:
                return

            try:
                analysis = json.loads(ai_response["message"]["content"])
            except json.JSONDecodeError:
                log_operation(trace_id, user_id, session_id,
                              "memory.extract.error", "JSON parse failed (async worker)")
                return

            if "none" in analysis:
                return

            updated = False
            now = datetime.datetime.now()

            if "project" in analysis:
                proj_text = f"[{now.strftime('%m-%d')}] {analysis['project']}"
                append_project(proj_text)
                updated = True

            if "knowledge" in analysis:
                append_skill(analysis["knowledge"])
                updated = True

            if "fact" in analysis and analysis["fact"] is not None:
                append_fact(analysis["fact"])
                updated = True

            if updated:
                log_operation(trace_id, user_id, session_id,
                              "memory.extract", f"async: {analysis}")

        except Exception as e:
            print(f"[Memory] 异步事实提取失败 (call #{call_id}): {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"mem-extract-{call_id}")
    t.start()


# ── 同步版本（保留供 debug / 测试使用）─

def extract_facts(content, trace_id="", user_id="", session_id="", model=None):
    """
    同步事实提取（保留兼容，新代码请用 extract_facts_async）。
    注意：此函数阻塞主链路 2-5s，不推荐生产使用。
    """
    system_prompt = {"role": "system", "content": FACT_EXTRACTION_PROMPT}
    user_prompt = {"role": "user", "content": content}

    ai_response = call_llm([system_prompt, user_prompt], model=model)

    if not ai_response or "message" not in ai_response:
        return

    try:
        analysis = json.loads(ai_response["message"]["content"])
    except json.JSONDecodeError:
        log_operation(trace_id, user_id, session_id, "memory.extract.error", "JSON parse failed")
        return

    if "none" in analysis:
        return

    updated = False
    now = datetime.datetime.now()

    if "project" in analysis:
        proj_text = f"[{now.strftime('%m-%d')}] {analysis['project']}"
        append_project(proj_text)
        updated = True

    if "knowledge" in analysis:
        append_skill(analysis["knowledge"])
        updated = True

    if "fact" in analysis and analysis["fact"] is not None:
        append_fact(analysis["fact"])
        updated = True

    if updated:
        log_operation(trace_id, user_id, session_id, "memory.extract", f"long_term updated: {analysis}")
