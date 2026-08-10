"""
对话压缩器：Token 预算制 + 本地模型（Ollama）+ per-session 摘要存储

v4：
  - 摘要写入 per-session summaries.db（替代旧 summaries.md）
  - 使用 YAML 配置的本地模型
  - 反向累加 Token 确定切割点
  - 保留最近 N 轮完整原文（可配置）
  - 递归摘要：新旧合并
"""
import json
import re
import threading
from typing import List, Dict, Any
from memory.config import (
    COMPRESS_MODEL_CONFIG, TOKEN_SOFT_THRESHOLD, TOKEN_HARD_THRESHOLD,
    TOKEN_TARGET, RECENT_TURNS_KEEP, MAX_SUMMARY_CHARS, SUMMARY_MODEL,
    RECENT_TOKEN_MIN, RECENT_TOKEN_MAX, SUMMARY_TOKEN_BUDGET,
)
from memory.llm import call_llm_with_config
from memory.stores.sqlite_store import (
    _get_chat_messages_with_ids,
    delete_chat_messages_before,
    add_summary,
    get_latest_summary,
    log_operation,
)
from logger import get_logger


# ── Token 估算 ──

def estimate_tokens(text):
    """估算 token 数量：CJK 字符 ~1.5 token/字，ASCII ~0.25 token/字。"""
    if not text:
        return 0
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    ascii_count = len(text) - cjk_count
    # CJK: ~1.5 tokens/char, ASCII: ~0.25 tokens/char, floor at 1
    return max(1, int(cjk_count * 1.5 + ascii_count * 0.25))


def estimate_messages_tokens(messages):
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", "")) + 4
    return total


# ── 摘要提示词 ──

SUMMARY_PROMPT_TEMPLATE = (
    "你是一个对话压缩助手。请用简洁中文（≤{max_len}字）总结以下对话。\n"
    "必须保留：未解决的任务、用户偏好/否定指令、关键参数值、已确认的技术方案。\n"
    "省略：纯闲聊内容（问候/谢谢/表情等），简短确认语。\n"
    "{old_summary}"
    "对话内容：\n{dialogue}"
)


# ═══════════════════════════════════════════════
# 结构化摘要（增量合并链路专用）
# ═══════════════════════════════════════════════
# 设计目标：关键参数（工作目录/路径/参数值）由代码确定性提取、不经过 LLM
# 压缩；仅非必要内容（任务状态/未完成任务/决策/工具轨迹简述）交给 LLM 合并。

# 结构化摘要必要字段白名单：这些字段在注入 LLM 前不允许被截断/改写
STRUCTURED_SUMMARY_KEYS = (
    "key_context",        # 工作目录/关键路径/关键参数（代码提取，原样保留）
    "tasks",              # 用户任务列表（user_task 原文保留，仅 ai_reply 压缩）
    "unfinished_tasks",   # 未完成任务（不压缩）
    "user_forbidden",     # 用户否定/偏好指令（不压缩）
)

# 从工具参数中识别"路径类"参数的键名（用于提取 key_paths）
_PATH_ARG_KEYS = ("file_path", "path", "working_dir", "cwd", "target_path",
                  "source_path", "dest_path", "output_path", "input_path",
                  "directory", "folder", "dir")

# 从工具参数中识别"工作目录"参数的键名
_WORKDIR_ARG_KEYS = ("working_dir", "cwd", "work_dir", "directory", "folder", "dir")


