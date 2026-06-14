"""
对话压缩器：异步生成摘要 + 追加到 summaries.md。

核心改动：
  - 异步执行（daemon 线程），不阻塞主流程
  - 摘要追加到 Markdown 文件（不再写 JSON / 不再向 SQLite 插入 dead system 消息）
  - 同一会话防并发：同一时刻只允许一个压缩任务
"""
import json
import threading
from memory.llm import call_llm
from memory.config import MAX_SUMMARY_LENGTH
from memory.long_term import append_summary
from memory.stores.sqlite_store import (
    _get_chat_messages_with_ids,
    delete_chat_messages_before,
    log_operation,
)

SUMMARY_PROMPT_TEMPLATE = (
    "请用一句话（不超过{max_len}字）总结以下对话的核心内容，保留用户需求、项目或关键事实：\n{dialogue}"
)

# ── 防并发锁 ──
_compression_locks = {}


# ═══════════════════════════════════════════════
# 摘要生成
# ═══════════════════════════════════════════════

def _generate_summary(messages, model=None):
    """调用 LLM 生成对话摘要。messages 为 [{"role":..., "content":...}] 列表。"""
    if not messages:
        return ""
    dialogue = json.dumps(messages, ensure_ascii=False)[:2000]
    prompt = {
        "role": "system",
        "content": SUMMARY_PROMPT_TEMPLATE.format(max_len=MAX_SUMMARY_LENGTH, dialogue=dialogue),
    }
    resp = call_llm([prompt], model=model)
    if resp and "message" in resp:
        return resp["message"]["content"].strip()
    return ""


# ═══════════════════════════════════════════════
# 异步压缩主函数
# ═══════════════════════════════════════════════

def compress_conversation_async(session_id, keep_rounds=8, summary_model=None,
                                 trace_id="", user_id=""):
    """
    异步压缩指定会话的历史消息。

    同一会话同时只运行一个压缩任务（多余调用直接跳过）。
    压缩后：删除 SQLite 中的旧消息 → 摘要追加到 summaries.md。
    不再向 SQLite 插入 system 摘要消息（历史消息不含 role=system，不会被清洗）。

    参数：
        session_id: 会话 ID
        keep_rounds: 保留最近 N 个用户轮次
        summary_model: LLM 模型名
        trace_id, user_id: 日志用
    """
    lock = _compression_locks.setdefault(session_id, threading.Lock())
    if lock.locked():
        return  # 已有压缩任务在进行中

    def _run():
        with lock:
            try:
                messages = _get_chat_messages_with_ids(session_id)

                # 找到所有用户消息的索引
                user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
                if len(user_indices) <= keep_rounds:
                    return

                # 保留点：从倒数第 keep_rounds 个用户消息开始
                keep_start = user_indices[-keep_rounds]
                old_msgs = messages[:keep_start]

                summary = _generate_summary(old_msgs, model=summary_model)
                if not summary:
                    return

                # 追加到 Markdown 长期记忆
                append_summary(f"【自动压缩】{summary}")

                # 删除 SQLite 中的旧消息
                cutoff_id = messages[keep_start]["id"]
                delete_chat_messages_before(session_id, cutoff_id)

                log_operation(trace_id, user_id, session_id,
                              "memory.compress.done", summary)
                print(f"[Memory] 会话 {session_id} 压缩完成: {summary[:60]}...")

            except Exception as e:
                print(f"[Memory] 异步压缩失败: {e}")

    t = threading.Thread(target=_run, daemon=True,
                         name=f"mem-compress-{session_id[:8]}")
    t.start()


# ═══════════════════════════════════════════════
# 同步版本（保留兼容）
# ═══════════════════════════════════════════════

def compress_conversation(session_id, keep_rounds=8, summary_model=None,
                           trace_id="", user_id=""):
    """
    同步压缩（保留兼容，新代码请用 compress_conversation_async）。
    注意：此函数阻塞主链路 3-5s，不推荐生产使用。
    """
    messages = _get_chat_messages_with_ids(session_id)

    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) <= keep_rounds:
        return

    keep_start = user_indices[-keep_rounds]
    old_msgs = messages[:keep_start]

    summary = _generate_summary(old_msgs, model=summary_model)
    if not summary:
        return

    append_summary(f"【自动压缩】{summary}")

    cutoff_id = messages[keep_start]["id"]
    delete_chat_messages_before(session_id, cutoff_id)

    log_operation(trace_id, user_id, session_id,
                  "memory.compress.done", summary)


# ═══════════════════════════════════════════════
# 兼容接口
# ═══════════════════════════════════════════════

def add_compressed_summary(summary, source_type="auto"):
    """兼容旧接口：追加到 summaries.md。"""
    prefix = "【自动压缩】" if source_type == "auto" else ""
    append_summary(f"{prefix}{summary}")


def get_recent_compressed(days=7):
    """兼容旧接口：从 summaries.md 读取最近 N 天的摘要。"""
    from memory.long_term import read_summaries
    recent = read_summaries(days)
    if not recent:
        return ""
    return "【历史对话摘要】\n" + "\n".join(f"- {s}" for s in recent)
