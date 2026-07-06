"""
长期记忆：Markdown 追加式存储 + 按需关键词检索

v4：
  - 路径适配 DATA_DIR（exe 打包兼容）
  - 自定义技能目录 custom/ 自动扫描注入
  - 保留摘要全局 summaries.md（供跨会话参考）
"""
import os
import re
import json
import datetime
import threading

from memory.config import (
    USER_MD, PROJECTS_MD, SKILLS_MD, FACTS_MD, SUMMARIES_MD,
    CUSTOM_SKILLS_DIR, MAX_RELEVANT_MEMORY_CHARS,
    MEMORY_FILE, COMPRESSED_FILE,
)

# ── 一次性迁移锁 ──
_migrated = False
_migrate_lock = threading.Lock()

# ── 按文件写入锁 ──
_file_locks = {}
_file_locks_guard = threading.Lock()


def _get_lock(filepath):
    with _file_locks_guard:
        if filepath not in _file_locks:
            _file_locks[filepath] = threading.Lock()
        return _file_locks[filepath]


# ═══════════════════════════════════════════════
# 初始化 + 迁移
# ═══════════════════════════════════════════════

def _migrate_from_json():
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        _migrated = True

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
                print(f"[Memory] 已从 {MEMORY_FILE} 迁移到 Markdown")
                try:
                    os.remove(MEMORY_FILE)
                except OSError:
                    pass
            except Exception as e:
                print(f"[Memory] 迁移长期记忆失败: {e}")

        if os.path.exists(COMPRESSED_FILE):
            try:
                with open(COMPRESSED_FILE, "r", encoding="utf-8") as f:
                    old_compressed = json.load(f)
                for entry in old_compressed:
                    summary = entry.get("summary", "")
                    if summary.strip():
                        append_summary(summary)
                print(f"[Memory] 已从 {COMPRESSED_FILE} 迁移 {len(old_compressed)} 条摘要")
                try:
                    os.remove(COMPRESSED_FILE)
                except OSError:
                    pass
            except Exception as e:
                print(f"[Memory] 迁移压缩记忆失败: {e}")


# ═══════════════════════════════════════════════
# 文件工具
# ═══════════════════════════════════════════════

def _ensure_migrated():
    if not _migrated:
        _migrate_from_json()


def _ensure_file(filepath, header):
    _ensure_migrated()
    if not os.path.exists(filepath):
        lock = _get_lock(filepath)
        with lock:
            if not os.path.exists(filepath):
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(header)


def _rewrite_md(filepath, content):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def _read_lines(filepath, skip_header=1):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if skip_header and (stripped.startswith("#") or not stripped):
            continue
        result.append(stripped)
    return result


# Token优化：mtime 缓存避免每次请求都重新读取 .md 文件
_lines_cache: dict = {}  # {filepath: (mtime, lines)}}


def _read_lines_cached(filepath, skip_header=1):
    """与 _read_lines 行为相同，但缓存结果直到文件 mtime 变化。"""
    try:
        mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
    except OSError:
        mtime = 0
    if filepath in _lines_cache:
        cached_mtime, cached_lines = _lines_cache[filepath]
        if cached_mtime == mtime:
            return cached_lines
    lines = _read_lines(filepath, skip_header=skip_header)
    _lines_cache[filepath] = (mtime, lines)
    return lines


def _timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_short():
    return datetime.datetime.now().strftime("%m-%d")


# ═══════════════════════════════════════════════
# 追加写入
# ═══════════════════════════════════════════════

