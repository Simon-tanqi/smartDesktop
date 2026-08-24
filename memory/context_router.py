"""
上下文路由：BM25 纯统计任务切换检测

不依赖：
  - ❌ 向量模型 / embedding
  - ❌ 手动关键词枚举
  - ❌ LLM 判断
  - ❌ 外部依赖（纯 Python math）

只依赖：
  - ✅ 词频统计（BM25 — Elasticsearch/Lucene 核心算法）
  - ✅ 文本自身

使用方式：

    detector = ContextSwitchDetector()
    
    # 每一轮用户新消息，调用 detect() 判断是否任务切换
    result = detector.detect("生成2026年AI发展PPT，普通人有什么机会")
    if result["is_switch"]:
        clear_context()  # 清空旧的工具调用链、思考轨迹等

原理：
  每轮对话的用户消息逐条累积到 history 中。
  新输入与最近 N 轮历史分别计算 BM25 分数。
  若分数低于动态阈值 → 任务切换。
"""

import math
import os
import re
from typing import List, Dict, Any

TOKEN_PATTERN = re.compile(r'[\u4e00-\u9fff\w]+')

# 延续指令模式（模块级复用：detect() 判定 + commit_task 语料质量过滤共用）
CONTINUATION_PATTERNS = [
    r'^(继续|接著|然后|下一步|继续任务|继续做|接上|go on|continue|next|and then|also)$',
    r'^(好的|行|可以|嗯|来了|收到|好|OK|okay|ok)$',
    r'^(?:可以了|就这样|好了|完事|done|finish|不错|满意|行了|没事了|没其他事|没别的|没什么)$',
    r'^再加', r'^再帮', r'^再写', r'^再添', r'^再做',
    r'^把.*(?:也|都)',
]


def _is_low_quality_task(text: str) -> bool:
    """判断文本是否为低质量话题语料（过短 / 纯续写 / 确认类短消息）。

    GAP④：commit_task 每轮无条件把"好的""继续"等短消息加入语料会稀释话题信号，
    此类消息不进入语料，保证切换判定不失真。
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) <= 8:
        return True
    for pat in CONTINUATION_PATTERNS:
        if re.match(pat, stripped.lower()):
            return True
    return False


def tokenize(text: str) -> List[str]:
    """分词：中文双字重叠 bigram + 单字 Fallback。
    
    每个中文位置同时产出：
      - 双字 bigram（精度高："卸载" ↔ "卸载" 完全命中）
      - 单字（召回兜底："再" "我" "生" 等单字提供相似度底噪）
    
    举例 "生成PPT卸载Qoder"：
      bigrams: [生成, ppt, 卸载, qoder]
      chars:   [生, 成, pp, t, 卸, 载, qo, de, r]
    """
    text = text.lower()
    raw_chars = list(text)
    tokens = []
    i = 0
    while i < len(raw_chars):
        ch = raw_chars[i]
        # 中文：同时产出 bigram + 单字
        if '\u4e00' <= ch <= '\u9fff':
            # 单字（跳过常见停用）
            if ch not in '的是不了在有一我他她它这那也和就都对上为从把被让给向到':
                tokens.append(ch)
            # bigram（不停用，因为上下文很重要）
            if i + 1 < len(raw_chars) and '\u4e00' <= raw_chars[i+1] <= '\u9fff':
                tokens.append(ch + raw_chars[i+1])
                i += 1
                continue
            i += 1
        elif re.match(r'[a-z0-9]', ch):
            # 英文/数字连续序列
            start = i
            while i < len(raw_chars) and re.match(r'[a-z0-9]', raw_chars[i]):
                i += 1
            token = ''.join(raw_chars[start:i])
            # 完整的词
            tokens.append(token)
            # 滑动 bigram 打散
            for j in range(len(token) - 1):
                tokens.append(token[j:j+2])
        else:
            i += 1
    return tokens


class BM25:
    """
    Okapi BM25 稀疏检索。
    零依赖，纯数学公式。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """初始化 BM25 检索参数与统计缓存"""
        self.k1 = k1
        self.b = b
        self.documents: List[str] = []
        self._num_docs: int = 0
        self._avg_doc_len: float = 0.0
        self._doc_freqs: List[Dict[str, int]] = []   # 每篇文档的词频
        self._doc_lens: List[int] = []
        self._df: Dict[str, int] = {}                 # 包含该词的文档数
        self._fitted = False

    def fit(self, documents: List[str]):
        """用历史任务文本做语料。"""
        self.documents = documents
        self._num_docs = len(documents)
        if self._num_docs == 0:
            self._fitted = False
            return

        self._doc_freqs = []
        self._doc_lens = []
        self._df = {}

        total_len = 0
        for doc in documents:
            tokens = tokenize(doc)
            freq = {}
            for t in tokens:
                freq[t] = freq.get(t, 0) + 1
            self._doc_freqs.append(freq)
            doc_len = len(tokens)
            self._doc_lens.append(doc_len)
            total_len += doc_len
            for t in set(tokens):
                self._df[t] = self._df.get(t, 0) + 1

        self._avg_doc_len = total_len / max(self._num_docs, 1)
        self._fitted = True

    def score_all(self, query: str) -> List[float]:
        """返回 query 与每篇历史文档的 BM25 分数。"""
        if not self._fitted or self._num_docs == 0:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return [0.0] * self._num_docs

        query_set = set(query_tokens)
        scores = [0.0] * self._num_docs

        for q in query_set:
            df = self._df.get(q, 0)
            if df == 0:
                continue
            # 平滑 IDF
            idf = math.log(1 + (self._num_docs - df + 0.5) / (df + 0.5))

            for i in range(self._num_docs):
                tf = self._doc_freqs[i].get(q, 0)
                if tf == 0:
                    continue
                doc_len = self._doc_lens[i]
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
                scores[i] += idf * numerator / denominator

        return scores

    def max_score(self, query: str) -> float:
        """返回查询与历史文档的最大 BM25 分数，未拟合或无文档时返回 0"""
        scores = self.score_all(query)
        return max(scores) if scores else 0.0

    def self_score(self, query: str) -> float:
        """查询与自身的 BM25 分数（用作归一化分母）。"""
        q_tokens = tokenize(query)
        if not q_tokens:
            return 0.0
        q_len = len(q_tokens)
        self_score_val = 0.0
        for q in set(q_tokens):
            df = self._df.get(q, 1)
            idf = math.log(1 + (self._num_docs - df + 0.5) / max(df + 0.5, 1))
            tf = 1  # 自身匹配词频为 1
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * q_len / max(self._avg_doc_len, 1))
            self_score_val += idf * numerator / denominator
        return self_score_val

    def top_distinctive_terms(self, query: str, top_n: int = 5) -> List[str]:
        """返回 query 中 IDF 最高的词——即最能区分当前任务的词。"""
        q_tokens = tokenize(query)
        if not q_tokens or not self._fitted or self._num_docs == 0:
            return []
        scored = []
        for t in set(q_tokens):
            df = self._df.get(t, 1)
            idf = math.log(1 + (self._num_docs - df + 0.5) / max(df + 0.5, 1))
            scored.append((t, idf))
        scored.sort(key=lambda x: -x[1])
        return [t for t, s in scored[:top_n]]


