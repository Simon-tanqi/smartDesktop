"""
长期记忆：Markdown 追加式存储 + 按需关键词检索

v4：
  - 路径适配 DATA_DIR（exe 打包兼容）
  - 自定义技能目录 custom/ 自动扫描注入
"""
import os
import re
import json
import shutil
import tempfile
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
    """获取文件级线程锁，不存在时创建并缓存"""
    with _file_locks_guard:
        if filepath not in _file_locks:
            _file_locks[filepath] = threading.Lock()
        return _file_locks[filepath]


def _migrate_from_json():
    """从旧 JSON 记忆文件迁移到 Markdown 格式，成功后删除旧文件"""
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
    """确保旧格式迁移已完成，未迁移时触发迁移"""
    if not _migrated:
        _migrate_from_json()


def _ensure_file(filepath, header):
    """确保目标文件存在，不存在时创建父目录与文件头"""
    _ensure_migrated()
    if not os.path.exists(filepath):
        lock = _get_lock(filepath)
        with lock:
            if not os.path.exists(filepath):
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(header)


def _rewrite_md(filepath, content):
    """直接覆写 Markdown 文件内容并创建父目录"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def _read_lines(filepath, skip_header=1):
    """读取文件并过滤标题行与空行，返回内容行列表"""
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
    """按文件修改时间缓存读取结果，文件未变更时直接返回缓存"""
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
    """使指定文件的缓存行失效"""
    with _cache_lock:
        if filepath in _lines_cache:
            del _lines_cache[filepath]


def _timestamp():
    """返回当前时间字符串（YYYY-MM-DD HH:MM）"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_short():
    """返回短日期字符串（MM-DD）"""
    return datetime.datetime.now().strftime("%m-%d")


