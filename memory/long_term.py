"""
长期记忆：Markdown 追加式存储 + 按需检索

替代原 json_store.py 中的 load_long_term / save_long_term / load_compressed / save_compressed。

原则：
  存储格式 — Markdown 纯文本，人可读可编辑
  读写策略 — 只追加（append-only），不重写整个文件
  注入策略 — 按需注入，不全量塞 system prompt（关键词匹配 + 字符上限）
  首次启动 — 自动从旧 JSON 迁移
"""
import os
import re
import json
import datetime
import threading

from memory.config import (
    USER_MD, PROJECTS_MD, SKILLS_MD, FACTS_MD, SUMMARIES_MD,
    MEMORY_FILE, COMPRESSED_FILE, MAX_RELEVANT_MEMORY_CHARS,
)

# ── 一次性迁移锁 ──
_migrated = False
_migrate_lock = threading.Lock()


# ═══════════════════════════════════════════════
# 初始化 + 迁移
# ═══════════════════════════════════════════════

def _migrate_from_json():
    """一次性从旧 JSON 迁移到 Markdown。"""
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        _migrated = True

        # 迁移长期记忆 (user_memory.json → *.md)
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)

                user_info = old.get("user_info", {})
                if user_info:
                    name = user_info.get("name", "")
                    job = user_info.get("job_title", "")
                    company = user_info.get("company", "")
                    user_lines = [
                        f"- 姓名: {name or '(未设置)'}",
                        f"- 职位: {job or '(未设置)'}",
                        f"- 公司: {company or '(未设置)'}",
                    ]
                    _rewrite_md(USER_MD, "# 用户信息\n\n" + "\n".join(user_lines) + "\n\n# 偏好\n\n(无)\n")

                for proj in old.get("projects", []):
                    append_project(str(proj))

                for skill in old.get("core_knowledge", []):
                    append_skill(str(skill))

                for pref in old.get("preferences", []):
                    append_fact(str(pref))

                for fact in old.get("facts", []):
                    append_fact(str(fact))

                print(f"[Memory] 已从 {MEMORY_FILE} 迁移到 Markdown 文件")
                # 迁移后删除旧 JSON，避免 debug reloader 重启时重复迁移
                try:
                    os.remove(MEMORY_FILE)
                except OSError:
                    pass
            except Exception as e:
                print(f"[Memory] 迁移长期记忆失败: {e}")

        # 迁移压缩记忆 (compressed_memory.json → summaries.md)
        if os.path.exists(COMPRESSED_FILE):
            try:
                with open(COMPRESSED_FILE, "r", encoding="utf-8") as f:
                    old_compressed = json.load(f)

                for entry in old_compressed:
                    summary = entry.get("summary", "")
                    if summary.strip():
                        append_summary(summary)

                print(f"[Memory] 已从 {COMPRESSED_FILE} 迁移 {len(old_compressed)} 条摘要")
                # 迁移后删除旧 JSON，避免 debug reloader 重启时重复迁移
                try:
                    os.remove(COMPRESSED_FILE)
                except OSError:
                    pass
            except Exception as e:
                print(f"[Memory] 迁移压缩记忆失败: {e}")


# ═══════════════════════════════════════════════
# 内部文件工具
# ═══════════════════════════════════════════════

def _ensure_migrated():
    if not _migrated:
        _migrate_from_json()


def _ensure_file(filepath, header):
    """确保 Markdown 文件存在，否则创建并写入标题。"""
    _ensure_migrated()
    if not os.path.exists(filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header)


def _rewrite_md(filepath, content):
    """重写整个 Markdown 文件（仅迁移时使用，运行时禁止）。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def _read_lines(filepath, skip_header=1):
    """读取 Markdown 文件的行（跳过标题和空行）。"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # 跳过标题行（以 # 开头）和空行
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        result.append(stripped)
    return result


def _timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_short():
    return datetime.datetime.now().strftime("%m-%d")


# ═══════════════════════════════════════════════
# 追加写入（append-only）
# ═══════════════════════════════════════════════