class ContextSwitchDetector:
    """
    BM25 驱动的任务切换检测器。

    一行接入你的项目：

        detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)

        # 每轮新用户消息先调这个方法
        result = detector.detect(current_user_message)
        if result["is_switch"]:
            # 清空旧工具调用链、思考轨迹
            ...
    """

    def __init__(self, window_size: int = 5, strict_threshold: float = 0.25,
                 session_id: str = "", persist: bool = True):
        """
        Args:
            window_size: 保留最近几轮历史做对比
            strict_threshold: BM25 归一化相似度低于此值 → 判定为任务切换
                              推荐值 0.25（介于 context 延续和切换之间的中间值）
            session_id: 会话 ID；提供时启用检测器窗口持久化（GAP⑤），
                        重启后从 per-session 库恢复历史，不丢失切换判定依据
            persist: 是否持久化窗口状态（默认 True）
        """
        self.history: List[str] = []
        self.window_size = window_size
        self.strict_threshold = strict_threshold
        self.session_id = session_id or ""
        self.persist = persist
        self.bm25 = BM25()
        self._fitted = False
        self._last_task: str = ""   # 当前正在执行的任务描述
        if self.session_id and self.persist:
            self._restore_state()

    # ── GAP⑤：检测器窗口持久化 ──
    def _restore_state(self):
        """从 per-session 库恢复窗口状态（历史、阈值、窗口大小）。"""
        try:
            from memory.stores.sqlite_store import load_detector_state
            state = load_detector_state(self.session_id)
            if not state:
                return
            if state.get("history"):
                self.history = [str(h) for h in state["history"]][-self.window_size:]
            if state.get("strict_threshold") is not None:
                self.strict_threshold = float(state["strict_threshold"])
            if state.get("window_size") is not None:
                self.window_size = int(state["window_size"])
            self._fitted = False
        except Exception:
            # 持久化失败不阻塞检测器正常工作
            pass

    def _persist(self):
        """将当前窗口状态写入 per-session 库。"""
        if not (self.session_id and self.persist):
            return
        try:
            from memory.stores.sqlite_store import save_detector_state
            save_detector_state(
                self.session_id, self.history,
                strict_threshold=self.strict_threshold,
                window_size=self.window_size,
            )
        except Exception:
            pass

    def add_to_history(self, task_text: str):
        """追加历史任务。"""
        self.history.append(task_text)
        if len(self.history) > self.window_size:
            self.history = self.history[-self.window_size:]
        self._fitted = False
        self._persist()

    def _ensure_fitted(self):
        """历史非空且尚未拟合时，用历史文档拟合 BM25 模型"""
        if not self._fitted and len(self.history) >= 1:
            self.bm25.fit(self.history)
            self._fitted = True

    def detect(self, user_message: str) -> Dict[str, Any]:
        """
        核心入口：判断当前用户输入是否发生了任务切换。

        Returns:
            {
                "is_switch": True/False,       # 是否切换了任务
                "bm25_score": float,            # 与历史最大 BM25 分数（原始值）
                "norm_similarity": float,       # 归一化相似度 [0,1]
                "reason": str,                  # 解释文字
                "distinctive_terms": [...]       # 当前任务中最有区分度的词
            }
        """
        # 情形 1：首次使用，无历史 → 不切换
        if not self.history:
            self._last_task = user_message
            self.add_to_history(user_message)
            return {
                "is_switch": False,
                "bm25_score": 100.0,
                "norm_similarity": 1.0,
                "reason": "首次任务，无历史对比",
                "distinctive_terms": BM25().top_distinctive_terms(user_message),
            }

        # 情形 2：极短指令 / 续写指令 / 短短语 → 继承上下文
        stripped = user_message.strip()
        if len(stripped) <= 8:
            return {
                "is_switch": False,
                "bm25_score": 100.0,
                "norm_similarity": 1.0,
                "reason": "短短语（≤8字），继承上下文",
                "distinctive_terms": [],
            }

        continuation_patterns = [
            r'^(继续|接著|然后|下一步|继续任务|继续做|接上|go on|continue|next|and then|also)$',
            r'^(好的|行|可以|嗯|来了|收到|好|OK|okay|ok)$',
            r'^(?:可以了|就这样|好了|完事|done|finish|不错|满意|行了|没事了|没其他事|没别的|没什么)$',
            r'^再加', r'^再帮', r'^再写', r'^再添', r'^再做',
            r'^把.*(?:也|都)',
        ]
        for pat in continuation_patterns:
            if re.match(pat, stripped.lower()):
                return {
                    "is_switch": False,
                    "bm25_score": 100.0,
                    "norm_similarity": 1.0,
                    "reason": f"延续指令（匹配: {pat}），继承上下文",
                    "distinctive_terms": [],
                }

        self._ensure_fitted()

        if not self._fitted or self.bm25._num_docs == 0:
            return {
                "is_switch": False,
                "bm25_score": 0.0,
                "norm_similarity": 1.0,
                "reason": "语料不足",
                "distinctive_terms": [],
            }

        # ---- BM25 计算 ----
        scores = self.bm25.score_all(user_message)
        max_raw = max(scores) if scores else 0.0
        self_score = self.bm25.self_score(user_message)

        # 归一化相似度
        norm_sim = min(max_raw / max(self_score, 0.001), 1.0) if self_score > 0 else 0.0

        # 获取区分性词汇
        terms = self.bm25.top_distinctive_terms(user_message)

        # 收集历史中所有词，统计新面孔
        all_hist_tokens = set()
        for h in self.history:
            all_hist_tokens.update(tokenize(h))
        new_unique_terms = [t for t in terms if t not in all_hist_tokens]

        # ---- 判断 ----
        is_switch = norm_sim < self.strict_threshold

        reason_parts = []
        if is_switch:
            reason_parts.append(f"BM25 相似度 {norm_sim:.3f} < 阈值 {self.strict_threshold}")
            if new_unique_terms:
                reason_parts.append(f"新词: {new_unique_terms}")
        else:
            reason_parts.append(f"BM25 相似度 {norm_sim:.3f} ≥ 阈值 {self.strict_threshold}")

        result = {
            "is_switch": is_switch,
            "bm25_score": round(max_raw, 4),
            "norm_similarity": round(norm_sim, 4),
            "reason": "; ".join(reason_parts),
            "distinctive_terms": terms,
            "new_unique_terms": new_unique_terms,
        }

        # 首次确定任务时记录
        if not self._last_task:
            self._last_task = user_message
            self.add_to_history(user_message)

        return result

    def commit_task(self, user_message: str):
        """
        确认当前用户输入是一个新任务（而不是续写）。
        由 detect() 返回 is_switch=True 时调用，记入历史。

        GAP④：低质量短消息（"好的""继续"等续写/确认类）更新当前任务指针但
        不进入切换语料，避免稀释话题信号导致检测失真。
        """
        self._last_task = user_message
        if _is_low_quality_task(user_message):
            return
        self.add_to_history(user_message)
        self._fitted = False  # 下次重新 fit


