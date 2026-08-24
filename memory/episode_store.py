"""Episode Store 经验库：SQLite 全局库存储成功情节，支持 BM25 回放。

表 episodes 字段：
- normalized_query: 规范化查询文本（去空白/标点/小写，用于精确合并）
- tool_sequence: 工具序列 JSON（[{tool, params}, ...]）
- param_template: 参数模板 JSON（{tool: {参数名: 示例值}}）
- result_summary: 结果摘要文本
- tags: 标签（逗号分隔，取工具名去重）
- source_session_id: 来源会话 ID
- success_count: 成功次数（近似合并时累加）
- created_at / updated_at

存储于全局库 EXPERIENCE_DB（%APPDATA%\\smartDesktop\\memory\\experience.db），
复用 sqlite_store._get_conn_simple 低频全局库连接模式，与 per-session messages.db 隔离。
"""
import re
import json
import datetime
from typing import Any, Dict, List, Optional, Tuple

from memory.config import EXPERIENCE_DB
from memory.stores.sqlite_store import _get_conn_simple
from memory.context_router import BM25

_EPISODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_query TEXT NOT NULL,
    tool_sequence TEXT NOT NULL,
    param_template TEXT NOT NULL DEFAULT '{}',
    result_summary TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    source_session_id TEXT DEFAULT '',
    success_count INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_norm ON episodes(normalized_query);
"""

# 近似合并判定阈值：新查询与既有情节规范化文本的 BM25 归一化相似度达到该值即合并计数
_EPISODE_MERGE_THRESHOLD = 0.80


def _now() -> str:
    """当前时间戳（本地时间字符串）。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _init_episode_tables() -> None:
    """建表（幂等）。"""
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        conn.executescript(_EPISODE_SCHEMA)


def _row_to_episode(row) -> dict:
    """SQLite 行 → 情节 dict。"""
    return {
        "id": row[0],
        "normalized_query": row[1],
        "tool_sequence": json.loads(row[2] or "[]"),
        "param_template": json.loads(row[3] or "{}"),
        "result_summary": row[4] or "",
        "tags": row[5] or "",
        "source_session_id": row[6] or "",
        "success_count": row[7] or 1,
        "created_at": row[8] or "",
        "updated_at": row[9] or "",
    }


def normalize_query(text: str) -> str:
    """规范化查询文本：去空白、去常见标点、小写。用于精确合并与检索文档。"""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", str(text).strip())
    t = re.sub(r"[，。！？；：、,.!?;:()（）\[\]【】\"'\"'\-_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _extract_param_template(tool_sequence: List[dict]) -> dict:
    """从工具调用链提取参数模板：{tool: {参数名: 示例值}}，仅保留 dict 型参数。"""
    template: Dict[str, dict] = {}
    for call in tool_sequence or []:
        tool = call.get("tool", "")
        params = call.get("params") or {}
        if not tool or not isinstance(params, dict):
            continue
        template.setdefault(tool, {})
        for k, v in params.items():
            template[tool].setdefault(k, v)
    return template


def _default_tags(tool_sequence: List[dict]) -> str:
    """默认标签：工具名去重后逗号拼接。"""
    return ",".join(sorted({c.get("tool", "") for c in tool_sequence or [] if c.get("tool")}))


def upsert_episode(normalized_query: str, tool_sequence: List[dict],
                   param_template: Optional[dict] = None,
                   result_summary: str = "", tags: str = "",
                   source_session_id: str = "") -> int:
    """写入/合并一条成功情节。

    合并策略：
    1) normalized_query 精确命中 → 累加 success_count 并更新字段；
    2) 否则以全部既有 normalized_query 为文档做 BM25 检索，top-1 归一化相似度
       ≥ _EPISODE_MERGE_THRESHOLD → 视为同义情节合并计数；
    3) 否则插入新记录。
    返回情节 id。
    """
    _init_episode_tables()
    now = _now()
    query = normalized_query or ""
    seq_json = json.dumps(tool_sequence or [], ensure_ascii=False)
    tmpl_json = json.dumps(param_template if param_template is not None
                           else _extract_param_template(tool_sequence), ensure_ascii=False)
    tag_text = tags or _default_tags(tool_sequence)

    with _get_conn_simple(EXPERIENCE_DB) as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT id, success_count FROM episodes WHERE normalized_query = ?", (query,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE episodes SET tool_sequence=?, param_template=?, result_summary=?, "
                "tags=?, source_session_id=?, success_count=success_count+1, updated_at=? "
                "WHERE id=?",
                (seq_json, tmpl_json, result_summary, tag_text, source_session_id, now, row[0]),
            )
            conn.commit()
            return row[0]

        # 近似合并：BM25 检索既有情节
        docs = []
        rows = conn.execute("SELECT id, normalized_query FROM episodes").fetchall()
        for rid, rq in rows:
            if rq:
                docs.append(rq)
        if docs:
            try:
                bm25 = BM25()
                bm25.fit(docs)
                scores = bm25.score_all(query)
                self_score = bm25.self_score(query)
                if self_score > 0:
                    best_idx = max(range(len(scores)), key=lambda i: scores[i])
                    norm = min(scores[best_idx] / self_score, 1.0)
                    if norm >= _EPISODE_MERGE_THRESHOLD:
                        merged_id = rows[best_idx][0]
                        conn.execute(
                            "UPDATE episodes SET tool_sequence=?, param_template=?, result_summary=?, "
                            "tags=?, source_session_id=?, success_count=success_count+1, updated_at=? "
                            "WHERE id=?",
                            (seq_json, tmpl_json, result_summary, tag_text, source_session_id, now, merged_id),
                        )
                        conn.commit()
                        return merged_id
            except Exception:
                pass  # 检索失败不影响主流程，直接走插入

        cur = conn.execute(
            "INSERT INTO episodes (normalized_query, tool_sequence, param_template, "
            "result_summary, tags, source_session_id, success_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (query, seq_json, tmpl_json, result_summary, tag_text, source_session_id, now, now),
        )
        conn.commit()
        return cur.lastrowid