def append_skill(text):
    """追加一条技能。"""
    _ensure_file(SKILLS_MD, "# 技能\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with open(SKILLS_MD, "a", encoding="utf-8") as f:
        f.write(line)


def append_project(text):
    """追加一条项目。"""
    _ensure_file(PROJECTS_MD, "# 项目\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with open(PROJECTS_MD, "a", encoding="utf-8") as f:
        f.write(line)


def append_fact(text):
    """追加一条一般事实。"""
    _ensure_file(FACTS_MD, "# 事实\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with open(FACTS_MD, "a", encoding="utf-8") as f:
        f.write(line)


def append_summary(text):
    """追加一条对话摘要（含时间戳）。"""
    _ensure_file(SUMMARIES_MD, "# 历史对话摘要\n\n")
    line = f"- [{_timestamp()}] {text.strip()}\n"
    with open(SUMMARIES_MD, "a", encoding="utf-8") as f:
        f.write(line)


# ═══════════════════════════════════════════════
# 读取（带分页 / 限制）
# ═══════════════════════════════════════════════

def read_skills(limit=20):
    """读取最近 N 条技能。"""
    lines = _read_lines(SKILLS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_projects(limit=10):
    """读取最近 N 条项目。"""
    lines = _read_lines(PROJECTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_facts(limit=10):
    """读取最近 N 条事实。"""
    lines = _read_lines(FACTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_summaries(days=7):
    """读取最近 N 天的对话摘要。"""
    lines = _read_lines(SUMMARIES_MD)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    recent = []
    for line in lines:
        m = re.match(r"\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})\]\s*(.*)", line)
        if m:
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                if ts >= cutoff:
                    recent.append(m.group(2).strip())
            except ValueError:
                continue
    return recent


def read_user_info():
    """读取 user.md 中的所有行。"""
    return _read_lines(USER_MD, skip_header=0)


# ═══════════════════════════════════════════════
# 按需注入（核心）
# ═══════════════════════════════════════════════

# 中文停用词（高频无意义词）
_STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "们", "这", "那",
    "个", "有", "不", "在", "和", "就", "都", "也", "要", "会", "能",
    "可以", "还", "没", "很", "吗", "啊", "吧", "呢", "哦", "什么",
    "怎么", "为什么", "这个", "那个", "一个", "一些", "一下", "如果",
    "因为", "所以", "但是", "然后", "而且", "虽然", "不过", "或者",
    "请", "帮", "来", "去", "给", "让", "把", "被", "用", "到", "对",
    "从", "向", "跟", "与", "为", "以", "通过", "根据", "关于",
}


def _extract_keywords(text):
    """从用户消息中提取关键词。"""
    # 简单分词：按中英文混合切分
    words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
    result = []
    for w in words:
        if len(w) > 1 and w not in _STOPWORDS:
            result.append(w)
    # 去重保持顺序
    seen = set()
    unique = []
    for w in result:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique


def get_relevant_memory(user_message, max_chars=MAX_RELEVANT_MEMORY_CHARS):
    """
    根据用户消息，从所有 Markdown 文件中提取相关内容。

    策略：
    1. 提取用户消息的关键词
    2. 对每个 Markdown 文件逐行检查关键词命中
    3. 命中行计入结果（去重，字符上限控制）
    4. 保留 user.md 中的基本信息始终注入

    返回：格式化后的字符串（可直接拼入 system prompt），无相关内容时返回空字符串。
    """
    _ensure_migrated()

    keywords = _extract_keywords(user_message)
    if not keywords:
        keywords = [user_message[:10]]

    sections = []

    # ── 用户基本信息（始终注入）─
    user_lines = _read_lines(USER_MD, skip_header=0)
    if user_lines:
        sections.append(("用户", user_lines))

    # ── 按类别检索 ──
    categories = [
        ("技能", SKILLS_MD),
        ("项目", PROJECTS_MD),
        ("事实", FACTS_MD),
        ("摘要", SUMMARIES_MD),
    ]

    for cat_name, filepath in categories:
        lines = _read_lines(filepath)
        if not lines:
            continue
        # 最近优先：倒序检查最近 30 条
        recent = lines[-30:] if len(lines) > 30 else lines
        hits = []
        for line in recent[::-1]:  # 最新在前
            line_lower = line.lower()
            if any(k.lower() in line_lower for k in keywords):
                hits.append(line)
                if len(hits) >= 5:
                    break
        if hits:
            sections.append((cat_name, hits[::-1]))  # 恢复时间正序

    if not sections:
        return ""

    # ── 拼接输出 ──
    parts = []
    total_chars = 0
    for cat_name, lines in sections:
        cat_header = f"【{cat_name}】"
        parts.append(cat_header)
        for line in lines:
            if total_chars > max_chars:
                return "\n".join(parts)
            parts.append(line)
            total_chars += len(line)

    return "\n".join(parts)


# ═══════════════════════════════════════════════
# 兼容旧接口（供 extractor.py 迁移过渡）
# ═══════════════════════════════════════════════

def add_compressed_summary(summary, source_type="auto"):
    """兼容旧 compressor 接口，改为追加到 summaries.md。"""
    prefix = "【自动压缩】" if source_type == "auto" else ""
    append_summary(f"{prefix}{summary}")
