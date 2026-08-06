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
    add_summary, count_chat_messages, get_full_session_messages_rich,
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
        # 兼容普通消息（timestamp）与富消息（created_at）两种来源
        timestamp = m.get("timestamp") or m.get("created_at", "")

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

        elif role in ("user", "tool") and msg_type == "tool_result":
            # 兼容两种存储：新代码 role="tool"，旧代码 role="user"
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


def _infer_tool_result_status(result) -> str:
    """推断单次工具执行结果的状态：success / failed / unknown。

    优先解析 JSON 结构（web_search / search_files 等工具以 role="tool" 存储
    原始结果 JSON），显式检查 success / status / error 字段，避免 naive 关键词
    匹配误判（如 {"success": true, "results": []} 实际是空结果却被判成功）。

    Args:
        result: 工具原始结果（JSON 字符串 / dict / 纯文本）

    Returns:
        "success" / "failed" / "unknown"
    """
    if not result:
        return "unknown"

    data = result
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except (json.JSONDecodeError, TypeError, ValueError):
                data = None  # 解析失败 → 走文本兜底

    if isinstance(data, dict):
        # 显式 success 字段优先
        if data.get("success") is False:
            return "failed"
        if data.get("success") is True:
            # success=true 但无有效结果 → 对下游同样不可用，判 failed
            if _is_empty_result(data):
                return "failed"
            return "success"
        # status 字段
        status = str(data.get("status", "")).lower()
        if status in ("empty", "low_quality", "error", "failed"):
            return "failed"
        if status in ("success", "ok", "completed"):
            return "success"
        # error 类字段
        if data.get("error") or data.get("error_code"):
            return "failed"
        err_msg = str(data.get("message") or "")
        if any(kw in err_msg.lower() for kw in ("失败", "错误", "error", "fail", "不可用", "无法", "exception")):
            return "failed"
        # 有 results/data 列表但为空 → 空结果，判 failed
        if _is_empty_result(data):
            return "failed"
        return "success"

    # 非 JSON 文本：回退到关键词匹配（仅作兜底）
    text = str(result).lower()
    if any(kw in text for kw in ("失败", "错误", "error", "fail", "exception", "\"success\": false")):
        return "failed"
    if any(kw in text for kw in ("成功", "success", "completed", "完成")):
        return "success"
    return "unknown"


def _is_empty_result(data: dict) -> bool:
    """判断工具返回 JSON 是否为「无有效结果」形态（空列表 / filtered_count=0）。

    优先识别明确的非空信号（result/output/message 非空、results 等列表非空），
    仅在所有内容字段都为空时才判空，避免误伤携带 result 字段的成功结果。
    """
    # 明确非空信号：只要有一处内容即视为非空
    for key in ("result", "output", "message"):
        if data.get(key):
            return False
    for key in ("results", "data", "items", "rows"):
        val = data.get(key)
        if isinstance(val, list) and val:
            return False
    # 明确空信号：存在列表字段且为空
    for key in ("results", "data", "items", "rows"):
        if key in data and isinstance(data.get(key), list) and len(data[key]) == 0:
            return True
    # web_search 相关性过滤：filtered_count=0 且无其他内容 → 空结果
    if isinstance(data.get("filtered_count"), (int, float)) and data.get("filtered_count", 0) == 0:
        return True
    return False


_CONFIRM_REQUEST_MARKERS = (
    "请确认", "请您确认", "是否确认", "是否符合您的预期",
    "需要您确认", "需要确认", "等待您的确认", "确认这个", "确认后",
)


def _is_confirmation_request(text: str) -> bool:
    """判断 ai_reply 是否为"展示结果并请求用户确认"类消息。

    依据确认请求轮的真实文案特征（如"请确认这个大纲是否符合您的预期？"、
    "需要确认，您觉得这个大纲结构合适吗？"），命中任一标记即视为确认请求轮。
    标记均为明确的"请求确认"措辞，避免与普通完成回复误判。
    """
    if not text:
        return False
    return any(marker in text for marker in _CONFIRM_REQUEST_MARKERS)


