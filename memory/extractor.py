"""
事实提取器：本地模型 + 自定义技能支持

v4：
  - 使用 YAML 配置的本地模型（Ollama）
  - 支持自定义技能类别提取
  - 异步执行，不阻塞主流程
"""
import json
import re
import threading
import datetime
from memory.config import EXTRACT_MODEL_CONFIG
from memory.llm import call_llm_with_config
from memory.long_term import append_project, append_skill, append_fact, append_custom_skill
from memory.stores.sqlite_store import log_operation


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


_custom_skill_categories = []
_categories_lock = threading.Lock()
_counter_lock = threading.Lock()
_worker_call_counter = 0


def register_custom_categories(categories):
    """注册自定义技能类别，供提取器识别。

    categories 示例：[{"key": "stock", "label": "股票关注"}]
    """
    global _custom_skill_categories
    with _categories_lock:
        _custom_skill_categories = categories or []


def _build_extraction_prompt(categories=None):
    """动态构建提取提示词，注入自定义技能类别。"""
    cats = categories if categories is not None else _custom_skill_categories
    extra_categories = ""
    extra_fields = ""
    if cats:
        lines = []
        fields = []
        for i, cat in enumerate(cats):
            idx = i + 5
            lines.append(f"{idx}. 【{cat['label']}】{cat.get('description', '')}")
            fields.append(f'"{cat["key"]}": "{cat["label"]}相关"')
        extra_categories = "\n".join(lines)
        extra_fields = ", " + ", ".join(fields)
    return FACT_EXTRACTION_PROMPT_BASE.format(
        extra_categories=f"\n{extra_categories}" if extra_categories else "",
        extra_fields=extra_fields,
    )


def _clean_llm_output(raw):
    """清理 LLM 输出，去除思考标记和代码块。"""
    if not raw:
        return ""
    text = raw.strip()
    # 整体删除 thinking 块（含块内思考文本），避免残留文本阻断后续代码块剥离
    text = re.sub(r'<\|begin[_ ]of[_ ]thinking\|>.*?<\|end[_ ]of[_ ]thinking\|>', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    # 清理可能残留的单个标记（如只有 begin 或只有 end）
    text = re.sub(r'<\|.*?\|>', '', text)
    text = re.sub(r'^\s*```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```\s*$', '', text)
    text = text.strip()
    return text


def _safe_json_parse(raw):
    """安全解析 JSON，支持多种格式的容错处理。"""
    text = _clean_llm_output(raw)
    if not text:
        return None

    try:
        if text.startswith("{"):
            return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        json_str = match.group(0)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_facts_core(content, trace_id="", user_id="", session_id="", categories=None):
    """核心提取逻辑，被同步和异步版本共享。"""
    if not content or not content.strip():
        return

    prompt = _build_extraction_prompt(categories)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content},
    ]

    ai_response = call_llm_with_config(messages, EXTRACT_MODEL_CONFIG)
    if not ai_response or "message" not in ai_response:
        return

    raw = ai_response["message"]["content"]
    analysis = _safe_json_parse(raw)

    if analysis is None:
        log_operation(trace_id, user_id, session_id,
                      "memory.extract.error", f"JSON parse failed: {raw[:100]}")
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

    cats = categories if categories is not None else _custom_skill_categories
    for cat in cats:
        key = cat["key"]
        if analysis.get(key):
            append_custom_skill(key, analysis[key])
            updated = True

    if updated:
        log_operation(trace_id, user_id, session_id,
                      "memory.extract", json.dumps(analysis, ensure_ascii=False))


def extract_facts_async(content, trace_id="", user_id="", session_id=""):
    """异步事实提取。"""
    global _worker_call_counter
    with _counter_lock:
        _worker_call_counter += 1
        call_id = _worker_call_counter

    with _categories_lock:
        categories_snapshot = list(_custom_skill_categories)

    def _run():
        """后台线程执行事实提取，忽略网络类异常避免错误刷屏"""
        try:
            _extract_facts_core(content, trace_id, user_id, session_id, categories_snapshot)
        except Exception as e:
            err_msg = str(e)
            if "Max retries exceeded" not in err_msg and "Connection refused" not in err_msg:
                print(f"[Memory] 异步提取失败 (call #{call_id}): {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"mem-extract-{call_id}")
    t.start()


def extract_facts(content, trace_id="", user_id="", session_id=""):
    """同步提取（保留兼容），阻塞主链路。"""
    try:
        _extract_facts_core(content, trace_id, user_id, session_id)
    except json.JSONDecodeError:
        log_operation(trace_id, user_id, session_id,
                      "memory.extract.error", "JSON parse")
    except Exception as e:
        log_operation(trace_id, user_id, session_id,
                      "memory.extract.error", str(e)[:100])
        print(f"[Memory] 同步提取失败: {e}")