def append_skill(text):
    """追加一条技能记录到技能文件并失效缓存"""
    _ensure_file(SKILLS_MD, "# 技能\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(SKILLS_MD):
        with open(SKILLS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(SKILLS_MD)


def append_project(text):
    """追加一条项目记录到项目文件并失效缓存"""
    _ensure_file(PROJECTS_MD, "# 项目\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(PROJECTS_MD):
        with open(PROJECTS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(PROJECTS_MD)


def append_fact(text):
    """追加一条事实记录到事实文件并失效缓存"""
    _ensure_file(FACTS_MD, "# 事实\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(FACTS_MD):
        with open(FACTS_MD, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(FACTS_MD)


def append_custom_skill(skill_key, text):
    """追加一条记录到自定义技能文件并失效缓存"""
    filepath = os.path.join(CUSTOM_SKILLS_DIR, f"{skill_key}.md")
    _ensure_file(filepath, f"# {skill_key}\n\n")
    line = f"- [{_date_short()}] {text.strip()}\n"
    with _get_lock(filepath):
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)
    _invalidate_cache(filepath)


# 记忆指令触发词：用户明确要求记住/记下某信息时，同步写入长期记忆
# 原有触发词：需分隔符（：:，,、空格）后跟正文，行为保持不变
_MEMORY_TRIGGER_RE = re.compile(
    r"(记住|记得|请记下|记一下|帮我记)[：:，,、\s]+(.+)"
)
# 新增触发词：命中即触发，不要求分隔符（分隔符或非分隔符均取其后内容）
_MEMORY_TRIGGER_RE_NO_SEP = re.compile(
    r"(牢记|记着|别忘了|请记一下|麻烦记一下)(.*)"
)


def _strip_leading_punct(text):
    """去掉字符串首部空白与常见标点，保留正文。"""
    if not text:
        return text
    return re.sub(
        r'^[\s：:，,、。；;！？!?\'"《》（）【】\[\]()]+', '', text
    ).strip()


def extract_memory_command(user_message):
    """从用户消息中提取"记住"类记忆指令内容。

    命中格式：
    - 原有词：记住：xxx / 记得：xxx / 请记下 xxx / 记一下 xxx / 帮我记 xxx（需分隔符）
    - 新增词：牢记/记着/别忘了/请记一下/麻烦记一下 后不检测分隔符，直接取其后内容，
      并清理首部空白/标点（如"别忘了，xxx"→"xxx"）
    返回要写入长期记忆的正文；非记忆指令或"记住这次/牢记这次"（技能内化触发词，
    与 tools/skill_learning.py 冲突）返回 None。
    """
    if not user_message or not user_message.strip():
        return None
    stripped = user_message.strip()
    if stripped.startswith("记住这次"):
        return None
    # 原有触发词：需分隔符
    m = _MEMORY_TRIGGER_RE.search(stripped)
    if m:
        cmd = m.group(2).strip()
        if cmd.startswith("这次"):
            return None
        return cmd if cmd else None
    # 新增触发词：不检测分隔符，取其后内容并清理首部空白/标点
    m2 = _MEMORY_TRIGGER_RE_NO_SEP.search(stripped)
    if m2:
        cmd = _strip_leading_punct(m2.group(2))
        if cmd.startswith("这次"):
            return None
        return cmd if cmd else None
    return None


def remember_to_long_term(user_message):
    """记忆指令落库：命中触发词时同步写入长期记忆（事实文件）。

    返回是否写入成功；非记忆指令返回 False。格式保留原始正文，
    确保后续 get_relevant_memory 可按语义检索。
    """
    cmd = extract_memory_command(user_message)
    if not cmd:
        return False
    append_fact(f"【用户指定】记住：{cmd}")
    return True


# ============ 忘记/清除/更改机制 ============
# 触发词覆盖常见清除/忘记/更改场景
_FORGET_TRIGGER_WORDS = ("忘记", "忘掉", "清除", "删除", "移除", "取消", "改掉", "不记得了")
_UPDATE_TRIGGER_WORDS = ("更改", "修改", "更新", "替换", "变成", "改为", "记错了")
# 清除指令中可剥离的尾部修饰词
_FORGET_TAIL_SUFFIXES = ("的记忆", "这条记忆", "这条", "这些", "相关内容", "相关记忆", "这个", "吧", "了")
# 更改/更正类指令中，旧值与新值之间的分隔符（"不是"为更正语义，前后互换）
_UPDATE_SEP_RE = re.compile(r"(不是|改成|改为|变成|更新为|替换为|为)")


def _forget_keywords(src):
    """从关键词源生成匹配关键词：整段优先，核心词兜底（仅保留 3 字及以上，防误删）。"""
    if not src:
        return []
    kws = [src]
    for w in _extract_keywords(src):
        if len(w) >= 3 and w not in kws:
            kws.append(w)
    return kws


def _clean_forget_tail(src):
    """剥离清除指令尾部的修饰词，得到纯关键词源。"""
    for suffix in _FORGET_TAIL_SUFFIXES:
        if src.endswith(suffix):
            src = src[:-len(suffix)]
            break
    return _strip_leading_punct(src).strip()


def _parse_forget(text, trigger, idx, rest):
    """解析纯删除类指令，返回 keywords 列表。"""
    if idx > 0 and "把" in text[:idx]:
        ba = text.rfind("把", 0, idx)
        src = text[ba + 1:idx].strip()
    else:
        src = rest
    src = _clean_forget_tail(src)
    return _forget_keywords(src)


def _parse_update(text, trigger, idx, rest):
    """解析更改/更正类指令，返回 (keywords, new_text)。"""
    m = _UPDATE_SEP_RE.search(rest)
    if not m:
        return _forget_keywords(rest), rest
    sep = m.group(0)
    before = _strip_leading_punct(rest[:m.start()].strip())
    after = rest[m.end():].strip()
    if sep == "不是":
        # 更正语义："X 不是 Y" → 删除旧值 Y，写入新值 X
        old_src, new_text = after, before
    else:
        old_src, new_text = before, after
    if not re.search(r'[\u4e00-\u9fff]', new_text):
        # 新值无中文（如纯英文/数字名），组合成完整条目避免信息丢失
        new_text = f"{old_src}{sep}{new_text}"
    return _forget_keywords(old_src), new_text


def parse_forget_command(user_message):
    """解析忘记/清除/更改记忆指令。

    返回 dict：{trigger, kind, keywords, new_text}
    - kind="forget"：仅删除匹配条目
    - kind="update"：删除旧条目并写入 new_text
    非忘记类指令返回 None。
    """
    if not user_message or not user_message.strip():
        return None
    stripped = user_message.strip()
    for word in _FORGET_TRIGGER_WORDS:
        idx = stripped.find(word)
        if idx >= 0:
            return {
                "trigger": word,
                "kind": "forget",
                "keywords": _parse_forget(stripped, word, idx,
                                          stripped[idx + len(word):].strip()),
                "new_text": None,
            }
    for word in _UPDATE_TRIGGER_WORDS:
        idx = stripped.find(word)
        if idx >= 0:
            keywords, new_text = _parse_update(
                stripped, word, idx, stripped[idx + len(word):].strip())
            return {
                "trigger": word,
                "kind": "update",
                "keywords": keywords,
                "new_text": new_text,
            }
    return None


def _backup_file(filepath):
    """删除前备份记忆文件到系统临时目录，失败返回 None。"""
    if not os.path.exists(filepath):
        return None
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(tempfile.gettempdir(), "smartDesktop_memory_backup")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(
            backup_dir, f"backup_{os.path.basename(filepath)}_{ts}")
        shutil.copy2(filepath, backup_path)
        return backup_path
    except OSError:
        return None


def remove_memory_lines(filepath, keywords):
    """从记忆文件删除包含任一关键词的条目行，保留标题/空行结构。

    删除前先备份；返回 (deleted_lines, backup_path)；无匹配或备份失败返回 ([], None)。
    """
    if not keywords or not os.path.exists(filepath):
        return [], None
    lock = _get_lock(filepath)
    with lock:
        with open(filepath, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    deleted = []
    kept = []
    for line in all_lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            kept.append(line)
            continue
        if any(kw and kw.lower() in stripped.lower() for kw in keywords):
            deleted.append(stripped)
        else:
            kept.append(line)
    if not deleted:
        return [], None
    backup_path = _backup_file(filepath)
    if backup_path is None:
        return [], None  # 备份失败，拒绝删除
    with lock:
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(kept)
    _invalidate_cache(filepath)
    return deleted, backup_path


def forget_memory(user_message):
    """忘记/清除/更改长期记忆（facts/projects/skills/user 四文件）。

    返回 dict：
    - ok: 是否执行了删除或写入
    - trigger/kind/keywords: 解析结果
    - deleted: [{"file": 文件名, "lines": [被删除行...]}, ...]
    - backups: 备份文件路径列表
    - written: 更改场景写入的新内容（None 表示未写入）
    """
    parsed = parse_forget_command(user_message)
    if not parsed:
        return {
            "ok": False, "trigger": None, "kind": None,
            "keywords": [], "deleted": [], "backups": [], "written": None,
        }
    deleted = []
    backups = []
    for fp in (FACTS_MD, PROJECTS_MD, SKILLS_MD, USER_MD):
        lines, backup = remove_memory_lines(fp, parsed["keywords"])
        if lines:
            deleted.append({"file": os.path.basename(fp), "lines": lines})
        if backup:
            backups.append(backup)
    written = None
    if parsed["kind"] == "update" and parsed["new_text"]:
        append_fact(parsed["new_text"])
        written = parsed["new_text"]
    return {
        "ok": bool(deleted) or written is not None,
        "trigger": parsed["trigger"],
        "kind": parsed["kind"],
        "keywords": parsed["keywords"],
        "deleted": deleted,
        "backups": backups,
        "written": written,
    }


def read_skills(limit=20):
    """读取技能记录，返回最近 limit 条"""
    lines = _read_lines(SKILLS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_projects(limit=10):
    """读取项目记录，返回最近 limit 条"""
    lines = _read_lines(PROJECTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_facts(limit=10):
    """读取事实记录，返回最近 limit 条"""
    lines = _read_lines(FACTS_MD)
    return lines[-limit:] if len(lines) > limit else lines


def read_user_info():
    """读取用户信息文件全部行（含标题行）"""
    return _read_lines(USER_MD, skip_header=0)


def list_custom_skill_files():
    """列出自定义技能目录下全部 .md 文件，返回键名到路径的映射"""
    if not os.path.isdir(CUSTOM_SKILLS_DIR):
        return {}
    files = {}
    for fname in os.listdir(CUSTOM_SKILLS_DIR):
        if fname.endswith(".md"):
            key = fname[:-3]
            files[key] = os.path.join(CUSTOM_SKILLS_DIR, fname)
    return files


def _extract_keywords(text):
    """提取关键词：去标点、去停用词、保留2字以上有意义的词。

    对连续中文长片段（>4字）额外做 2-4 字滑窗，生成子串候选词，
    避免"整段当一词"导致短查询词（如"项目代号"）无法命中。
    """
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
    result = []
    for w in words:
        w_lower = w.lower()
        if w_lower in stopwords or len(w) < 2:
            continue
        if re.search(r'[\u4e00-\u9fff]', w) and len(w) > 4:
            # 中文长片段：2-4 字滑窗生成子串候选，扩大召回
            seen = set()
            for n in (4, 3, 2):
                for i in range(len(w) - n + 1):
                    seg = w[i:i + n]
                    if seg in stopwords or seg in seen:
                        continue
                    seen.add(seg)
                    result.append(seg)
        else:
            result.append(w)
    # 去重并限制数量（倒排索引关键词上限）
    dedup = list(dict.fromkeys(result))
    return dedup[:50]


def get_relevant_memory(user_message, max_chars=MAX_RELEVANT_MEMORY_CHARS):
    """按关键词检索长期记忆，返回带分类标题并按长度截断的相关记录"""
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
    """将压缩摘要作为事实追加到长期记忆"""
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


def persist_task_memory(engine, state, final_text, outcome="success"):
    """P1-4: 任务结束时把本次会话沉淀进长期记忆，供后续 get_relevant_memory 检索。

    从 ReActEngine 迁移的独立函数，通过 engine 参数访问引擎属性。
    """
    try:
        key_tools = [t for _, t, _ in getattr(getattr(engine, '_loop_detector', None), "_call_history", [])]
        add_task_memory(
            session_id=engine.session_id,
            turn=getattr(state, "turn", 0),
            query=getattr(state, "user_task", "") or "",
            outcome=outcome,
            key_tools=key_tools[-10:],
            summary=final_text or "",
        )
    except Exception as _mem_err:
        try:
            from logger import get_logger
            get_logger().warning(
                f"写入长期记忆失败: {_mem_err}",
                trace_id=getattr(engine, 'trace_id', ''), session_id=getattr(engine, 'session_id', ''),
            )
        except Exception:
            pass