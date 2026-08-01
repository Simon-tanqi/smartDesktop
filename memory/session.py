"""
会话生命周期管理：创建、切换、删除会话，以及消息写入主入口。

v4：
  - 适配 per-session 消息存储
  - 压缩摘要从 per-session summaries.db 读取
  - Token 预算制触发压缩
  - 每轮重新压缩：对全部历史重新生成综合性摘要
  - 结构化思考轨迹：解析 reasoning_trace JSON
"""
import os
import re
import json
import logging
import datetime
import threading

from memory.config import (
    ACTIVE_SESSION_FILE, LOGS_DIR, NOISE_PHRASES,
    RECENT_TOKEN_MIN, RECENT_TOKEN_MAX, SUMMARY_TOKEN_BUDGET,
    COMPRESS_MODEL_CONFIG, MAX_SUMMARY_CHARS,
)
from memory.stores.json_store import load_sessions_db, save_sessions_db
from memory.stores.sqlite_store import (
    add_chat_message, get_chat_messages, log_operation,
    get_session_messages, get_full_session_messages, get_latest_summary,
    add_summary, count_chat_messages,
)
from logger import get_logger
from memory.extractor import extract_facts_async
from memory.compressor import (
    compress_conversation_async,
    estimate_messages_tokens,
    estimate_tokens,
    increment_merge_summary,
)
from memory.long_term import get_relevant_memory


# 在 session.py 顶部定义常量
WORKING_MEMORY_ROUNDS = 2  # 情节缓存保留轮数

def _get_working_memory(session_id: str) -> dict:
    """获取当前会话的通用工作记忆（通用键值容器）。
    
    返回结构：
    {
        "variables": {},        # Agent 自由写入的任意键值对
        "plan": [],            # 当前任务拆解的计划步骤（可选）
        "current_step": 0,      # 当前执行到的步骤
        "last_observation": "", # 上一步的观察结果
    }
    """
    db = load_sessions_db()
    session_data = db.get(session_id, {})
    
    # 若不存在，初始化一个空的工作记忆容器
    if "working_memory" not in session_data:
        session_data["working_memory"] = {
            "variables": {},
            "plan": [],
            "current_step": 0,
            "last_observation": ""
        }
        db[session_id] = session_data
        save_sessions_db(db)
    
    return session_data["working_memory"]

def _update_working_memory(session_id: str, updates: dict):
    """供外部或工具调用的工作记忆更新接口。
    
    Args:
        updates: 要更新的字段，如 {"variables": {"file_path": "/tmp/a.csv"}}
    """
    db = load_sessions_db()
    if session_id not in db:
        return False
    
    session_data = db[session_id]
    if "working_memory" not in session_data:
        session_data["working_memory"] = {"variables": {}, "plan": [], "current_step": 0}
    
    # 深度合并（简单示例，生产可用深合并）
    for key, value in updates.items():
        if key == "variables" and isinstance(value, dict):
            session_data["working_memory"]["variables"].update(value)
        else:
            session_data["working_memory"][key] = value
    
    save_sessions_db(db)
    return True

def _get_tool_cache_from_registry(available_tools: list) -> list:
    """第一阶段：仅输出名称+单行描述的'菜单'，不携带完整 schema。"""
    cache = []
    for tool in available_tools:
        desc = tool.get("description", "")
        # 截取第一行或前80字符作为单行描述
        single_line = desc.split('\n')[0][:80] if desc else "无描述"
        cache.append({
            "name": tool.get("name", "unknown"),
            "desc": single_line
        })
    return cache


def _make_title(content: str) -> str:
    if not content:
        return "新会话"
    cleaned = re.sub(r"```[\s\S]*?```", "", content)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or not re.search(r'[\u4e00-\u9fff\w]', cleaned):
        fallback = re.sub(r"```[\s\S]*?```", "", content)
        fallback = re.sub(r"\s+", " ", fallback).strip()
        if not fallback:
            return "新会话"
        cleaned = fallback
    return cleaned[:25]


_FACT_EXTRACT_MIN_LEN = 3


def _is_noise(text: str) -> bool:
    normalized = re.sub(r'(.)\1+', r'\1', text.strip())
    return normalized in set(NOISE_PHRASES) or text.strip() in set(NOISE_PHRASES)


_session_lock = threading.Lock()