def _compute_elapsed_ms(child_turns):
    """回填 child_turns 的 elapsed_ms：同轮相邻子轮时间差（毫秒，best-effort）。

    时间戳来自消息的 timestamp / created_at；缺失或解析失败时保持 0，绝不抛异常。
    """
    if not child_turns or len(child_turns) < 2:
        return
    prev_ts = child_turns[0].get("timestamp", "")
    for ct in child_turns[1:]:
        curr_ts = ct.get("timestamp", "")
        if prev_ts and curr_ts:
            try:
                t0 = datetime.datetime.strptime(str(prev_ts)[:19], "%Y-%m-%d %H:%M:%S")
                t1 = datetime.datetime.strptime(str(curr_ts)[:19], "%Y-%m-%d %H:%M:%S")
                ct["elapsed_ms"] = max(0, int((t1 - t0).total_seconds() * 1000))
            except (ValueError, TypeError):
                pass
        prev_ts = curr_ts


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
        _compute_elapsed_ms(child_turns)

    # 状态推断：JSON-aware，避免 naive 关键词匹配误判
    all_success = True
    any_failed = False
    any_unknown = False

    for tc in tool_calls:
        status = _infer_tool_result_status(tc.get("tool_result", ""))
        if status == "failed":
            any_failed = True
            all_success = False
        elif status == "unknown":
            all_success = False
            any_unknown = True

    ai_reply = entry.get("ai_reply", "")
    if any_failed:
        entry["task_status"] = "failed"
    elif ai_reply and _is_confirmation_request(ai_reply):
        # 展示结果并请求用户确认：不视为失败/完成，标记为等待确认，
        # 供 _infer_current_focus / 确认信号注入识别"已请求确认"状态
        entry["task_status"] = "waiting_confirmation"
    elif tool_calls and (all_success or "final_message" in ai_reply or "任务已完成" in ai_reply):
        entry["task_status"] = "completed"
    elif ai_reply:
        entry["task_status"] = "completed"
    elif any_unknown and tool_calls:
        entry["task_status"] = "in_progress"
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
        elif role in ("user", "tool") and msg_type == "tool_result" and current_round:
            # 兼容两种存储：新代码 role="tool"，旧代码 role="user"
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
        # 使用富消息（含 created_at 时间戳），供结构化记忆回填 child_turns.elapsed_ms
        messages = get_full_session_messages_rich(session_id)
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


def get_long_term_memory(session_id: str) -> str:
    """获取长期记忆文本（存根封装，若 session_id 无关则返回空）。

    当前长期记忆通过关键词从 Markdown 文件检索，不关联 session_id。
    保留 session_id 参数用于未来向量库扩展。
    """
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    try:
        # 长期记忆通过 get_relevant_memory 按关键词检索，需要用户消息
        # 此处 session_id 仅作未来扩展用，当前返回空
        return ""
    except Exception as e:
        if ctx_log:
            ctx_log.error(f"获取长期记忆失败: {e}", exc_info=True)
        return ""


def get_compressed_summary(session_id: str) -> str:
    """获取最新压缩摘要。"""
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    try:
        summary = get_latest_summary(session_id) or ""
        if ctx_log:
            ctx_log.info(f"压缩摘要长度: {len(summary)} 字符")
        return summary
    except Exception as e:
        if ctx_log:
            ctx_log.warning(f"获取压缩摘要失败，降级: {e}")
        return ""


def get_current_plan(session_id: str) -> str:
    """获取当前任务规划。"""
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    try:
        wm = _get_working_memory(session_id)
        plan = wm.get("plan", [])
        if plan:
            if isinstance(plan, list):
                plan_text = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(plan))
            else:
                plan_text = str(plan)
            if ctx_log:
                ctx_log.info(f"任务规划长度: {len(plan_text)} 字符")
            return plan_text
        return ""
    except Exception as e:
        if ctx_log:
            ctx_log.warning(f"获取任务规划失败，降级: {e}")
        return ""


