"""
长期记忆：Markdown 追加式存储 + 按需关键词检索

v4：
  - 路径适配 DATA_DIR（exe 打包兼容）
  - 自定义技能目录 custom/ 自动扫描注入
"""
import os
import re
import json
import datetime
import threading

_cache_lock = threading.Lock()

from memory.config import (
    USER_MD, PROJECTS_MD, SKILLS_MD, FACTS_MD,
    CUSTOM_SKILLS_DIR, MAX_RELEVANT_MEMORY_CHARS,
    MEMORY_FILE, COMPRESSED_FILE,
)

_migrated = False
_migrate_lock = threading.Lock()

_file_locks = {}
_file_locks_guard = threading.Lock()


def _get_lock(filepath):
    with _file_locks_guard:
        if filepath not in _file_locks:
            _file_locks[filepath] = threading.Lock()
        return _file_locks[filepath]


def _migrate_from_json():
    global _migrated
    with _migrate_lock:
        if _migrated:
            return

        success = False
        try:
            if os.path.exists(MEMORY_FILE):
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

            if os.path.exists(COMPRESSED_FILE):
                with open(COMPRESSED_FILE, "r", encoding="utf-8") as f:
                    old_compressed = json.load(f)
                for entry in old_compressed:
                    summary = entry.get("summary", "")
                    if summary.strip():
                        append_fact(f"[摘要] {summary}")
                print(f"[Memory] 已从 {COMPRESSED_FILE} 迁移 {len(old_compressed)} 条摘要")
                try:
                    os.remove(COMPRESSED_FILE)
                except OSError:
                    pass

            success = True
        except Exception as e:
            print(f"[Memory] 迁移长期记忆失败: {e}")

        if success:
            _migrated = True


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
    lock = _get_lock(filepath)
    with lock:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if skip_header and (stripped.startswith("#") or not stripped):
            continue
        result.append(stripped)
    return result


_lines_cache: dict = {}


def _read_lines_cached(filepath, skip_header=1):
    try:
        mtime = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
    except OSError:
        mtime = 0
    with _cache_lock:
        if filepath in _lines_cache:
            cached_mtime, cached_lines = _lines_cache[filepath]
            if cached_mtime == mtime:
                return cached_lines
    lines = _read_lines(filepath, skip_header=skip_header)
    with _cache_lock:
        _lines_cache[filepath] = (mtime, lines)
    return lines


def _invalidate_cache(filepath):
    with _cache_lock:
        if filepath in _lines_cache:
            del _lines_cache[filepath]


def _timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_short():
    return datetime.datetime.now().strftime("%m-%d")