def _is_path_like(value) -> bool:
    """粗略判断一个字符串是否像路径（含盘符/反斜杠/正斜杠/常见目录词）。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    if len(v) > 512:
        return False
    if ":" in v and ("\\" in v or "/" in v):
        return True
    if v.startswith(("C:\\", "D:\\", "E:\\", "F:\\", "C:/", "D:/", "E:/", "F:/", "/", "~/")):
        return True
    return False


def _extract_structured_fields(new_round_data: dict) -> dict:
    """从单轮结构化数据中确定性提取必要字段（不经过 LLM，不压缩）。

    返回值结构：
    {
        "key_context": {"work_dir": str, "key_paths": [str], "key_params": {}},
        "tasks": [{"user_task": str, "ai_reply_brief": str, "status": str}],
        "unfinished_tasks": [str],
        "user_forbidden": [str],
        "decisions": [str],
        "tool_trace_brief": str,
    }
    """
    key_paths: list = []
    work_dir: str = ""
    key_params: dict = {}

    # 1) 用户任务原文（放大上限，避免长路径/长指令被截断）
    user_task = str(new_round_data.get("user_task", "") or "")[:3000]
    ai_reply = str(new_round_data.get("ai_reply", "") or "")[:300]
    status = str(new_round_data.get("status", new_round_data.get("task_status", "")) or "")

    # 2) 工具调用：提取路径类参数与工作目录
    tool_lines = []
    for tc in new_round_data.get("tool_calls", []) or []:
        if not isinstance(tc, dict):
            continue
        tool_name = tc.get("tool_name", "") or ""
        args = tc.get("tool_arguments", {}) or {}
        result = tc.get("tool_result", "") or ""
        if not isinstance(args, dict):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(result, str):
            result = str(result)

        # 提取 work_dir
        for k in _WORKDIR_ARG_KEYS:
            if args.get(k):
                cand = str(args[k])
                if _is_path_like(cand) or cand.startswith("C:") or cand.startswith("D:"):
                    work_dir = cand
                    break
        # 提取路径类参数
        for k, v in args.items():
            if k in _PATH_ARG_KEYS and _is_path_like(v):
                if v not in key_paths:
                    key_paths.append(v)
            elif k not in _WORKDIR_ARG_KEYS and isinstance(v, str):
                # 命令/字符串参数：提取引号内的路径片段（如 'C:\Program Files\a.exe'）
                for cand in re.findall(r"['\"]([^'\"]+)['\"]", v):
                    if _is_path_like(cand) and cand not in key_paths:
                        key_paths.append(cand)
                # 参数本身整体是路径也保留
                if _is_path_like(v) and v not in key_paths:
                    key_paths.append(v)
        # 关键参数：记录该工具全部参数（不做截断，通常量级可控）
        if tool_name:
            key_params[tool_name] = args
            tool_lines.append(f"{tool_name}({json.dumps(args, ensure_ascii=False)})"
                              f" -> {result[:200]}")

    return {
        "key_context": {
            "work_dir": work_dir,
            "key_paths": key_paths,
            "key_params": key_params,
        },
        "tasks": [{
            "user_task": user_task,
            "ai_reply_brief": ai_reply,
            "status": status,
        }],
        "unfinished_tasks": [],
        "user_forbidden": [],
        "decisions": [],
        "tool_trace_brief": "\n".join(tool_lines)[:2000],
    }


def _parse_structured_summary(text: str):
    """容错解析结构化摘要 JSON 文本。

    支持：纯 JSON、```json 围栏包裹、前后混有说明文字。
    解析失败返回 None（调用方负责回退）。
    """
    if not text:
        return None
    raw = str(text).strip()
    # 剥离 ```json ... ``` 围栏
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    # 提取第一个 { 到最后一个 } 之间的内容
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _legacy_summary_to_struct(old_summary: str) -> dict:
    """将旧的自然语言摘要（或任意无法解析的文本）转换为结构化外壳。

    旧摘要信息不丢失：放入 tool_trace_brief 供后续 LLM 合并参考；
    若已是结构化 JSON 则原样返回。
    """
    parsed = _parse_structured_summary(old_summary)
    if parsed is not None:
        return parsed
    return {
        "key_context": {"work_dir": "", "key_paths": [], "key_params": {}},
        "tasks": [],
        "unfinished_tasks": [],
        "user_forbidden": [],
        "decisions": [],
        "tool_trace_brief": str(old_summary)[:2000] or "",
    }


# 结构化摘要合并提示词：要求输出 JSON，且必须原样保留 key_context 全部字段
STRUCTURED_SUMMARY_PROMPT_TEMPLATE = (
    "你是对话压缩助手。请把【之前摘要】与【新对话】合并，输出一份结构化 JSON 摘要。\n"
    "硬性要求：\n"
    "1. 只输出 JSON，不要输出任何解释文字，不要使用 Markdown 围栏。\n"
    "2. 必须包含字段：key_context / tasks / unfinished_tasks / user_forbidden / decisions / tool_trace_brief。\n"
    "3. key_context 中的 work_dir、key_paths、key_params 必须【原样保留】全部值，禁止删减、改写、省略任何路径或参数。\n"
    "4. tasks 数组：保留每条 user_task 原文（不得截断），ai_reply_brief 可压缩为一行。\n"
    "5. unfinished_tasks / user_forbidden 如有新增必须合并进去，不得丢失旧条目。\n"
    "6. decisions 记录已确认的技术方案；tool_trace_brief 压缩为简要工具轨迹。\n"
    "7. 若之前摘要是自然语言旧格式，请将其中的关键路径/参数/任务信息吸收到对应字段。\n"
    "{old_summary_block}"
    "新对话：\n{dialogue}"
)


def _merge_with_fallback(old_struct: dict, new_struct: dict, parsed):
    """合并结构化摘要；parsed 为 LLM 输出（可为 None）。

    必要字段（key_context / tasks.user_task / unfinished_tasks / user_forbidden）
    以确定性数据为准：LLM 输出只允许补充，不允许删改。
    解析失败（parsed=None）时退化为纯代码拼接，保证关键信息不丢。
    """
    merged = {
        "key_context": dict(old_struct.get("key_context") or {}),
        "tasks": list(old_struct.get("tasks") or []),
        "unfinished_tasks": list(old_struct.get("unfinished_tasks") or []),
        "user_forbidden": list(old_struct.get("user_forbidden") or []),
        "decisions": list(old_struct.get("decisions") or []),
        "tool_trace_brief": str(old_struct.get("tool_trace_brief") or ""),
    }

    # 1) key_context：确定性字段强制覆盖（LLM 不得删改路径/参数）
    #    注意：key_paths 必须做并集而非整体覆盖，否则旧路径丢失
    old_kc = merged["key_context"]
    new_kc = dict(new_struct.get("key_context") or {})
    old_paths = list(old_kc.get("key_paths") or [])
    new_paths = list(new_kc.get("key_paths") or [])
    old_kc["key_paths"] = old_paths + [p for p in new_paths if p not in old_paths]
    for k, v in new_kc.items():
        if k == "key_paths":
            continue
        old_kc[k] = v

    # 2) tasks：新轮次 user_task 原文覆盖同任务条目；新任务追加
    for t in (new_struct.get("tasks") or []):
        ut = t.get("user_task")
        found = False
        for i, x in enumerate(merged["tasks"]):
            if x.get("user_task") == ut:
                merged["tasks"][i] = t
                found = True
                break
        if not found:
            merged["tasks"].append(t)

    # 3) unfinished_tasks / user_forbidden / decisions：LLM 输出可用则合并补充
    for key in ("unfinished_tasks", "user_forbidden", "decisions"):
        llm_vals = (parsed.get(key) or []) if isinstance(parsed, dict) else []
        if isinstance(llm_vals, list):
            for v in llm_vals:
                if v not in merged[key]:
                    merged[key].append(v)
        for v in (new_struct.get(key) or []):
            if v not in merged[key]:
                merged[key].append(v)

    # 4) tool_trace_brief：LLM 压缩版优先，失败则代码拼接
    if isinstance(parsed, dict) and parsed.get("tool_trace_brief"):
        merged["tool_trace_brief"] = str(parsed["tool_trace_brief"])[:2000]
    else:
        old_trace = str(merged.get("tool_trace_brief") or "")
        new_trace = str(new_struct.get("tool_trace_brief") or "")
        merged["tool_trace_brief"] = (old_trace + "\n" + new_trace).strip()[:2000]

    return merged


# ── 防并发锁 ──
_compression_locks = {}
_compression_lock_timestamps = {}
_LOCK_CLEANUP_INTERVAL = 300
_LOCK_EXPIRE_SECONDS = 3600


def _cleanup_expired_locks():
    import time
    now = time.time()
    expired = []
    for session_id, last_used in _compression_lock_timestamps.items():
        if now - last_used > _LOCK_EXPIRE_SECONDS:
            expired.append(session_id)
    for session_id in expired:
        try:
            del _compression_locks[session_id]
            del _compression_lock_timestamps[session_id]
        except KeyError:
            pass
    _compression_lock_timestamps["_last_cleanup"] = now


# ═══════════════════════════════════════════════
# 摘要生成
# ═══════════════════════════════════════════════

def _generate_summary(messages, old_summary="", max_len=MAX_SUMMARY_CHARS):
    if not messages and not old_summary:
        return ""
    # Token优化：不序列化完整 JSON，只提取 role + content 拼成自然对话，去掉 tool_calls 等噪音
    dialogue_parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if content:
            dialogue_parts.append(f"[{role}]: {str(content)[:500]}")
    dialogue = "\n".join(dialogue_parts)[:3000]
    old_block = f"【之前摘要】{old_summary[-2000:]}\n" if old_summary else ""
    system_prompt = {
        "role": "system",
        "content": SUMMARY_PROMPT_TEMPLATE.format(
            max_len=max_len,
            old_summary=old_block,
            dialogue=dialogue,
        ),
    }
    # 省略 role=user 正文，已包含在 system prompt 中
    resp = call_llm_with_config(
        [system_prompt, {"role": "user", "content": "请生成摘要"}],
        COMPRESS_MODEL_CONFIG,
    )
    if resp and "message" in resp:
        content = resp["message"]["content"].strip()
        content = re.sub(r'<\|end[_ ]of[_ ]thinking\|>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<\|begin[_ ]of[_ ]thinking\|>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<\|.*?\|>', '', content)
        content = content.strip()

        if SUMMARY_TOKEN_BUDGET > 0:
            while estimate_tokens(content) > SUMMARY_TOKEN_BUDGET and len(content) > 100:
                content = content[:int(len(content) * 0.9)].strip()
                if content and not content.endswith(('。', '!', '?', '；', ';')):
                    content = content.rsplit('。', 1)[0] + '。'

        return content
    return ""


def increment_merge_summary(session_id, new_round_data, max_len=MAX_SUMMARY_CHARS):
    """增量合并摘要（结构化版）：接收旧摘要 + 单轮结构化数据，输出结构化 JSON 摘要。

    设计要点：
    1. 关键参数不压缩：key_context（工作目录/路径/参数）由代码确定性提取，
       不经过 LLM，原样保留；只有非必要内容（任务状态/工具轨迹简述）交给 LLM 合并。
    2. 兼容旧格式：旧摘要若是自然语言（无法解析为 JSON），转入 tool_trace_brief
       供 LLM 吸收，不丢失信息。
    3. 失败回退：LLM 输出解析失败时，返回 旧摘要 + 新轮次必要字段 的代码拼接结果，
       保证关键信息不丢。
    4. 统一存储：写入 summaries.db，与 compress_conversation_async 共用存储。

    Args:
        session_id: 会话ID
        new_round_data: 单轮结构化数据，包含 user_task、ai_reply、tool_calls
        max_len: 摘要最大字符数（兼容参数，结构化摘要主要受字段白名单控制）

    Returns:
        合并后的结构化摘要 JSON 字符串
    """
    old_summary = get_latest_summary(session_id) or ""
    old_struct = _legacy_summary_to_struct(old_summary)

    # 1) 确定性提取新轮次必要字段（不压缩）
    new_struct = _extract_structured_fields(new_round_data)

    # 2) 组装对话块：旧结构化摘要（JSON）+ 新轮次字段
    old_block = f"【之前摘要】\n{json.dumps(old_struct, ensure_ascii=False)}\n"
    dialogue = json.dumps(new_struct, ensure_ascii=False)

    system_prompt = {
        "role": "system",
        "content": STRUCTURED_SUMMARY_PROMPT_TEMPLATE.format(
            old_summary_block=old_block,
            dialogue=dialogue,
        ),
    }

    resp = call_llm_with_config(
        [system_prompt, {"role": "user", "content": "请合并为结构化摘要 JSON"}],
        COMPRESS_MODEL_CONFIG,
    )

    if resp and "message" in resp:
        content = resp["message"]["content"].strip()
        content = re.sub(r'<\|end[_ ]of[_ ]thinking\|>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<\|begin[_ ]of[_ ]thinking\|>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<\|.*?\|>', '', content)
        content = content.strip()

        parsed = _parse_structured_summary(content)
        if parsed is not None:
            # 3) 校验必要字段完整：缺失时用确定性字段兜底
            merged = _merge_with_fallback(old_struct, new_struct, parsed)
            return json.dumps(merged, ensure_ascii=False)

    # 4) LLM 失败回退：代码合并旧摘要 + 新必要字段，保证关键信息不丢
    fallback = _merge_with_fallback(old_struct, new_struct, None)
    return json.dumps(fallback, ensure_ascii=False)


# ═══════════════════════════════════════════════
# Token 预算制切割
# ═══════════════════════════════════════════════

def _find_cut_point(messages, target_tokens):
    """反向累加 token，找到保留起点。保留最近 N 轮完整对话不动，但受硬上限约束。"""
    if not messages:
        return len(messages)

    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if not user_indices:
        return len(messages)

    keep_turns = min(RECENT_TURNS_KEEP, len(user_indices))
    last_user_idx = user_indices[-keep_turns] if keep_turns > 0 else len(messages)

    while keep_turns > 0:
        preserved_tokens = sum(
            estimate_tokens(messages[i].get("content", "")) + 4
            for i in range(last_user_idx, len(messages))
        )
        if preserved_tokens <= target_tokens * 0.6:
            break
        if keep_turns == 1:
            break
        keep_turns -= 1
        last_user_idx = user_indices[-keep_turns]

    if keep_turns >= len(user_indices):
        return len(messages)

    accumulated = 0
    cut_i = len(messages)

    for i in range(len(messages) - 1, -1, -1):
        accumulated += estimate_tokens(messages[i].get("content", "")) + 4
        if accumulated >= target_tokens:
            if i <= last_user_idx:
                cut_i = i
                break
            elif keep_turns == 1:
                cut_i = last_user_idx
                break

    return cut_i


# ═══════════════════════════════════════════════
# 异步压缩
# ═══════════════════════════════════════════════

def compress_conversation_async(session_id, recent_token_min=RECENT_TOKEN_MIN,
                                 recent_token_max=RECENT_TOKEN_MAX,
                                 summary_model=None, trace_id="", user_id=""):
    import time
    now = time.time()
    last_cleanup = _compression_lock_timestamps.get("_last_cleanup", 0)
    if now - last_cleanup > _LOCK_CLEANUP_INTERVAL:
        _cleanup_expired_locks()

    _compression_lock_timestamps[session_id] = now

    def _run():
        lock = _compression_locks.setdefault(session_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            messages = _get_chat_messages_with_ids(session_id)
            if not messages:
                return

            # 对话轮次少于 3 轮时不触发摘要压缩，避免过早消费 LLM 调用
            user_turns = sum(1 for m in messages if m.get("role") == "user")
            if user_turns < 3:
                return

            total_tokens = estimate_messages_tokens(messages)
            if total_tokens <= recent_token_min:
                return

            # 读取旧摘要（递归合并）
            old_summary = get_latest_summary(session_id)

            # 找到切割点
            cut_i = _find_cut_point(messages, TOKEN_TARGET)
            if cut_i >= len(messages):
                return

            old_msgs = messages[:cut_i]
            if not old_msgs:
                return

            # 生成合并摘要
            summary = _generate_summary(old_msgs, old_summary=old_summary)
            if not summary:
                return

            user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
            cover_end = 0
            for ui in user_indices:
                if messages[ui]["id"] < messages[cut_i]["id"]:
                    cover_end += 1
                else:
                    break

            summary_token_est = estimate_tokens(summary)

            try:
                add_summary(session_id, summary,
                            cover_turn_start=1,
                            cover_turn_end=cover_end,
                            token_est=summary_token_est)

                cutoff_id = messages[cut_i]["id"]
                delete_chat_messages_before(session_id, cutoff_id)
            except Exception as del_e:
                print(f"[Memory] 压缩事务失败，回滚摘要: {del_e}")
                from memory.stores.sqlite_store import get_session_summaries_db
                import sqlite3
                try:
                    db_path = get_session_summaries_db(session_id)
                    conn = sqlite3.connect(db_path, timeout=5)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "DELETE FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                        (session_id,)
                    )
                    conn.commit()
                    conn.close()
                except Exception as rollback_e:
                    print(f"[Memory] 摘要回滚失败: {rollback_e}")
                raise

            log_operation(trace_id, user_id, session_id,
                          "memory.compress.done",
                          f"tokens {total_tokens}→{summary_token_est} | {summary[:80]}")
            print(f"[Memory] 会话 {session_id[:12]} 压缩完成 "
                  f"(token {total_tokens}→{summary_token_est})")

        except Exception as e:
            print(f"[Memory] 异步压缩失败: {e}")
        finally:
            try:
                lock.release()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True,
                         name=f"mem-compress-{session_id[:8]}")
    t.start()


# ═══════════════════════════════════════════════
# 同步版本（保留兼容）
# ═══════════════════════════════════════════════

def compress_conversation(session_id, keep_rounds=RECENT_TURNS_KEEP,
                           summary_model=None, trace_id="", user_id=""):
    """同步压缩，阻塞主链路。"""
    import time
    now = time.time()
    last_cleanup = _compression_lock_timestamps.get("_last_cleanup", 0)
    if now - last_cleanup > _LOCK_CLEANUP_INTERVAL:
        _cleanup_expired_locks()

    lock = _compression_locks.setdefault(session_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    _compression_lock_timestamps[session_id] = now

    try:
        messages = _get_chat_messages_with_ids(session_id)
        if not messages:
            return

        total_tokens = estimate_messages_tokens(messages)
        if total_tokens <= RECENT_TOKEN_MIN:
            return

        old_summary = get_latest_summary(session_id)

        cut_i = _find_cut_point(messages, TOKEN_TARGET)
        if cut_i >= len(messages):
            return

        old_msgs = messages[:cut_i]
        if not old_msgs:
            return

        summary = _generate_summary(old_msgs, old_summary=old_summary)
        if not summary:
            return

        user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
        cover_end = 0
        for ui in user_indices:
            if messages[ui]["id"] < messages[cut_i]["id"]:
                cover_end += 1
            else:
                break

        summary_token_est = estimate_tokens(summary)

        try:
            add_summary(session_id, summary,
                        cover_turn_start=1,
                        cover_turn_end=cover_end,
                        token_est=summary_token_est)

            cutoff_id = messages[cut_i]["id"]
            delete_chat_messages_before(session_id, cutoff_id)
        except Exception as del_e:
            print(f"[Memory] 同步压缩事务失败，回滚摘要: {del_e}")
            from memory.stores.sqlite_store import get_session_summaries_db
            import sqlite3
            try:
                db_path = get_session_summaries_db(session_id)
                conn = sqlite3.connect(db_path, timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "DELETE FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                    (session_id,)
                )
                conn.commit()
                conn.close()
            except Exception as rollback_e:
                print(f"[Memory] 同步摘要回滚失败: {rollback_e}")
            raise

        log_operation(trace_id, user_id, session_id,
                      "memory.compress.done",
                      f"tokens {total_tokens}→{summary_token_est} | {summary[:80]}")
        print(f"[Memory] 会话 {session_id[:12]} 同步压缩完成 "
              f"(token {total_tokens}→{summary_token_est})")

    except Exception as e:
        print(f"[Memory] 同步压缩失败: {e}")
    finally:
        try:
            lock.release()
        except Exception:
            pass


# ═══════════════════════════════════════════════
# 兼容旧接口
# ═══════════════════════════════════════════════

def add_compressed_summary(summary, source_type="auto"):
    """兼容旧接口，写入当前活跃会话。"""
    from memory.session import get_active_session_id
    sid = get_active_session_id()
    if not sid:
        return
    add_summary(sid, summary)


def get_recent_compressed(days=7):
    """兼容旧接口。"""
    from memory.session import get_active_session_id
    sid = get_active_session_id()
    if not sid:
        return ""
    latest = get_latest_summary(sid)
    if not latest:
        return ""
    return f"【历史对话摘要】\n- {latest}"


def get_latest_summary_for_session(session_id):
    """获取指定会话的最新摘要。"""
    return get_latest_summary(session_id)


# ── 消息压缩（从 react_engine.py 迁入） ──


def check_and_compress_messages(messages: List[Dict[str, Any]], *,
                            max_context_tokens: int, safety_margin: int,
                            trace_id: str = "", session_id: str = "",
                            max_result_chars: int = 8000) -> List[Dict[str, Any]]:
    """检查消息列表的 token 总量，接近上限时自动压缩。

    使用 estimate_messages_tokens 估算 token 数，
    当超过 MAX_CONTEXT_TOKENS - SAFETY_MARGIN 时触发压缩。
    """
    total_tokens = estimate_messages_tokens(messages)
    threshold = max_context_tokens - safety_margin

    if total_tokens > threshold:
        get_logger().info(
            f"上下文 token 数 {total_tokens} 超过阈值 {threshold}，"
            f"触发压缩（目标：{max_context_tokens}）",
            trace_id=trace_id, session_id=session_id
        )
        return compress_messages(messages, target_tokens=threshold,
                                 session_id=session_id, trace_id=trace_id,
                                 max_result_chars=max_result_chars)
    return messages


def compress_messages(messages: List[Dict[str, Any]], target_tokens: int, *,
                  session_id: str = "", trace_id: str = "",
                  max_result_chars: int = 8000) -> List[Dict[str, Any]]:
    """压缩消息列表至目标 token 数以内。

    策略：保留 system 消息 + 最近若干轮对话，中间历史合并为摘要。
    """
    from memory.session import compress_conversation_async

    # 分离 system 消息
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if not non_system:
        return messages

    # 估算每轮 token 并保留末尾未压缩的轮次
    accumulated = 0
    keep_count = 0
    for m in reversed(non_system):
        mt = estimate_tokens(m.get("content", ""))
        accumulated += mt
        keep_count += 1
        if accumulated > target_tokens // 2:
            break

    if keep_count < len(non_system):
        # 调整切割边界：确保 assistant(tool_calls) + tool 配对不被切断
        # 避免 API 400（OpenAI/DeepSeek 要求 tool 消息必须紧跟含对应 tool_call_id 的 assistant）
        cut_idx = len(non_system) - keep_count

        # 1) 如果 to_keep 第一条是 tool，向上回溯包含其 assistant
        while cut_idx < len(non_system) and non_system[cut_idx].get("role") == "tool":
            cut_idx -= 1
            keep_count = len(non_system) - cut_idx

        # 2) 如果 to_compress 最后一条是 assistant(tool_calls)，把配对的 tool 也归入 to_keep
        while cut_idx > 0 and cut_idx < len(non_system) \
                and non_system[cut_idx - 1].get("role") == "assistant" \
                and non_system[cut_idx - 1].get("tool_calls"):
            assistant_tool_ids = set()
            for tc in non_system[cut_idx - 1].get("tool_calls", []):
                tid = tc.get("id") or ""
                if tid:
                    assistant_tool_ids.add(tid)
            if not assistant_tool_ids:
                break
            # 扫描后续 tool 消息，将匹配的移入 to_keep
            moved = False
            while cut_idx < len(non_system) \
                    and non_system[cut_idx].get("role") == "tool" \
                    and non_system[cut_idx].get("tool_call_id") in assistant_tool_ids:
                cut_idx += 1
                keep_count = len(non_system) - cut_idx
                moved = True
            if not moved:
                break

        # 用调整后的 cut_idx 重新划分
        to_compress = non_system[:cut_idx] if cut_idx > 0 else []
        to_keep = non_system[cut_idx:] if cut_idx < len(non_system) else []

        # 尝试压缩中间部分为摘要
        compressed = system_msgs + [
            {"role": "system", "content": "[上下文压缩] 以下为历史对话的压缩摘要，已省略详细轮次以节省上下文空间。"}
        ]

        try:
            if session_id and to_compress:
                loop = None
                try:
                    loop = __import__('asyncio').get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import asyncio
                    task = asyncio.run_coroutine_threadsafe(
                        compress_conversation_async(session_id, to_compress),
                        loop
                    )
                    try:
                        summary = task.result(timeout=10)
                        if summary:
                            compressed.append({
                                "role": "system",
                                "content": f"[历史摘要] {summary[:MAX_SUMMARY_CHARS]}"
                            })
                    except Exception:
                        pass
                else:
                    # 同步 fallback：用 estimate_tokens 做简单截断
                    total_chars = sum(len(m.get("content", "")) for m in to_compress)
                    if total_chars > max_result_chars:
                        compressed.append({
                            "role": "system",
                            "content": f"[历史压缩] 已省略 {len(to_compress)} 轮历史对话（约 {total_chars} 字符）。"
                        })
                    else:
                        compressed.extend(to_compress)

            compressed.extend(to_keep)
        except Exception as e:
            get_logger().warning(f"上下文压缩失败，使用原始消息: {e}",
                                trace_id=trace_id, session_id=session_id)
            return messages

        return compressed

    return messages