def get_recent_thinking_trace(session_id: str, rounds: int = 2) -> str:
    """提取最近 N 轮 assistant 消息中的 thinking_summary，拼接返回。"""
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    try:
        records = get_full_session_messages_rich(session_id, limit=100)
        traces = []
        for r in records:
            if r.get("role") == "assistant" and r.get("thinking_summary"):
                traces.append(r["thinking_summary"])
        recent = traces[-rounds:] if len(traces) > rounds else traces
        result = "\n".join(recent)
        if ctx_log:
            ctx_log.info(f"思考轨迹: {len(recent)} 条, {len(result)} 字符")
        return result
    except Exception as e:
        if ctx_log:
            ctx_log.warning(f"获取思考轨迹失败，降级: {e}")
        return ""


def load_recent_rounds(session_id: str, rounds: int = 3) -> list:
    """加载最近 N 轮完整历史（按时间排序的字典列表）。

    每轮包含：一条 user 消息 + 若干 assistant（含 tool_calls）+ 若干 tool 结果。
    从 chat_history 表中最近 30 条消息中按轮次边界切分。
    """
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    try:
        records = get_full_session_messages_rich(session_id, limit=30)
        if not records:
            return []

        # 按轮次边界切分：user 消息作为新轮次开始
        round_groups = []
        current_round = []
        for r in records:
            if r.get("role") == "user" and r.get("msg_type") == "chat":
                if current_round:
                    round_groups.append(current_round)
                current_round = [r]
            elif current_round:
                current_round.append(r)
        if current_round:
            round_groups.append(current_round)

        recent = round_groups[-rounds:] if len(round_groups) > rounds else round_groups
        if ctx_log and recent:
            timestamps = [m.get("created_at", "") for group in recent for m in group if m.get("created_at")]
            ctx_log.info(
                f"加载最近 {len(recent)} 轮: {len(sum(recent, []))} 条消息, "
                f"时间范围 {timestamps[0] if timestamps else 'N/A'} ~ {timestamps[-1] if timestamps else 'N/A'}"
            )
        return sum(recent, [])  # 展平
    except Exception as e:
        if ctx_log:
            ctx_log.error(f"加载最近轮次失败: {e}", exc_info=True)
        return []


def _is_tool_error_content(content) -> bool:
    """判断工具消息内容是否为失败报文（忽略大小写）。

    兼容两种格式：
    1. JSON 报文：含 "success": false / error / fail / exception 关键词
    2. 【上一步执行摘要】文本：含失败标记（失败原因 / ❌ 失败）
    """
    if not content:
        return False
    lower = content.lower()
    if '"success": false' in lower or '"success":false' in lower:
        return True
    for kw in ("error", "fail", "exception"):
        if kw in lower:
            return True
    # 摘要文本中文失败标记（build_step_summary 失败输出）
    if "失败原因" in content:
        return True
    if "❌" in content and "失败" in content:
        return True
    return False


def _extract_traceback_head(traceback_raw, max_lines: int = 3) -> str:
    """提取 traceback 前 max_lines 行。"""
    if not traceback_raw:
        return ""
    lines = str(traceback_raw).splitlines()
    return "\n".join(lines[:max_lines])


def _parse_tool_error_content(content, ctx_log=None):
    """解析工具失败报文，返回 (tool_name, error_msg, suggestion)。

    兼容两种格式：
    1. JSON：提取 error / message / traceback（traceback 只取前 3 行）
    2. 【上一步执行摘要】文本：提取工具 / 失败原因 / 重要提示首条
    """
    content = (content or "").strip()
    if not content:
        return ("", "", "")

    stripped = content.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                tool_name = str(data.get("tool_name") or data.get("tool") or data.get("name") or "").strip()
                error = data.get("error") or ""
                if isinstance(error, dict):
                    error = error.get("message") or str(error)
                message = data.get("message") or ""
                traceback = _extract_traceback_head(data.get("traceback") or data.get("stack_trace") or "")
                error_msg = str(error or message or traceback).strip()
                suggestion = str(data.get("suggestion") or data.get("advice") or data.get("建议") or "").strip()
                return (tool_name, error_msg, suggestion)
        except (json.JSONDecodeError, TypeError, ValueError):
            if ctx_log:
                ctx_log.warning("get_last_tool_error JSON 解析失败，回退原始文本: %s", content[:200])

    # 【上一步执行摘要】文本 / 原文
    tool_name = ""
    m = re.search(r"工具\s*[:：]\s*([^\n]+)", content)
    if m:
        tool_name = m.group(1).strip()
    m = re.search(r"失败原因\s*[:：]\s*([^\n]+)", content)
    if m:
        error_msg = m.group(1).strip()
    else:
        error_msg = content
    suggestion = ""
    m = re.search(r"重要提示[:：]?\s*\n?\s*-?\s*([^\n]+)", content)
    if m:
        suggestion = m.group(1).strip()
    return (tool_name, error_msg, suggestion)