def generate_session_id():
    now = datetime.datetime.now()
    base = now.strftime("%Y%m%d%H")
    db = load_sessions_db()
    max_num = 0
    for sid in db:
        if sid.startswith(base):
            try:
                num = int(sid.split("_")[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    return f"{base}_{max_num + 1}"


def _get_next_session_name():
    db = load_sessions_db()
    used_numbers = set()
    for data in db.values():
        title = data.get("title", "")
        m = re.match(r"^新会话(\d+)$", title)
        if m:
            used_numbers.add(int(m.group(1)))
    n = 1
    while n in used_numbers:
        n += 1
    return f"新会话{n}"


def create_session(title=None):
    with _session_lock:
        if title is None:
            title = _get_next_session_name()
        session_id = generate_session_id()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db = load_sessions_db()
        db[session_id] = {
            "id": session_id,
            "title": title,
            "created": now,
        }
        save_sessions_db(db)
    set_active_session(session_id)
    return session_id


def rename_session(session_id, new_title):
    new_title = new_title.strip()
    if not new_title:
        return False
    db = load_sessions_db()
    if session_id not in db:
        return False
    db[session_id]["title"] = new_title
    save_sessions_db(db)
    return True


def ensure_session_titles():
    db = load_sessions_db()
    used_numbers = set()
    for data in db.values():
        title = data.get("title", "")
        m = re.match(r"^新会话(\d+)$", title)
        if m:
            used_numbers.add(int(m.group(1)))
    changed = False
    for sid, data in db.items():
        title = data.get("title", "")
        if not title or title.strip() == "":
            n = 1
            while n in used_numbers:
                n += 1
            data["title"] = f"新会话{n}"
            used_numbers.add(n)
            changed = True
    if changed:
        save_sessions_db(db)


def get_active_session_id():
    with _session_lock:
        if os.path.exists(ACTIVE_SESSION_FILE):
            with open(ACTIVE_SESSION_FILE, "r", encoding="utf-8") as f:
                sid = f.read().strip()
            db = load_sessions_db()
            if sid and sid in db:
                return sid
        return create_session("新会话")


def set_active_session(session_id):
    with open(ACTIVE_SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(session_id)


def list_sessions():
    db = load_sessions_db()
    sessions = []
    for sid, data in db.items():
        if sid.startswith(("test", "diag", "debug")):
            continue
        sessions.append({
            "id": sid,
            "title": data.get("title", sid),
            "created_at": data.get("created", ""),
        })
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    return sessions


def get_session(session_id):
    db = load_sessions_db()
    return db.get(session_id)


def delete_session(session_id):
    from memory.stores.sqlite_store import delete_chat_session
    db = load_sessions_db()
    if session_id in db:
        del db[session_id]
        save_sessions_db(db)
        delete_chat_session(session_id)
        if os.path.exists(ACTIVE_SESSION_FILE):
            with open(ACTIVE_SESSION_FILE, "r", encoding="utf-8") as f:
                if f.read().strip() == session_id:
                    os.remove(ACTIVE_SESSION_FILE)


def update_last_turn(session_id, turn):
    db = load_sessions_db()
    if session_id in db:
        db[session_id]["last_turn"] = turn
        save_sessions_db(db)


def get_last_turn(session_id):
    db = load_sessions_db()
    return db.get(session_id, {}).get("last_turn", -1)


def load_turn_count(session_id: str) -> int:
    """读该会话已分配的最大 turn 序号；不存在返回 -1。读失败返回 -1 并记日志，绝不抛。"""
    try:
        db = load_sessions_db()
        return db.get(session_id, {}).get("turn_count", -1)
    except Exception as e:
        get_logger().error(f"load_turn_count 失败 session={session_id}: {e}")
        return -1


def save_turn_count(session_id: str, n: int) -> bool:
    """写回；成功返回 True，失败返回 False（调用方据此回滚内存）。"""
    try:
        db = load_sessions_db()
        if session_id in db:
            db[session_id]["turn_count"] = n
            save_sessions_db(db)
            return True
        return False
    except Exception as e:
        get_logger().error(f"save_turn_count 失败 session={session_id}: {e}")
        return False


def _detect_repeated_query(messages, current_user_msg, similarity_threshold=0.35):
    if not current_user_msg or not messages:
        return []

    def _bigrams(text):
        chinese = re.sub(r'[^\u4e00-\u9fff]', '', text)
        english = re.findall(r'[a-zA-Z]{2,}', text.lower())
        features = set()
        if len(chinese) >= 2:
            features.update(chinese[i:i+2] for i in range(len(chinese)-1))
        features.update(english)
        return features

    def _is_tool_call(content):
        content = content.strip()
        return ('"tool_name"' in content[:200] or
                content.startswith('{"tool_call"') or
                content.startswith('{"type":"tool"'))

    current_bigrams = _bigrams(current_user_msg)
    if not current_bigrams:
        return []

    exclude_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        hist_content = msg.get("content", "")
        hist_bigrams = _bigrams(hist_content)
        if not hist_bigrams:
            continue
        intersection = len(current_bigrams & hist_bigrams)
        union = len(current_bigrams | hist_bigrams)
        if union == 0:
            continue
        if intersection / union >= similarity_threshold:
            j = i + 1
            turn_assistants = []
            while j < len(messages) and messages[j].get("role") != "user":
                if messages[j].get("role") == "assistant":
                    turn_assistants.append(j)
                j += 1
            for idx in turn_assistants:
                if not _is_tool_call(messages[idx].get("content", "")):
                    exclude_indices.append(idx)
    return sorted(set(exclude_indices), reverse=True)


def _calculate_session_relevance(current_user_msg, messages, threshold=0.1):
    if not current_user_msg or not messages:
        return True, 1.0

    def _bigrams(text):
        chinese = re.sub(r'[^\u4e00-\u9fff]', '', text)
        english = re.findall(r'[a-zA-Z]{2,}', text.lower())
        features = set()
        if len(chinese) >= 2:
            features.update(chinese[i:i+2] for i in range(len(chinese)-1))
        features.update(english)
        return features

    current_bigrams = _bigrams(current_user_msg)
    if not current_bigrams:
        return True, 1.0

    user_messages = [(i, m) for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_messages:
        return True, 1.0

    total_weight = 0.0
    weighted_score = 0.0
    n = len(user_messages)

    for idx, (pos, msg) in enumerate(user_messages):
        hist_bigrams = _bigrams(msg.get("content", ""))
        if not hist_bigrams:
            continue
        intersection = len(current_bigrams & hist_bigrams)
        union = len(current_bigrams | hist_bigrams)
        if union == 0:
            continue
        score = intersection / union
        recency_weight = 0.5 + 0.5 * (idx / max(n - 1, 1))
        weighted_score += score * recency_weight
        total_weight += recency_weight

    if total_weight == 0:
        return False, 0.0

    avg_score = weighted_score / total_weight
    return avg_score >= threshold, avg_score
    
#供 Agent 调用
def tool_save_memory(session_id: str, key: str, value: str) -> str:
    """保存关键信息到工作记忆（用于跨轮次传递）。
    
    Args:
        key: 变量名（如 "generated_file", "user_preference"）
        value: 具体的值
    """
    if not session_id:
        return "错误：未找到会话"
    
    current = _get_working_memory(session_id)
    current["variables"][key] = value
    _update_working_memory(session_id, {"variables": {key: value}})
    
    return f"已记住：{key} = {value[:50]}..."

def tool_recall_memory(session_id: str, key: str = None) -> str:
    """从工作记忆中提取信息。
    
    Args:
        key: 要查询的变量名，若不填则返回所有记忆。
    """
    if not session_id:
        return "错误：未找到会话"
    
    memory = _get_working_memory(session_id)
    if key:
        value = memory["variables"].get(key, "未找到该信息")
        return f"{key} = {value}"
    else:
        return json.dumps(memory["variables"], ensure_ascii=False, indent=2)


def _parse_tool_call(content):
    tool_name = ""
    tool_arguments = {}

    try:
        clean_content = content.strip()

        action_match = re.search(r'```(?:action|json)?\s*([\s\S]*?)\s*```', clean_content)
        if action_match:
            clean_content = action_match.group(1).strip()

        parsed = json.loads(clean_content)
        if isinstance(parsed, dict):
            tool_name = parsed.get("tool_name") or parsed.get("tool_call", "")
            if isinstance(tool_name, dict):
                tool_name = tool_name.get("tool_name", "")
            tool_arguments = parsed.get("arguments", {})
    except (json.JSONDecodeError, TypeError):
        pass

    return tool_name, tool_arguments


def _extract_tool_calls(content):
    tool_calls = []

    try:
        clean_content = content.strip()

        action_match = re.search(r'```(?:action|json)?\s*([\s\S]*?)\s*```', clean_content)
        if action_match:
            clean_content = action_match.group(1).strip()

        parsed = json.loads(clean_content)
        if isinstance(parsed, dict):
            tool_name = parsed.get("tool_name") or parsed.get("tool_call", "")
            if isinstance(tool_name, dict):
                tool_name = tool_name.get("tool_name", "")
            tool_args = parsed.get("arguments", {})
            if tool_name:
                tool_calls.append({"tool_name": tool_name, "arguments": tool_args})
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    tool_name = item.get("tool_name") or item.get("tool_call", "")
                    if isinstance(tool_name, dict):
                        tool_name = tool_name.get("tool_name", "")
                    tool_args = item.get("arguments", {})
                    if tool_name:
                        tool_calls.append({"tool_name": tool_name, "arguments": tool_args})
    except (json.JSONDecodeError, TypeError):
        pass

    return tool_calls


def _parse_reasoning_trace(content):
    """从 AI 回复中解析结构化思考轨迹 JSON。
    
    期望格式：
    ```reasoning
    {
        "current_intent": "...",
        "decision_rationale": "...",
        "active_hypothesis": "...",
        "next_steps": [...],
        "confidence": 0.8
    }
    ```
    """
    try:
        reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', content)
        if reasoning_match:
            reasoning_json = reasoning_match.group(1).strip()
            parsed = json.loads(reasoning_json)
            if isinstance(parsed, dict):
                return parsed

        json_match = re.search(r'(\{[\s\S]*"current_intent"[\s\S]*\})', content)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                if isinstance(parsed, dict) and "current_intent" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _clean_reasoning_for_archive(child_turn, preserve_full=False):
    """清理子轮次中的思考轨迹，仅保留工具调用和精简结果。
    
    设计要点：思考轨迹不永久存储，仅在当前用户任务内的子轮次间传递。
    
    Args:
        preserve_full: 归档时仍完整保留工具结果（含 key field 的长 JSON）
    """
    tool_result_raw = child_turn.get("tool_result", "")
    
    # 判断是否包含关键字段的长 JSON
    _KEY_FIELDS = ['UninstallString', 'InstallLocation', 'ExecutablePath', 'DisplayName']
    has_key_fields = any(f in str(tool_result_raw) for f in _KEY_FIELDS)
    is_long_json = isinstance(tool_result_raw, str) and len(tool_result_raw) > 300
    
    # 长且含关键字段的 JSON，归档时完整保留
    if is_long_json and has_key_fields:
        tool_result_clean = str(tool_result_raw)
    else:
        tool_result_clean = _trim_tool_result(tool_result_raw, max_len=2000 if preserve_full else 400)
    
    cleaned = {
        "child_turn": child_turn.get("child_turn"),
        "timestamp": child_turn.get("timestamp"),
        "tool_calls": child_turn.get("tool_calls", []),
        "tool_result": tool_result_clean,
    }
    return cleaned


def _trim_tool_result(result, max_len=200, preserve_full=False):
    """精简工具结果。
    
    成功结果保留更多信息（2000 字符）；失败结果仅保留错误类型和核心原因（200 字符）。
    关键字段（UninstallString, ExecutablePath, DisplayName 等）始终优先保留。
    """
    if not result:
        return ""
    
    result_str = str(result)
    
    # 调用方明确要求完整保留
    if preserve_full:
        return result_str
    
    # 判断是否包含关键信息的长 JSON
    _KEY_FIELDS = ['UninstallString', 'InstallLocation', 'ExecutablePath', 'DisplayName']
    has_key_fields = any(f in result_str for f in _KEY_FIELDS)
    
    # 判断是否失败
    is_error = any(keyword in result_str.lower() for keyword in ["fail", "失败", "error", "错误"])
    
    if is_error:
        # 失败：提取错误位置附近的 ±100 字符
        for keyword in ["失败", "错误", "fail", "error"]:
            idx = result_str.lower().find(keyword)
            if idx != -1:
                start = max(0, idx - 30)
                return result_str[start:start + max_len].strip()
        return result_str[:max_len].strip()
    
    # 成功 + 含关键字段 → 保留 2000 字符，且强制确保关键字段完整
    if has_key_fields:
        truncated = result_str[:2000]
        for field in _KEY_FIELDS:
            if field in truncated:
                # 确保字段值不截断在中间
                field_end = truncated.find(field) + len(field) + 2  # 跳过 ": "
                val_end = truncated.find('"', field_end + 1)
                if val_end == -1 or val_end > len(truncated) - 2:
                    # 字段值可能被截断，延长到完整字段结束
                    full_end = result_str.find('"', field_end + 1)
                    if full_end != -1:
                        truncated = result_str[:max(full_end + 1, len(truncated))]
        if len(truncated) > len(result_str):
            truncated = result_str
        return truncated
    
    # 普通成功结果：简单截断
    return result_str[:max_len].strip()


def _build_structured_history(messages, session_id="", 
                              preserve_thinking_rounds=3,
                              is_current_turn=False,
                              max_reasoning_subturns=2):
    """构建结构化历史。
    
    设计要点：
    1. 思考轨迹生命周期：仅在当前用户任务内的子轮次间传递，轮次归档后剥离
    2. 子轮次裁剪规则：近3轮子轮次保留完整思考轨迹，更早子轮次只保留工具结果
    3. 归档态处理：用户轮次结束时，剥离所有思考轨迹与原始思考内容
    4. 关键字段保护：UninstallString 等 JSON 大字段在归档时动态保留完整值

    Args:
        messages: 原始消息列表
        session_id: 会话ID
        preserve_thinking_rounds: 保留思考轨迹的用户轮次数量
        is_current_turn: 是否为当前正在进行的用户轮次（运行态）
        max_reasoning_subturns: 当前轮次内保留完整思考轨迹的子轮次数量（从末尾计数）
    """
    history_entries = []
    current_entry = None
    current_tool_calls = []
    current_tool_result = ""
    last_tool_name = ""
    last_tool_args = {}
    current_child_turns = []

    for m in messages:
        role = m.get("role")
        msg_type = m.get("msg_type", "chat")
        content = m.get("content", "")
        timestamp = m.get("timestamp", "")

        if role == "user" and msg_type == "chat":
            if current_entry:
                _finalize_entry_v2(current_entry, current_tool_calls, last_tool_name, last_tool_args, current_tool_result, current_child_turns)
                history_entries.append(current_entry)

            current_entry = {
                "user_task": content,
                "ai_reply": "",
                "tool_calls": [],
                "task_status": "",
                "subtask_status": "",
                "main_task_status": "",
                "timestamp": timestamp,
                "round_index": len(history_entries),
                "context_summary": "",
                "child_turns": []
            }
            current_tool_calls = []
            current_tool_result = ""
            last_tool_name = ""
            last_tool_args = {}
            current_child_turns = []

        elif role == "assistant":
            # 仅对话类 assistant 消息参与结构化历史：
            # 排除 msg_type="think"（思考摘要）和 "think_step"（前端展示摘要），
            # 否则会污染子轮次记录，并让 ai_reply 抓到思考内容而非真实回复
            if current_entry and msg_type == "final_answer":
                # AI 最终回复（_safe_final 以 msg_type="final_answer" 入库）：
                # 填充当前轮次的 ai_reply，使本轮对话摘要进入 context_memory（近3轮结构化记忆）。
                # 此前 final_answer 被忽略，导致 ai_reply 恒为空——context_memory 从未包含 AI 回复。
                # 每轮仅一条 final_answer（_safe_final 有 final_emitted 守卫），直接覆盖天然幂等。
                current_entry["ai_reply"] = content[:500] if len(content) > 500 else content
            elif current_entry and msg_type in ("chat", "tool_call"):
                tool_name, tool_args = _parse_tool_call(content)
                tool_call_list = _extract_tool_calls(content)
                reasoning_trace = _parse_reasoning_trace(content)

                child_turn_data = {
                    "child_turn": len(current_child_turns),
                    "timestamp": timestamp,
                    "user_input": "",
                    "ai_thinking": "",
                    "ai_output": content,
                    "tool_calls": [],
                    "tool_result": "",
                    "models": "",
                    "elapsed_ms": 0,
                    "reasoning_trace": reasoning_trace or {}
                }

                if tool_call_list:
                    child_turn_data["tool_calls"] = [
                        {"tool": tc["tool_name"], "arguments": tc["arguments"]}
                        for tc in tool_call_list
                    ]
                    for tc in tool_call_list:
                        if last_tool_name and current_tool_result:
                            current_tool_calls.append({
                                "tool_name": last_tool_name,
                                "tool_arguments": last_tool_args,
                                "tool_result": current_tool_result
                            })
                            current_tool_result = ""
                        last_tool_name = tc["tool_name"]
                        last_tool_args = tc["arguments"]
                elif tool_name:
                    child_turn_data["tool_calls"] = [{
                        "tool": tool_name,
                        "arguments": tool_args
                    }]
                    if last_tool_name and current_tool_result:
                        current_tool_calls.append({
                            "tool_name": last_tool_name,
                            "tool_arguments": last_tool_args,
                            "tool_result": current_tool_result
                        })
                        current_tool_result = ""
                    last_tool_name = tool_name
                    last_tool_args = tool_args
                else:
                    child_turn_data["ai_thinking"] = content
                    if not current_entry["ai_reply"]:
                        current_entry["ai_reply"] = content

                current_child_turns.append(child_turn_data)

        elif role == "user" and msg_type == "tool_result":
            if current_entry and current_child_turns:
                current_tool_result = content
                current_child_turns[-1]["tool_result"] = content

        elif role == "system":
            pass

    if current_entry:
        _finalize_entry_v2(current_entry, current_tool_calls, last_tool_name, last_tool_args, current_tool_result, current_child_turns)
        history_entries.append(current_entry)

    for i, entry in enumerate(history_entries):
        rounds_from_end = len(history_entries) - 1 - i
        
        if rounds_from_end > preserve_thinking_rounds:
            entry["child_turns"] = [
                _clean_reasoning_for_archive(ct)
                for ct in entry.get("child_turns", [])
            ]
        elif not is_current_turn and rounds_from_end > 0:
            child_turns = entry.get("child_turns", [])
            new_child_turns = []
            for j, ct in enumerate(child_turns):
                turns_from_end = len(child_turns) - 1 - j
                if turns_from_end <= 1:
                    new_child_turns.append(ct)
                else:
                    new_child_turns.append(_clean_reasoning_for_archive(ct))
            entry["child_turns"] = new_child_turns
        elif is_current_turn and rounds_from_end == 0:
            # 当前轮次：裁剪超过 max_reasoning_subturns 的子轮次思考轨迹
            child_turns = entry.get("child_turns", [])
            for j, ct in enumerate(child_turns):
                turns_from_end = len(child_turns) - 1 - j
                if turns_from_end >= max_reasoning_subturns:
                    ct["reasoning_trace"] = {}
                    ct["ai_thinking"] = ""
                    ct["ai_output"] = _trim_tool_result(
                        ct.get("ai_output", ""), max_len=200
                    )

    return history_entries


def _finalize_entry_v2(entry, tool_calls, last_tool_name, last_tool_args, last_tool_result, child_turns=None):
    if last_tool_name and last_tool_result:
        tool_calls.append({
            "tool_name": last_tool_name,
            "tool_arguments": last_tool_args,
            "tool_result": last_tool_result
        })

    entry["tool_calls"] = tool_calls

    if child_turns:
        entry["child_turns"] = child_turns

    all_success = True
    any_failed = False

    for tc in tool_calls:
        result_str = tc.get("tool_result", "").lower()
        if "fail" in result_str or "失败" in result_str:
            any_failed = True
            all_success = False
        elif not ("success" in result_str or "成功" in result_str):
            all_success = False

    ai_reply = entry.get("ai_reply", "")
    if any_failed:
        entry["task_status"] = "failed"
    elif tool_calls and (all_success or "final_message" in ai_reply or "任务已完成" in ai_reply):
        entry["task_status"] = "completed"
    elif ai_reply:
        entry["task_status"] = "completed"
    else:
        entry["task_status"] = "in_progress"


def _trigger_increment_compression(session_id, history_entries):
    """触发增量合并压缩。
    
    设计要点：
    1. 轮次触发：每完成1个用户轮次，且结构化轮次 > 3时，取出最老1轮数据合并进摘要
    2. 存储统一：写入 summaries.db，与 compress_conversation_async 共用存储
    3. 去重保护：避免同一轮次重复压缩
    
    Args:
        session_id: 会话ID
        history_entries: 结构化历史条目列表
    """
    if len(history_entries) <= 3:
        return

    oldest_round = history_entries[0]
    
    new_summary = increment_merge_summary(session_id, oldest_round)
    if new_summary:
        try:
            cover_end = oldest_round.get("round_index", 1) + 1
            add_summary(session_id, new_summary,
                        cover_turn_start=1,
                        cover_turn_end=cover_end,
                        token_est=estimate_tokens(new_summary))
        except Exception as e:
            print(f"[Memory] 增量压缩保存失败: {e}")


def build_child_turn_context(child_turns: list, keep_reasoning_rounds: int = 2) -> str:
    """构建子轮次增量上下文。
    
    设计要点：
    1. 裁剪规则：近2轮子轮次保留完整思考轨迹，更早子轮次只保留工具结果
    2. 错误统一处理：工具执行失败时，结果统一精简为「错误类型 + 核心原因」，≤100字
    3. 标准化输出：格式化的增量文本，可直接拼入用户消息体
    
    Args:
        child_turns: 当前用户任务内的子轮次历史列表
        keep_reasoning_rounds: 保留完整思考轨迹的子轮次数量
    
    Returns:
        格式化的增量上下文字符串
    """
    if not child_turns:
        return ""
    
    parts = []
    total_child_turns = len(child_turns)
    
    for i, ct in enumerate(child_turns):
        turns_from_end = total_child_turns - 1 - i
        
        tool_calls = ct.get("tool_calls", [])
        tool_result = ct.get("tool_result", "")
        reasoning_trace = ct.get("reasoning_trace", {})
        ai_thinking = ct.get("ai_thinking", "")
        
        ct_parts = [f"【子轮次{i+1}】"]
        
        if turns_from_end < keep_reasoning_rounds:
            if reasoning_trace:
                trace_lines = []
                if reasoning_trace.get("current_intent"):
                    trace_lines.append(f"  核心意图：{reasoning_trace['current_intent']}")
                if reasoning_trace.get("decision_rationale"):
                    trace_lines.append(f"  决策理由：{reasoning_trace['decision_rationale']}")
                if reasoning_trace.get("active_hypothesis"):
                    trace_lines.append(f"  假设预测：{reasoning_trace['active_hypothesis']}")
                if trace_lines:
                    ct_parts.append("\n".join(trace_lines))
            
            if ai_thinking:
                ct_parts.append(f"  思考内容：{ai_thinking[:200]}")
        
        if tool_calls:
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                arguments = tc.get("arguments", {})
                args_preview = json.dumps(arguments, ensure_ascii=False)[:100]
                ct_parts.append(f"  工具调用：{tool_name}(参数={args_preview})")
        
        if tool_result:
            result_str = str(tool_result)
            
            if any(keyword in result_str.lower() for keyword in ["fail", "失败", "error", "错误"]):
                result_str = _trim_tool_result(tool_result, max_len=100)
            elif len(result_str) > 800:
                # 成功结果压缩到800字符，确保关键字段（如 UninstallString）不被截断
                result_str = result_str[:800] + "\n[...结果过长已压缩]"
            
            ct_parts.append(f"  工具结果：{result_str}")
        
        parts.append("\n".join(ct_parts))
    
    return "\n\n".join(parts)
    
    
WORKING_ROUNDS = 3           # 情节缓存保留的用户轮次数
COMPRESS_THRESHOLD = 3       # 超过此轮次触发摘要压缩
KEEP_REASONING_SUBTURNS = 2  # 子轮次思考轨迹保留数


def _build_episodic_buffer(messages, rounds=2):
    """构建紧凑的情节缓存，提取最近 rounds 轮的核心信息。
    
    消息格式与 _build_structured_history_v2 一致：
    - user + chat: 用户任务消息
    - assistant: AI回复（含工具调用或思考）
    - user + tool_result: 工具执行结果
    """
    buffer = []
    current_round = None
    
    for m in messages:
        role = m.get("role")
        msg_type = m.get("msg_type", "chat")
        content = m.get("content", "")
        
        if role == "user" and msg_type == "chat":
            if current_round:
                buffer.append(current_round)
            current_round = {
                "user_task": content[:300],
                "tool_calls": []
            }
        elif role == "assistant" and current_round:
            tool_name, tool_args = _parse_tool_call(content)
            if tool_name:
                current_round["tool_calls"].append({
                    "tool": tool_name,
                    "arguments": tool_args,
                    "result": ""
                })
            # 非工具调用的助手回复（纯文本最终回复）不注入紧凑缓存，
            # 完整回复由 _build_structured_history 的 ai_reply 承担，避免重复注入
        elif role == "user" and msg_type == "tool_result" and current_round:
            result_snippet = content[:400] if len(content) > 400 else content
            if current_round["tool_calls"]:
                current_round["tool_calls"][-1]["result"] = result_snippet
    
    if current_round:
        buffer.append(current_round)
    
    return buffer[-rounds:] if len(buffer) > rounds else buffer


def get_layered_memory(session_id: str, user_message: str = "") -> dict:
    """为 react_engine 子轮次拼接提供分层记忆数据。

    纯读取无副作用，不触发压缩、不做重复查询检测、不构建 context_suffix。

    Returns:
        {
            "long_term": str,              # ① 长期记忆
            "recent_structured": str,      # ② 近三轮结构化记忆（JSON）
            "older_summary": str,          # ③ 超三轮自然语言摘要
            "plan_info": str,              # ⑤ 规划信息
        }
    """
    logger = logging.getLogger(__name__)
    try:
        messages = get_full_session_messages(session_id)
    except Exception:
        messages = []

    # 找到用户消息索引（仅 chat 类型）
    chat_user_indices = [i for i, m in enumerate(messages)
                         if m.get("role") == "user" and m.get("msg_type") == "chat"]
    total_rounds = len(chat_user_indices)

    # ① 长期记忆
    try:
        long_term = get_relevant_memory(user_message) or ""
    except Exception as e:
        logger.warning(f"[get_layered_memory] ① 长期记忆构建失败: {e}")
        long_term = ""

    # ② 近三轮结构化记忆
    recent_structured = ""
    try:
        if total_rounds > 0:
            if total_rounds > WORKING_ROUNDS:
                cutoff_idx = chat_user_indices[-WORKING_ROUNDS]
                episodic_messages = messages[cutoff_idx:]
            else:
                episodic_messages = messages

            episodic_history = _build_structured_history(
                episodic_messages, session_id,
                preserve_thinking_rounds=WORKING_ROUNDS,
                is_current_turn=True,
                max_reasoning_subturns=KEEP_REASONING_SUBTURNS
            )

            recent_structured = json.dumps(episodic_history, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[get_layered_memory] ② 结构化记忆构建失败: {e}")
        recent_structured = ""

    # ③ 超三轮自然语言摘要
    older_summary = ""
    try:
        if total_rounds > WORKING_ROUNDS:
            older_summary = get_latest_summary(session_id) or ""
    except Exception as e:
        logger.warning(f"[get_layered_memory] ③ 摘要构建失败: {e}")
        older_summary = ""

    # ⑤ 规划信息（从工作记忆提取）
    plan_info = ""
    try:
        wm = _get_working_memory(session_id)
        plan = wm.get("plan", [])
        current_step = wm.get("current_step", 0)
        last_observation = wm.get("last_observation", "")

        if plan or last_observation:
            lines = []
            if plan:
                if isinstance(plan, list):
                    lines.append("计划:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan)))
                else:
                    lines.append(f"计划: {plan}")
            if current_step > 0:
                lines.append(f"执行步数: {current_step}")
            if last_observation:
                lines.append(f"最近观察: {str(last_observation)[:300]}")
            plan_info = "\n".join(lines)
    except Exception as e:
        logger.warning(f"[get_layered_memory] ⑤ 规划信息构建失败: {e}")
        plan_info = ""

    return {
        "long_term": long_term,
        "recent_structured": recent_structured,
        "older_summary": older_summary,
        "plan_info": plan_info,
    }


def build_context_suffix(session_id, current_user_input):
    """收集所有动态上下文信息，拼成后缀字符串。

    优先级：长期记忆 → 短期记忆摘要 → 子轮次/执行状态。
    所有动态信息统一拼接到 user message 末尾，保证 system prompt 静态可缓存。

    Args:
        session_id: 会话 ID
        current_user_input: 当前用户输入，用于检索长期记忆

    Returns:
        拼接后的后缀字符串，无动态信息时返回空字符串
    """
    suffix_parts = []

    # 1. 长期记忆（根据 current_user_input 检索相关记忆）
    try:
        long_term_memory = get_relevant_memory(current_user_input)
        if long_term_memory:
            suffix_parts.append(f"\n\n### 长期记忆 ###\n{long_term_memory}")
    except Exception:
        pass

    # 2. 短期记忆摘要（用户轮次 > 3 时启用）
    #    使用 count_chat_messages 估算轮次数，避免引入额外依赖
    try:
        total_msgs = count_chat_messages(session_id)
        # 粗略估算用户轮次：总消息数 / 2（每轮至少包含 user + assistant）
        estimated_rounds = total_msgs // 2
        if estimated_rounds > 3:
            summary = get_latest_summary(session_id)
            if summary:
                suffix_parts.append(f"\n\n### 对话摘要 ###\n{summary}")
    except Exception:
        pass

    # 3. 子轮次/执行状态（当前计划、步骤进度、最近观察）
    working_mem = _get_working_memory(session_id)
    plan = working_mem.get("plan", [])
    current_step = working_mem.get("current_step", 0)
    last_observation = working_mem.get("last_observation", "")

    if plan or last_observation:
        state_lines = []
        if plan:
            state_lines.append(f"当前计划: {json.dumps(plan, ensure_ascii=False, default=str)}")
        if current_step > 0:
            state_lines.append(f"执行步数: {current_step}")
        if last_observation:
            state_lines.append(f"最近观察: {str(last_observation)[:300]}")
        suffix_parts.append(f"\n\n### 执行状态 ###\n" + "\n".join(state_lines))

    return "".join(suffix_parts)


def build_context_messages(session_id, limit=150, summary_max_tokens=SUMMARY_TOKEN_BUDGET,
                           user_message="", is_continuation=False, available_tools=None):
    """构建 LLM 上下文"""
    
    messages = get_full_session_messages(session_id, limit=limit)
    chat_user_indices = [i for i, m in enumerate(messages) 
                         if m.get("role") == "user" and m.get("msg_type") == "chat"]
    total_rounds = len(chat_user_indices)
    
    # --- 1. 重复查询处理 ---
    # 话题切换不由这里判定：react_engine 的 BM25 检测器（context_router）是唯一判定源，
    # 且切换时引擎会清空缓存的短期记忆（cached_short_term_memory=""），
    # 因此这里不再跑第二套相似度检测，避免两套算法结论互相矛盾。
    last_user_msg = user_message
    if not last_user_msg:
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("msg_type") == "chat":
                last_user_msg = m.get("content", "")
                break

    repeat_query_note = ""
    if last_user_msg and is_continuation:
        exclude_indices = _detect_repeated_query(messages, last_user_msg)
        if exclude_indices:
            # 删除前先校验索引是否在当前 messages 范围内（防止切片后旧索引越界）
            valid_exclude = [idx for idx in exclude_indices if idx < len(messages)]
            for idx in sorted(valid_exclude, reverse=True):
                del messages[idx]
            repeat_query_note = "【重复查询】检测到重复提问，已保留历史工具结果，仅需重试失败步骤。"
            chat_user_indices = [i for i, m in enumerate(messages)
                                 if m.get("role") == "user" and m.get("msg_type") == "chat"]
            total_rounds = len(chat_user_indices)
    
    # --- 2. 切割分层：历史摘要(早期) vs 情节缓存(最近N轮) ---
    if total_rounds > WORKING_ROUNDS:
        # 最近3轮 → 情节缓存
        cutoff_idx = chat_user_indices[-WORKING_ROUNDS]
        historical_messages = messages[:cutoff_idx]   # 第1~N-3轮
        episodic_messages = messages[cutoff_idx:]      # 最近3轮
    else:
        historical_messages = []
        episodic_messages = messages
    
    result_messages = []
    
    # --- 3. 构建动态上下文后缀 ---
    # 所有动态信息（长期记忆 / 摘要 / 执行状态）通过后缀注入到 user message 末尾，
    # system prompt 保持静态以最大化 LLM 服务端缓存命中率。
    context_suffix = build_context_suffix(
        session_id, last_user_msg or user_message
    )
    
    # 将重复查询提示放入结果（话题切换由 react_engine BM25 检测器统一判定）
    if repeat_query_note:
        result_messages.append({
            "role": "system",
            "content": repeat_query_note
        })
    
    # --- 4. 动态上下文后缀（拼入 user message，此处作为独立消息返回供调用方拼接） ---
    if context_suffix:
        result_messages.append({
            "role": "context_suffix",
            "content": context_suffix
        })
    
    # 3.5 【历史摘要】（早期轮次的压缩）
    historical_summary = get_latest_summary(session_id)
    if historical_messages:
        historical_summary = get_latest_summary(session_id)
        if historical_summary:
            result_messages.append({
                "role": "system",
                "content": f"【历史摘要】(第1~{total_rounds - WORKING_ROUNDS}轮压缩)\n{historical_summary}"
            })
        
        # 触发增量压缩：超过阈值时，把最老的1轮合并进摘要
        if total_rounds > COMPRESS_THRESHOLD + WORKING_ROUNDS:
            history_entries = _build_structured_history(
                historical_messages, session_id, 
                preserve_thinking_rounds=0,  # 归档态：完全剥离思考轨迹
                is_current_turn=False
            )
            _trigger_increment_compression(session_id, history_entries)
    
    # 4. 情节缓存 (最近3轮，子轮次完整保留，思考轨迹按规则裁剪)
    episodic_history = _build_structured_history(
        episodic_messages, session_id,
        preserve_thinking_rounds=WORKING_ROUNDS,
        is_current_turn=True
    )
    
    # 对每个用户轮次内部的子轮次做思考轨迹裁剪
    for entry in episodic_history:
        child_turns = entry.get("child_turns", [])
        total_subturns = len(child_turns)
        for j, ct in enumerate(child_turns):
            turns_from_end = total_subturns - 1 - j
            if turns_from_end >= KEEP_REASONING_SUBTURNS:
                # 超过2个子轮次的，剥离思考轨迹，仅保留工具名+参数+结果
                ct["reasoning_trace"] = {}
                ct["ai_thinking"] = ""
                ct["ai_output"] = _trim_tool_result(
                    ct.get("ai_output", ""), max_len=200
                )
    
    result_messages.append({
        "role": "system",
        "content": f"【情节缓存 - 最近{WORKING_ROUNDS}轮完整交互】\n"
                   f"说明: 每轮内保留最近{KEEP_REASONING_SUBTURNS}个子轮次的思考轨迹，"
                   f"更早子轮次仅保留工具调用与结果。\n\n"
                   f"{json.dumps(episodic_history, ensure_ascii=False, indent=2)}"
    })
    
    return result_messages


def append_session_message(session_id, role, content,
                           trace_id="", user_id="",
                           skip_compress=False,
                           is_temporary=False,
                           msg_type="chat",
                           msg_id=""):
    content = content.strip()
    if not content:
        return session_id

    if _is_noise(content):
        log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
        return session_id

    db = load_sessions_db()
    if session_id not in db:
        db[session_id] = {
            "id": session_id,
            "title": _make_title(content),
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_sessions_db(db)

    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add_chat_message(session_id, role, content, msg_type=msg_type,
                     trace_id=trace_id, user_id=user_id, timestamp=now_ts,
                     is_temporary=is_temporary, msg_id=msg_id)

    if role == "user":
        stripped = content.strip()
        if len(stripped) >= _FACT_EXTRACT_MIN_LEN and not _is_noise(stripped):
            extract_facts_async(content, trace_id=trace_id, user_id=user_id,
                                session_id=session_id)

        if db[session_id].get("title", "") in ("新会话", "") or \
           (db[session_id].get("title", "") or "").startswith("Session_"):
            db[session_id]["title"] = _make_title(content)
            save_sessions_db(db)

        if not skip_compress:
            messages = get_chat_messages(session_id)
            total_tokens = estimate_messages_tokens(messages)
            if total_tokens > RECENT_TOKEN_MIN:
                threshold_label = "soft" if total_tokens <= RECENT_TOKEN_MAX else "hard"
                log_operation(trace_id, user_id, session_id,
                              "memory.compress.start",
                              f"tokens {total_tokens} > {RECENT_TOKEN_MIN} ({threshold_label})")
                compress_conversation_async(
                    session_id,
                    recent_token_min=RECENT_TOKEN_MIN,
                    recent_token_max=RECENT_TOKEN_MAX,
                    trace_id=trace_id,
                    user_id=user_id,
                )

    log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
    return session_id


def build_step_summary(child_turn_data: dict) -> str:
    """生成前端展示用的子轮次精简摘要。

    规则：
    - 工具调用：仅展示工具名称 + 核心动作，去掉完整参数
    - 纯思考：截断到50字内，去掉代码块和冗余内容
    """
    tool_calls = child_turn_data.get("tool_calls", [])
    ai_thinking = child_turn_data.get("ai_thinking", "").strip()

    if tool_calls:
        tool_names = [tc.get("tool", "未知工具") for tc in tool_calls]
        first_args = tool_calls[0].get("arguments", {})
        action_desc = ""
        if "action" in first_args:
            action_desc = f" - {first_args['action']}"
        elif "query" in first_args:
            action_desc = f" - 查询: {str(first_args['query'])[:20]}"
        elif "file" in first_args:
            action_desc = f" - 目标: {str(first_args['file'])[:20]}"
        elif "command" in first_args:
            action_desc = f" - 命令: {str(first_args['command'])[:20]}"
        return f"调用工具：{', '.join(tool_names)}{action_desc}"

    if ai_thinking:
        clean_thought = re.sub(r"```[\s\S]*?```", "", ai_thinking).strip()
        clean_thought = re.sub(r"\s+", " ", clean_thought)
        if len(clean_thought) > 50:
            clean_thought = clean_thought[:50] + "..."
        return f"思考中：{clean_thought}"

    return "处理中..."


def append_think_step(session_id, content, trace_id="", user_id=""):
    """写入子轮次执行摘要，仅用于前端渲染执行过程。

    不参与对话轮次计数，不混入普通聊天消息。
    """
    content = content.strip()
    if not content:
        return
    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add_chat_message(
        session_id,
        role="assistant",
        content=content,
        msg_type="think_step",
        trace_id=trace_id,
        user_id=user_id,
        timestamp=now_ts,
        is_temporary=False,
    )


def log_session_turn(role, content, trace_id="", user_id="", session_id=""):
    log_operation(trace_id, user_id, session_id,
                  operation=f"chat.{role}", detail=content)


def get_log_dir():
    return LOGS_DIR


def save_ai_log(content: str, filename: str = None):
    if not content.strip():
        return
    if filename is None:
        filename = f"ai_log{datetime.datetime.now().strftime('%Y%m%d')}.md"
    log_path = os.path.join(get_log_dir(), filename)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n**{timestamp}**\n\n{content}\n")