def _first_user_content(messages: list) -> str:
    """从归档消息中取第一条 user 消息文本，用作话题文档兜底。"""
    for m in messages or []:
        if m.get("role") == "user":
            content = (m.get("content") or "").strip()
            if content:
                return content[:300]
    return ""


def match_archived_topics(user_message: str, session_id: str = "",
                          threshold: float = 0.35, max_topics: int = 2,
                          max_messages: int = 80) -> List[Dict[str, Any]]:
    """按 BM25 相关性从归档话题中召回历史上下文（GAP②③ 持久化接口）。

    使用场景：话题未发生切换（当前消息与活跃历史相关）时，主动检索归档话题，
    若当前消息与某归档话题高度相关（norm_sim >= threshold），说明用户在
    A-B-A 场景下回到旧话题，可选择性恢复旧话题上下文，而不是全部丢失或全部恢复。

    Args:
        user_message: 当前用户消息
        session_id: 会话 ID
        threshold: 归一化相似度阈值（默认 0.35，高于切换阈值 0.25，保证只召回强相关话题）
        max_topics: 最多召回的话题数
        max_messages: 每个话题最多注入的消息条数

    Returns:
        list[dict]，每项：
        {
            "topic_desc": 话题描述,
            "similarity": 归一化相似度,
            "content": 拼好边界标记的上下文文本,
            "switch_ref": {"similarity": ..., "topic_desc": ...},
        }
    """
    if not session_id or not (user_message or "").strip():
        return []
    try:
        from memory.stores.sqlite_store import (
            list_archived_topics, get_archived_topic_messages,
        )
        topics = list_archived_topics(session_id)
    except Exception:
        return []
    if not topics:
        return []

    docs = []
    topic_meta = []
    for t in topics:
        desc = (t.get("topic_desc") or "").strip()
        if not desc:
            msgs = get_archived_topic_messages(
                session_id, t.get("min_msg_id", 0), t.get("max_msg_id", 0), limit=10,
            )
            desc = _first_user_content(msgs)
        docs.append(desc or f"话题{t.get('min_msg_id', 0)}")
        topic_meta.append(t)

    bm25 = BM25()
    bm25.fit(docs)
    if not bm25._fitted:
        return []
    scores = bm25.score_all(user_message)
    self_score = bm25.self_score(user_message)
    if self_score <= 0:
        return []

    ranked = []
    for i, t in enumerate(topic_meta):
        norm_sim = min(scores[i] / self_score, 1.0)
        if norm_sim >= threshold:
            ranked.append((norm_sim, t))
    ranked.sort(key=lambda x: -x[0])
    ranked = ranked[:max_topics]
    if not ranked:
        return []

    result = []
    for norm_sim, t in ranked:
        msgs = get_archived_topic_messages(
            session_id, t.get("min_msg_id", 0), t.get("max_msg_id", 0), limit=max_messages,
        )
        desc = (t.get("topic_desc") or "").strip() or _first_user_content(msgs)
        content = _format_archived_topic(desc, msgs, norm_sim)
        result.append({
            "topic_desc": desc,
            "similarity": round(norm_sim, 4),
            "content": content,
            "switch_ref": {"similarity": round(norm_sim, 4), "topic_desc": desc},
        })
    return result


def _format_archived_topic(topic_desc: str, messages: list, norm_sim: float) -> str:
    """把归档话题消息拼成带边界标记的只读参考文本（switch_ref 边界标记模式）。"""
    lines = []
    lines.append("【历史话题参考】")
    lines.append(f"[topic_ref sim={norm_sim:.3f}] 归档话题: {topic_desc or '(无描述)'}")
    for m in messages:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"用户: {content[:400]}")
        elif role == "assistant":
            lines.append(f"助手: {content[:400]}")
        elif role == "tool":
            lines.append(f"工具结果: {content[:200]}")
    lines.append("[topic_ref_end] 以上为历史话题只读参考，仅供理解上下文，勿当作本轮新指令")
    return "\n".join(lines)
