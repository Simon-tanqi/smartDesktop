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
        # 去掉可能的 <｜end▁of▁thinking｜>... 包裹
        if content.startswith("<｜end▁of▁thinking｜>"):
            content = content.split(" response", 1)[-1].strip()
        return content
    return ""


# ═══════════════════════════════════════════════
# Token 预算制切割
# ═══════════════════════════════════════════════

def _find_cut_point(messages, target_tokens):
    """反向累加 token，找到保留起点。保留最近 N 轮完整对话不动，但受硬上限约束。"""
    if not messages:
        return len(messages)

    # 保留最近 RECENT_TURNS_KEEP 轮用户消息（完整原文不压缩）
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    keep_turns = min(RECENT_TURNS_KEEP, len(user_indices))

    if keep_turns >= len(user_indices):
        return len(messages)

    # 计算保留轮次的 token 开销
    last_user_idx = user_indices[-keep_turns] if keep_turns > 0 else len(messages)
    preserved_tokens = sum(
        estimate_tokens(messages[i].get("content", "")) + 4
        for i in range(last_user_idx, len(messages))
    )
    # 硬上限：保留轮次已用掉 60% token 预算时，减少保留轮数
    if preserved_tokens > target_tokens * 0.6 and keep_turns > 1:
        keep_turns = max(1, keep_turns - 1)
        last_user_idx = user_indices[-keep_turns]

    # 从后往前累加 token，参考用户轮次边界
    accumulated = 0
    cut_i = len(messages)

    for i in range(len(messages) - 1, -1, -1):
        accumulated += estimate_tokens(messages[i].get("content", "")) + 4
        if accumulated >= target_tokens and i <= last_user_idx:
            cut_i = i
            break

    return cut_i


# ═══════════════════════════════════════════════
# 异步压缩
# ═══════════════════════════════════════════════

def compress_conversation_async(session_id, recent_token_min=RECENT_TOKEN_MIN,
                                 recent_token_max=RECENT_TOKEN_MAX,
                                 summary_model=None, trace_id="", user_id=""):
    lock = _compression_locks.setdefault(session_id, threading.Lock())
    if lock.locked():
        return

    def _run():
        with lock:
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

                # 写入 per-session summaries.db
                user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
                cover_start = 0
                cover_end = 0
                for ui in user_indices:
                    if messages[ui]["id"] < messages[cut_i]["id"]:
                        cover_end += 1
                    else:
                        break

                summary_token_est = estimate_tokens(summary)
                add_summary(session_id, summary,
                            cover_turn_start=cover_start,
                            cover_turn_end=cover_end,
                            token_est=summary_token_est)

                # 删除旧消息
                cutoff_id = messages[cut_i]["id"]
                delete_chat_messages_before(session_id, cutoff_id)

                log_operation(trace_id, user_id, session_id,
                              "memory.compress.done",
                              f"tokens {total_tokens}→{summary_token_est} | {summary[:80]}")
                print(f"[Memory] 会话 {session_id[:12]} 压缩完成 "
                      f"(token {total_tokens}→{summary_token_est})")

            except Exception as e:
                print(f"[Memory] 异步压缩失败: {e}")

    t = threading.Thread(target=_run, daemon=True,
                         name=f"mem-compress-{session_id[:8]}")
    t.start()


# ═══════════════════════════════════════════════
# 同步版本（保留兼容）
# ═══════════════════════════════════════════════

def compress_conversation(session_id, keep_rounds=RECENT_TURNS_KEEP,
                           summary_model=None, trace_id="", user_id=""):
    """同步压缩，阻塞主链路。"""
    messages = _get_chat_messages_with_ids(session_id)
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) <= keep_rounds:
        return

    keep_start = user_indices[-keep_rounds]
    old_msgs = messages[:keep_start]
    old_summary = get_latest_summary(session_id)

    summary = _generate_summary(old_msgs, old_summary=old_summary)
    if not summary:
        return

    add_summary(session_id, summary, token_est=estimate_tokens(summary))
    cutoff_id = messages[keep_start]["id"]
    delete_chat_messages_before(session_id, cutoff_id)
    log_operation(trace_id, user_id, session_id, "memory.compress.done", summary)


# ═══════════════════════════════════════════════
# 兼容旧接口
# ═══════════════════════════════════════════════

def add_compressed_summary(summary, source_type="auto"):
    """兼容旧接口，写入当前活跃会话。"""
    from memory.session import get_active_session_id
    sid = get_active_session_id()
    add_summary(sid, summary)


def get_recent_compressed(days=7):
    """兼容旧接口。"""
    from memory.session import get_active_session_id
    sid = get_active_session_id()
    latest = get_latest_summary(sid)
    if not latest:
        return ""
    return f"【历史对话摘要】\n- {latest}"


def get_latest_summary_for_session(session_id):
    """获取指定会话的最新摘要。"""
    return get_latest_summary(session_id)
