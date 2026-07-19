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
    old_block = f"【之前摘要】{old_summary}\n" if old_summary else ""
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
    """增量合并摘要：接收旧摘要 + 单轮结构化数据，输出合并后的新摘要。
    
    设计要点：
    1. 算力恒定：仅合并最新一轮数据，不重算全部历史
    2. 保持连续性：基于旧摘要增量更新，避免内容漂移
    3. 统一存储：写入 summaries.db，与 compress_conversation_async 共用存储
    
    Args:
        session_id: 会话ID
        new_round_data: 单轮结构化数据，包含 user_task、ai_reply、tool_calls
        max_len: 摘要最大字符数
    
    Returns:
        合并后的新摘要字符串
    """
    old_summary = get_latest_summary(session_id)
    
    round_text = []
    if new_round_data.get("user_task"):
        round_text.append(f"用户: {new_round_data['user_task'][:500]}")
    if new_round_data.get("ai_reply"):
        round_text.append(f"AI: {new_round_data['ai_reply'][:500]}")
    for tc in new_round_data.get("tool_calls", []):
        tool_name = tc.get("tool_name", "")
        tool_args = tc.get("tool_arguments", {})
        tool_result = tc.get("tool_result", "")
        args_preview = json.dumps(tool_args, ensure_ascii=False)[:100]
        result_preview = tool_result[:100] if isinstance(tool_result, str) else str(tool_result)[:100]
        round_text.append(f"工具[{tool_name}]: 参数={args_preview}, 结果={result_preview}")
    
    dialogue = "\n".join(round_text)
    
    old_block = f"【之前摘要】{old_summary}\n" if old_summary else ""
    
    system_prompt = {
        "role": "system",
        "content": SUMMARY_PROMPT_TEMPLATE.format(
            max_len=max_len,
            old_summary=old_block,
            dialogue=dialogue,
        ),
    }
    
    resp = call_llm_with_config(
        [system_prompt, {"role": "user", "content": "请将新对话内容合并到摘要中，保持简洁"}],
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

    lock = _compression_locks.setdefault(session_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    _compression_lock_timestamps[session_id] = now

    def _run():
        try:
            messages = _get_chat_messages_with_ids(session_id)
            if not messages:
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