def _format_tool_error(tool_name, error_msg, suggestion, max_len) -> str:
    """组装精简报错文本，总长度强制截断至 max_len 字符。"""
    lines = ["【上一轮工具报错】"]
    if tool_name:
        lines.append(f"· 工具: {tool_name}")
    lines.append(f"· 错误: {error_msg}")
    if suggestion:
        lines.append(f"· 建议: {suggestion}")
    text = "\n".join(lines)
    if len(text) > max_len:
        suffix = "...（已截断）"
        text = text[:max_len - len(suffix)] + suffix
    return text


def get_last_tool_error(session_id: str, max_len: int = 500) -> str:
    """获取最近一轮工具执行报错（精简版），用于拼入 user 消息。

    轮次边界：role=='user' 且 msg_type=='chat' 作为一轮开始；
    仅取最近一轮内的 tool 消息，按时间倒序取最近一条失败消息。
    无匹配返回空字符串；数据库异常降级返回空字符串。
    """
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    if not session_id:
        return ""

    try:
        import sqlite3 as _sqlite3
        from memory.config import get_session_messages_db
        db_path = get_session_messages_db(session_id)
        if not os.path.exists(db_path):
            if ctx_log:
                ctx_log.info("get_last_tool_error: 会话无消息库，未检测到报错")
            return ""

        conn = _sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            # 1. 定位最近一轮边界：最后一条 user chat 消息
            row = conn.execute(
                "SELECT id FROM chat_history "
                "WHERE session_id = ? AND role = 'user' AND msg_type = 'chat' AND is_temporary = 0 "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                if ctx_log:
                    ctx_log.info("get_last_tool_error: 无用户轮次，未检测到报错")
                return ""
            boundary_id = row[0]

            # 2. 取该轮内 tool 消息，按时间倒序（created_at 精度不足时以 id 兜底）
            rows = conn.execute(
                "SELECT content, created_at FROM chat_history "
                "WHERE session_id = ? AND role = 'tool' AND id > ? AND is_temporary = 0 "
                "ORDER BY created_at DESC, id DESC",
                (session_id, boundary_id),
            ).fetchall()
        finally:
            conn.close()

        # 3. 找最近一条失败消息
        failed_content = ""
        for content, _created_at in rows:
            if _is_tool_error_content(content):
                failed_content = content
                break
        if not failed_content:
            if ctx_log:
                ctx_log.info("get_last_tool_error: 最近一轮未检测到工具失败消息")
            return ""

        if ctx_log:
            ctx_log.debug("get_last_tool_error 原始报文: %s", failed_content[:200])

        # 4. 解析并组装精简输出
        tool_name, error_msg, suggestion = _parse_tool_error_content(failed_content, ctx_log)
        if not error_msg:
            return ""
        text = _format_tool_error(tool_name, error_msg, suggestion, max_len)
        if ctx_log:
            ctx_log.info("get_last_tool_error 提取到报错: tool=%s len=%d", tool_name or "未知", len(text))
        return text
    except Exception as e:
        if ctx_log:
            ctx_log.error("get_last_tool_error 数据库查询异常，返回空串: %s", e, exc_info=True)
        return ""


def build_dynamic_system_prompt(session_id: str = "", persona_mode: str = None) -> str:
    """构建系统提示词：静态部分 + 动态上下文。

    静态部分：core.md / output.md / safety.md
    动态部分：长期记忆 / 压缩摘要 / 任务规划 / 思考轨迹

    返回格式：
        {静态内容}

        ==== 动态上下文（本轮生效） ====

        {动态内容}
    """
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    # 1. 加载静态提示词
    static_parts = []
    prompt_files = {
        "core": "prompts/core.md",
        "output": "prompts/output.md",
        "safety": "prompts/safety.md",
    }

    for label, rel_path in prompt_files.items():
        try:
            file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel_path)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                static_parts.append(content)
            else:
                if ctx_log:
                    ctx_log.warning(f"提示词文件缺失: {rel_path}，使用默认内容")
                # 硬编码默认文本
                defaults = {
                    "core": "你是功能强大的 AI 桌面助手。请用中文回复，简洁高效。",
                    "output": "输出使用 Markdown 格式化，禁止使用表情符号。",
                    "safety": "安全规则：禁止执行危险操作，数据保护优先。",
                }
                static_parts.append(defaults.get(label, ""))
        except Exception as e:
            if ctx_log:
                ctx_log.error(f"读取提示词文件失败: {rel_path}: {e}", exc_info=True)
            static_parts.append("")

    static_content = "\n\n".join(p for p in static_parts if p)

    # 2. 构建动态内容
    dynamic_blocks = []

    if session_id:
        try:
            long_term = get_long_term_memory(session_id)
            if long_term:
                dynamic_blocks.append(f"【长期记忆】\n{long_term}")
                if ctx_log:
                    ctx_log.info(f"长期记忆: {len(long_term)} 字符")
            else:
                if ctx_log:
                    ctx_log.warning("长期记忆为空")
        except Exception as e:
            if ctx_log:
                ctx_log.warning(f"长期记忆获取失败: {e}")

        try:
            summary = get_compressed_summary(session_id)
            if summary:
                dynamic_blocks.append(f"【历史摘要】\n{summary}")
        except Exception as e:
            if ctx_log:
                ctx_log.warning(f"历史摘要获取失败: {e}")

        try:
            plan = get_current_plan(session_id)
            if plan:
                dynamic_blocks.append(f"【当前任务规划】\n{plan}")
        except Exception as e:
            if ctx_log:
                ctx_log.warning(f"任务规划获取失败: {e}")

        try:
            thinking = get_recent_thinking_trace(session_id, rounds=2)
            if thinking:
                dynamic_blocks.append(f"【上一轮思考摘要】\n{thinking}")
        except Exception as e:
            if ctx_log:
                ctx_log.warning(f"思考轨迹获取失败: {e}")

    dynamic_content = "\n\n".join(dynamic_blocks)

    # 3. 拼接：有 session_id 时始终输出分隔符（即使动态内容为空），保证前缀一致、缓存可复用
    if session_id:
        return f"{static_content}\n\n==== 动态上下文（本轮生效） ====\n\n{dynamic_content}"
    return static_content


