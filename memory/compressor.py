"""
对话压缩器：异步生成摘要 + 追加到 summaries.md。

核心改动（v3 — Token 预算制 + 递归摘要）：
  - 异步执行（daemon 线程），不阻塞主流程
  - 触发条件从"轮次计数"改为"Token 预算"
  - 递归摘要：每次压缩合并旧摘要，防止信息衰减
  - 同一会话防并发：同一时刻只允许一个压缩任务
"""
import json
import threading
from memory.llm import call_llm
from memory.config import MAX_SUMMARY_LENGTH, SUMMARY_MODEL
from memory.long_term import append_summary, read_summaries
from memory.stores.sqlite_store import (
    _get_chat_messages_with_ids,
    delete_chat_messages_before,
    log_operation,
)

# ── Token 估算 ──

def estimate_tokens(text):
    """估算文本 token 数（中文约 1 字≈1 token，英文约 1 词≈1.3 token，混合取 1 字≈0.5 token 保守值）。

    不使用 tiktoken（避免依赖），采用安全近似：
      中文场景 token 略少于字符数，按 1 字符≈0.55 token 计算。
    """
    if not text:
        return 0
    return max(1, int(len(text) * 0.55))


def estimate_messages_tokens(messages):
    """估算消息列表的总 token 数。"""
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content", ""))
        # role 标记约 4 token
        total += 4
    return total


# ── 递归摘要提示词 ──

SUMMARY_PROMPT_TEMPLATE = (
    "请用简洁的中文（不超过{max_len}字）总结以下对话内容，保留关键事实、用户需求、项目信息和技术决策。\n"
    "{old_summary}"
    "对话：\n{dialogue}"
)


# ── 防并发锁 ──
_compression_locks = {}


# ═══════════════════════════════════════════════
# 摘要生成（支持递归合并）
# ═══════════════════════════════════════════════

def _generate_summary(messages, old_summary="", model=None):
    """调用 LLM 生成对话摘要。

    Args:
        messages: [{"role":..., "content":...}] 列表
        old_summary: 上一次压缩的摘要（用于递归合并）
        model: LLM 模型名，默认使用 SUMMARY_MODEL

    Returns:
        摘要字符串，失败返回空字符串
    """
    if not messages and not old_summary:
        return ""
    dialogue = json.dumps(messages, ensure_ascii=False)[:3000]
    old_block = f"【之前的摘要】{old_summary}\n" if old_summary else ""
    prompt = {
        "role": "system",
        "content": SUMMARY_PROMPT_TEMPLATE.format(
            max_len=MAX_SUMMARY_LENGTH,
            old_summary=old_block,
            dialogue=dialogue,
        ),
    }
    resp = call_llm([prompt], model=model or SUMMARY_MODEL)
    if resp and "message" in resp:
        return resp["message"]["content"].strip()
    return ""


# ═══════════════════════════════════════════════
# 异步压缩主函数（Token 预算制）
# ═══════════════════════════════════════════════

def compress_conversation_async(session_id, recent_token_min=30000,
                                 recent_token_max=50000, summary_model=None,
                                 trace_id="", user_id=""):
    """
    异步压缩指定会话的历史消息（Token 预算制）。

    触发逻辑：
      当前消息总 token > recent_token_min → 触发压缩
      当前消息总 token > recent_token_max → 必须压缩（硬阈值）

    压缩流程：
      1. 计算当前消息 token 数
      2. 读取旧摘要（递归合并）
      3. 找到需要压缩的消息范围
      4. 生成递归摘要（旧摘要 + 新消息 → 合并摘要）
      5. 追加合并摘要到 summaries.md
      6. 删除 SQLite 中的旧消息

    参数：
        session_id: 会话 ID
        recent_token_min: 软阈值（默认 30000）
        recent_token_max: 硬阈值（默认 50000）
        summary_model: LLM 模型名（默认 SUMMARY_MODEL）
        trace_id, user_id: 日志用
    """
    lock = _compression_locks.setdefault(session_id, threading.Lock())
    if lock.locked():
        return  # 已有压缩任务在进行中

    def _run():
        with lock:
            try:
                messages = _get_chat_messages_with_ids(session_id)
                if not messages:
                    return

                total_tokens = estimate_messages_tokens(messages)

                # ── Token 预算判断 ──
                # 找到用户消息索引
                user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
                if not user_indices:
                    return

                # 软阈值：当前 token 未超过最小值，无需压缩
                if total_tokens <= recent_token_min:
                    return

                # 硬阈值：必须压缩；软阈值：可压缩
                # 保留最近若干轮用户消息，使剩余 token 在预算内
                target_tokens = max(recent_token_min // 2, 10000)  # 压缩后目标：一半软阈值

                # 从后往前找保留点
                keep_start = len(messages)
                accumulated = 0
                for i in range(len(messages) - 1, -1, -1):
                    accumulated += estimate_tokens(messages[i].get("content", "")) + 4
                    if accumulated >= target_tokens:
                        # 确保保留点在用户消息边界
                        for ui in reversed(user_indices):
                            if ui <= i:
                                keep_start = ui
                                break
                        else:
                            keep_start = i
                        break

                if keep_start >= len(messages):
                    return  # 无需压缩

                old_msgs = messages[:keep_start]

                # ── 递归摘要：读取旧摘要 ──
                old_summaries = read_summaries(days=30)
                old_summary = old_summaries[-1] if old_summaries else ""

                # ── 生成合并摘要 ──
                summary = _generate_summary(old_msgs, old_summary=old_summary,
                                            model=summary_model)
                if not summary:
                    return

                # ── 追加合并摘要到 summaries.md ──
                append_summary(f"【自动压缩】{summary}")

                # ── 删除 SQLite 中的旧消息 ──
                cutoff_id = messages[keep_start]["id"]
                delete_chat_messages_before(session_id, cutoff_id)

                log_operation(trace_id, user_id, session_id,
                              "memory.compress.done",
                              f"tokens {total_tokens}→{estimate_tokens(summary)} | {summary[:80]}")
                print(f"[Memory] 会话 {session_id} 压缩完成 "
                      f"(tokens {total_tokens}→{estimate_tokens(summary)}): {summary[:60]}...")

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

    # 递归摘要：读取旧摘要
    old_summaries = read_summaries(days=30)
    old_summary = old_summaries[-1] if old_summaries else ""

    summary = _generate_summary(old_msgs, old_summary=old_summary,
                                model=summary_model)
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
    recent = read_summaries(days)
    if not recent:
        return ""
    return "【历史对话摘要】\n" + "\n".join(f"- {s}" for s in recent)


def get_latest_summary(days=30):
    """获取最近一条压缩摘要（用于注入上下文）。"""
    recent = read_summaries(days)
    return recent[-1] if recent else ""