def append_skill(text):
    _ensure_file(SKILLS_MD, "# 技能\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(SKILLS_MD):
        with open(SKILLS_MD, "a", encoding="utf-8") as f:
            f.write(line)


def append_project(text):
    _ensure_file(PROJECTS_MD, "# 项目\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(PROJECTS_MD):
        with open(PROJECTS_MD, "a", encoding="utf-8") as f:
            f.write(line)


def append_fact(text):
    _ensure_file(FACTS_MD, "# 事实\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(FACTS_MD):
        with open(FACTS_MD, "a", encoding="utf-8") as f:
            f.write(line)


def append_summary(text):
    _ensure_file(SUMMARIES_MD, "# 历史对话摘要\n\n")
    line = f"- [{_timestamp()}] {text.strip()}\n"
    with _get_lock(SUMMARIES_MD):
        with open(SUMMARIES_MD, "a", encoding="utf-8") as f:
            f.write(line)


def append_custom_skill(skill_key, text):
    """追加到自定义技能文件。"""
    filepath = os.path.join(CUSTOM_SKILLS_DIR, f"{skill_key}.md")
    _ensure_file(filepath, f"# {skill_key}\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(filepath):
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)


# ═══════════════════════════════════════════════
# 读取
# ═══════════════════════════════════════════════

def read_skills(limit=20):
    lines = _read_lines(SKILLS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_projects(limit=10):
    lines = _read_lines(PROJECTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_facts(limit=10):
    lines = _read_lines(FACTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_summaries(days=7):
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
    return _read_lines(USER_MD, skip_header=0)


def list_custom_skill_files():
    """列出 custom/ 下所有 .md 文件。"""
    if not os.path.isdir(CUSTOM_SKILLS_DIR):
        return {}
    files = {}
    for fname in os.listdir(CUSTOM_SKILLS_DIR):
        if fname.endswith(".md"):
            key = fname[:-3]
            files[key] = os.path.join(CUSTOM_SKILLS_DIR, fname)
    return files


# ═══════════════════════════════════════════════
# 按需注入
# ═══════════════════════════════════════════════

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
    words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
    result = []
    seen = set()
    for w in words:
        if len(w) >= 2:
            if w not in seen:
                seen.add(w)
                result.append(w)
    return result


def _detect_time_window(text):
    now = datetime.datetime.now()
    today = now.date()

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            target = today.replace(month=month, day=day)
            if target > today:
                target = target.replace(year=today.year - 1)
            return target, target
        except ValueError:
            pass

    m = re.search(r"(?:近|最近)\s*(\d+)\s*天", text)
    if m:
        n = int(m.group(1))
        return today - datetime.timedelta(days=n), today

    m = re.search(r"(?:近|最近)\s*(\d+)\s*周", text)
    if m:
        n = int(m.group(1))
        return today - datetime.timedelta(weeks=n), today

    m = re.search(r"(?:近|最近)\s*(\d+)\s*个?月", text)
    if m:
        n = int(m.group(1))
        year, month = today.year, today.month
        month -= n
        while month <= 0:
            month += 12
            year -= 1
        return datetime.date(year, month, 1), today

    if re.search(r"今[日天]", text):
        return today, today
    if re.search(r"昨[日天]", text):
        return today - datetime.timedelta(days=1), today - datetime.timedelta(days=1)
    if re.search(r"前[日天]", text):
        return today - datetime.timedelta(days=2), today - datetime.timedelta(days=2)
    if re.search(r"最近|近期|这几天|这一阵", text):
        return today - datetime.timedelta(days=7), today
    if re.search(r"本周|这周|这星期", text):
        monday = today - datetime.timedelta(days=today.weekday())
        return monday, today
    if re.search(r"上周|上星期", text):
        last_monday = today - datetime.timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + datetime.timedelta(days=6)
        return last_monday, last_sunday
    if re.search(r"本月|这个月", text):
        return today.replace(day=1), today
    if re.search(r"上月|上个月", text):
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - datetime.timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)
        return first_of_prev, last_of_prev

    return None, None


def _parse_summary_timestamp(line):
    m = re.match(r"- \[(\d{4}-\d{2}-\d{2})\s\d{2}:\d{2}\]", line)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def get_relevant_memory(user_message, max_chars=MAX_RELEVANT_MEMORY_CHARS):
    """
    从所有 Markdown 文件 + 自定义技能目录中检索相关内容。
    返回格式化字符串，可直接拼入 system prompt。
    
    修改：长期记忆不多的情况下，全部拼接，不进行关键词过滤
    """
    _ensure_migrated()

    sections = []

    # 用户基本信息（始终注入）
    user_lines = _read_lines_cached(USER_MD, skip_header=0)
    if user_lines:
        sections.append(("用户", user_lines))

    # 基础类别 - 全部加载（长期记忆不多的情况下）
    categories = [
        ("技能", SKILLS_MD),
        ("项目", PROJECTS_MD),
        ("事实", FACTS_MD),
        # 摘要已迁移到 per-session summaries.db，不再从此文件读取
    ]

    # 自定义技能文件
    for key, filepath in list_custom_skill_files().items():
        categories.append((f"技能: {key}", filepath))

    # 加载所有类别的内容（不进行关键词过滤）
    for cat_name, filepath in categories:
        lines = _read_lines_cached(filepath)
        if lines:
            # 取最近的内容，避免过长
            recent = lines[-20:] if len(lines) > 20 else lines
            sections.append((cat_name, recent))

    # 如果没有找到任何内容，返回空字符串（用户信息已在sections中）
    if not sections:
        return ""

    parts = []
    total_chars = 0
    for cat_name, lines in sections:
        parts.append(f"【{cat_name}】")
        for line in lines:
            # 先检查再追加，避免超预算后仍写入当前行
            if total_chars + len(line) > max_chars:
                return "\n".join(parts)
            parts.append(line)
            total_chars += len(line)

    return "\n".join(parts)


# ═══════════════════════════════════════════════
# 兼容旧接口
# ═══════════════════════════════════════════════

def add_compressed_summary(summary, source_type="auto"):
    prefix = "【自动压缩】" if source_type == "auto" else ""
    append_summary(f"{prefix}{summary}")