def get_all_episodes() -> List[dict]:
    """返回全部情节（按最近更新倒序）。"""
    _init_episode_tables()
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        rows = conn.execute(
            "SELECT id, normalized_query, tool_sequence, param_template, result_summary, "
            "tags, source_session_id, success_count, created_at, updated_at "
            "FROM episodes ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_episode(r) for r in rows]


def get_episode(normalized_query: str) -> Optional[dict]:
    """按规范化查询精确查询情节。"""
    _init_episode_tables()
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        row = conn.execute(
            "SELECT id, normalized_query, tool_sequence, param_template, result_summary, "
            "tags, source_session_id, success_count, created_at, updated_at "
            "FROM episodes WHERE normalized_query = ?", (normalized_query,)
        ).fetchone()
    return _row_to_episode(row) if row else None


def bm25_search_episodes(query: str, limit: int = 5) -> List[Tuple[dict, float]]:
    """BM25 检索经验库：以 normalized_query + tags 为文档，返回 [(episode, 归一化相似度)] 倒序。

    归一化相似度算法与 find_historic_topic_match 保持一致：max_raw / max(self_score, eps)。
    """
    _init_episode_tables()
    query_norm = normalize_query(query)
    if not query_norm:
        return []
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        rows = conn.execute(
            "SELECT id, normalized_query, tool_sequence, param_template, result_summary, "
            "tags, source_session_id, success_count, created_at, updated_at "
            "FROM episodes"
        ).fetchall()
    if not rows:
        return []
    docs = []
    valid = []
    for r in rows:
        ep = _row_to_episode(r)
        doc = f"{ep['normalized_query']} {ep['tags']}".strip()
        if not doc:
            continue
        docs.append(doc)
        valid.append(ep)
    if not docs:
        return []
    try:
        bm25 = BM25()
        bm25.fit(docs)
        scores = bm25.score_all(query_norm)
        self_score = bm25.self_score(query_norm)
        if self_score <= 0:
            return []
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results = []
        for idx in ranked[:limit]:
            norm = min(scores[idx] / self_score, 1.0)
            results.append((valid[idx], round(norm, 4)))
        return results
    except Exception:
        return []


_EPISODE_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/][^\s，。；、,;:！？\"'（）()\[\]{}<>]*"
    r"|/home/[^\s，。；、,;:！？\"'（）()\[\]{}<>]*"
    r"|/Users/[^\s，。；、,;:！？\"'（）()\[\]{}<>]*"
    r"|//[^\s，。；、,;:！？\"'（）()\[\]{}<>]*)"
)


def _sanitize_episode_content(content: str) -> str:
    """对情节中的路径片段做脱敏，防止 LLM 从经验参考中提取不存在的路径调用工具。"""
    if not content:
        return content
    return _EPISODE_PATH_RE.sub("[历史路径已脱敏]", content)


def build_episode_context(episode: dict, max_chars: int = 3000) -> str:
    """将情节构建为带【历史经验参考】前缀的 user 消息文本（含禁令，受字符预算约束）。

    注入内容：规范化查询 + 工具序列 + 参数模板 + 结果摘要，路径脱敏后按预算裁剪。
    """
    if not episode:
        return ""
    tool_seq = episode.get("tool_sequence") or []
    param_tmpl = episode.get("param_template") or {}
    summary = (episode.get("result_summary") or "").strip()
    norm_q = (episode.get("normalized_query") or "").strip()

    lines = [
        "【历史经验参考：仅作参考，非当前任务】",
        "以下是一段成功完成过的相似任务经验（工具序列与参数结构），可用于参考执行方式：",
        "注意：禁止从中提取任何文件路径、命令或未经当前上下文确认的参数值；",
        "参数需基于本次对话的真实信息重新确认后再调用工具。",
    ]
    if norm_q:
        lines.append(f"历史任务：{norm_q}")
    if tool_seq:
        seq_text = json.dumps(tool_seq, ensure_ascii=False)
        lines.append(f"工具序列：{seq_text}")
    if param_tmpl:
        tmpl_text = json.dumps(param_tmpl, ensure_ascii=False)
        lines.append(f"参数模板：{tmpl_text}")
    if summary:
        lines.append(f"结果摘要：{summary}")
    text = "\n".join(lines)
    text = _sanitize_episode_content(text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（内容过长已截断）"
    return text