def build_context_suffix(session_id, current_user_input):
    """向后兼容存根。

    旧代码将动态上下文拼入 user 消息末尾，重构后改为方案 B
    （System Prompt 动态段），动态内容不再注入 user 消息。
    保留函数签名不变，返回空字符串。
    """
    return ""


def build_context_messages(session_id, user_query="", persona_mode=None,
                           limit=150, summary_max_tokens=SUMMARY_TOKEN_BUDGET,
                           is_continuation=False, available_tools=None):
    """构建发送给 LLM 的完整 messages 数组（方案 B：API 规范）。

    双模式：
    1. user_query 为空 → 仅触发压缩副作用（react_engine 遗留兼容），返回空列表
    2. user_query 非空 → 构建标准 API 格式 messages 数组

    API 模式返回格式：
    [
      {"role": "system", "content": "<静态 + 动态 System Prompt>"},
      {"role": "user", "content": "..."},        # 历史 user
      {"role": "assistant", "content": ..., "tool_calls": [...]},  # 历史 assistant
      {"role": "tool", "tool_call_id": "...", "content": "..."},   # 工具结果
      ...
      {"role": "user", "content": "<user_query>"},  # 当前用户消息
    ]
    """
    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    # ── 模式 1：无用户查询，仅触发压缩副作用 ──
    if not user_query:
        try:
            messages = get_full_session_messages(session_id, limit=limit)
            chat_user_indices = [i for i, m in enumerate(messages)
                                 if m.get("role") == "user" and m.get("msg_type") == "chat"]
            total_rounds = len(chat_user_indices)

            if total_rounds > WORKING_ROUNDS:
                cutoff_idx = chat_user_indices[-WORKING_ROUNDS]
                historical_messages = messages[:cutoff_idx]

                if total_rounds > COMPRESS_THRESHOLD + WORKING_ROUNDS:
                    history_entries = _build_structured_history(
                        historical_messages, session_id,
                        preserve_thinking_rounds=0,
                        is_current_turn=False
                    )
                    _trigger_increment_compression(session_id, history_entries)
        except Exception as e:
            if ctx_log:
                ctx_log.error(f"压缩副作用触发失败: {e}", exc_info=True)
        return []

    # ── 模式 2：构建 API 兼容 messages 数组 ──
    result = []

    # 1. System Prompt（静态 + 动态）
    try:
        system_content = build_dynamic_system_prompt(session_id, persona_mode)
        if ctx_log:
            ctx_log.info(f"System Prompt 长度: {len(system_content)} 字符")
    except Exception as e:
        if ctx_log:
            ctx_log.error(f"构建 System Prompt 失败: {e}", exc_info=True)
        # 降级：仅静态内容
        system_content = build_dynamic_system_prompt("", None)
    result.append({"role": "system", "content": system_content})

    # 2. 历史消息（最近 3 轮完整历史）
    try:
        recent_records = load_recent_rounds(session_id, rounds=3)
        if ctx_log:
            ctx_log.debug(f"历史消息: {len(recent_records)} 条")
    except Exception as e:
        if ctx_log:
            ctx_log.error(f"加载历史消息失败: {e}", exc_info=True)
        recent_records = []

    for record in recent_records:
        role = record.get("role", "")
        content = record.get("content", "")

        if role == "user":
            result.append({"role": "user", "content": content})

        elif role == "assistant":
            tool_calls_raw = record.get("tool_calls", "")
            thinking_summary = record.get("thinking_summary", "") or None

            if tool_calls_raw:
                # 解析 tool_calls JSON
                try:
                    tool_calls = json.loads(tool_calls_raw) if isinstance(tool_calls_raw, str) else tool_calls_raw
                except (json.JSONDecodeError, TypeError):
                    if ctx_log:
                        ctx_log.warning(f"tool_calls JSON 解析失败，跳过: {tool_calls_raw[:100]}")
                    tool_calls = None

                if tool_calls:
                    result.append({
                        "role": "assistant",
                        "content": thinking_summary,  # can be None per API spec
                        "tool_calls": tool_calls,
                    })
                else:
                    result.append({"role": "assistant", "content": content})
            else:
                result.append({"role": "assistant", "content": content})

        elif role == "tool":
            tool_call_id = record.get("tool_call_id", "")
            result.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })

        # system / context_suffix / think_step 等角色跳过（不进入 API messages）

    # 3. 当前用户消息（模式2：拼入上一轮工具报错精简上下文，位于用户问题之前以 --- 分隔）
    error_context = get_last_tool_error(session_id)
    if error_context:
        user_content = f"{error_context}\n\n---\n\n{user_query}"
    else:
        user_content = user_query
    result.append({"role": "user", "content": user_content})

    if ctx_log:
        ctx_log.info(
            f"build_context_messages 完成: "
            f"历史 {len(recent_records)} 条 → {len(result)} 条 API 消息"
        )

    return result


def append_session_message(session_id, role, content,
                           trace_id="", user_id="",
                           skip_compress=False,
                           is_temporary=False,
                           msg_type="chat",
                           msg_id="",
                           tool_call_id="",
                           tool_calls="",
                           thinking_summary=""):
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
                     is_temporary=is_temporary, msg_id=msg_id,
                     tool_call_id=tool_call_id,
                     tool_calls=tool_calls,
                     thinking_summary=thinking_summary)

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