def append_skill(text):
    _ensure_file(SKILLS_MD, "# 技能\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(SKILLS_MD):
        with open(SKILLS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(SKILLS_MD)


def append_project(text):
    _ensure_file(PROJECTS_MD, "# 项目\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(PROJECTS_MD):
        with open(PROJECTS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(PROJECTS_MD)


def append_fact(text):
    _ensure_file(FACTS_MD, "# 事实\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(FACTS_MD):
        with open(FACTS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(FACTS_MD)


def append_custom_skill(skill_key, text):
    filepath = os.path.join(CUSTOM_SKILLS_DIR, f"{skill_key}.md")
    _ensure_file(filepath, f"# {skill_key}\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(filepath):
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(filepath)


def read_skills(limit=20):
    lines = _read_lines(SKILLS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_projects(limit=10):
    lines = _read_lines(PROJECTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_facts(limit=10):
    lines = _read_lines(FACTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_user_info():
    return _read_lines(USER_MD, skip_header=0)


def list_custom_skill_files():
    if not os.path.isdir(CUSTOM_SKILLS_DIR):
        return {}
    files = {}
    for fname in os.listdir(CUSTOM_SKILLS_DIR):
        if fname.endswith(".md"):
            key = fname[:-3]
            files[key] = os.path.join(CUSTOM_SKILLS_DIR, fname)
    return files


def _extract_keywords(text):
    """提取关键词：去标点、去停用词、保留2字以上有意义的词"""
    clean = re.sub(r'[^\w\u4e00-\u9fff]', ' ', text)
    words = re.findall(r'[\w\u4e00-\u9fff]+', clean)
    stopwords = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
        "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
        "着", "没有", "看", "好", "自己", "这", "他", "她", "它", "我们",
        "他们", "对", "与", "为", "把", "被", "让", "从", "那", "吗", "吧",
        "呢", "啊", "哦", "嗯", "什么", "怎么", "如何", "为什么", "可以",
        "能", "该", "个", "些", "里", "中", "后", "前", "时", "将", "要",
        "还", "已经", "正在", "这个", "那个", "这些", "那些", "做", "用",
        "没", "可能", "向", "比", "更", "最", "太", "很", "非常", "比较",
        "因为", "所以", "但", "是", "不", "如果", "虽然", "然后", "而且",
        "或者", "还是", "但是", "可以", "应该", "需要", "经过", "通过",
    }
    return [w for w in words if w.lower() not in stopwords and len(w) >= 2]


def get_relevant_memory(user_message, max_chars=MAX_RELEVANT_MEMORY_CHARS):
    _ensure_migrated()

    keywords = _extract_keywords(user_message)
    if not keywords:
        return ""

    sections = []

    user_lines = _read_lines_cached(USER_MD, skip_header=0)
    if user_lines:
        sections.append(("用户", user_lines))

    categories = [
        ("技能", SKILLS_MD),
        ("项目", PROJECTS_MD),
        ("事实", FACTS_MD),
    ]

    for key, filepath in list_custom_skill_files().items():
        categories.append((f"技能: {key}", filepath))

    for cat_name, filepath in categories:
        lines = _read_lines_cached(filepath)
        if lines:
            recent = lines[-20:] if len(lines) > 20 else lines
            sections.append((cat_name, recent))

    if not sections:
        return ""

    # 按关键词匹配数打分，仅保留有关联的记录
    scored = []
    for cat_name, lines in sections:
        for line in lines:
            match_count = sum(1 for kw in keywords if kw.lower() in line.lower())
            if match_count > 0:
                scored.append((match_count, cat_name, line))

    if not scored:
        return ""

    # 按分数降序排列
    scored.sort(key=lambda x: -x[0])

    # 按分数截断，尽量保留整条句子
    parts = []
    total_chars = 0
    current_header = None

    for score, cat_name, line in scored:
        # 是否需要加分类标题
        if cat_name != current_header:
            header = f"【{cat_name}】"
            if total_chars + len(header) > max_chars:
                break
            parts.append(header)
            total_chars += len(header)
            current_header = cat_name

        if total_chars + len(line) > max_chars:
            # 尝试保留到句子边界
            remaining = max_chars - total_chars
            if remaining > 20:
                truncated = line[:remaining]
                for sep in ("。", "！", "？", "\n", ".", "!", "?"):
                    idx = truncated.rfind(sep)
                    if idx > 0:
                        parts.append(truncated[:idx + 1])
                        break
            break

        parts.append(line)
        total_chars += len(line)

    return "\n".join(parts)


def add_compressed_summary(summary, source_type="auto"):
    prefix = "【自动压缩】" if source_type == "auto" else ""
    append_fact(f"{prefix}{summary}")


def add_task_memory(session_id, turn, query, outcome="success",
                    key_tools=None, summary="", timestamp=None):
    """任务结束沉淀：将本次任务的 query/结果/关键工具写入长期记忆（事实库）。

    追加到 FACTS_MD，供 get_relevant_memory 关键词检索。
    """
    key_tools = key_tools or []
    if isinstance(key_tools, (list, tuple, set)):
        key_tools = ",".join(str(k) for k in key_tools if k)
    ts = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    query_s = str(query).strip().replace("\n", " ")[:200]
    summary_s = str(summary).strip().replace("\n", " ")[:300]

    line = (f"【任务】[{ts}] sid={session_id} turn={turn} "
            f"结果={outcome} 查询={query_s}")
    if key_tools:
        line += f" 工具={key_tools}"
    if summary_s:
        line += f" 摘要={summary_s}"
    append_fact(line)
    return line