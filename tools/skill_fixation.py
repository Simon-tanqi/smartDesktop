"""技能习惯固化：被动式习惯技能自动固化与 L1 直连执行。

与 skill_learning.py（主动式"保存为技能"，LLM 生成 Markdown）互补：
本模块不依赖 LLM，纯规则驱动。每次成功执行把 (query, tool_seq, params, date)
记入 skill_usage；对 query 做槽位抽象（相对日期/绝对日期/数字/文件名 →
<date>/<num>/<name> 占位符）得到 query_template；某模板在最近
SKILL_WINDOW_DAYS 天窗口内 >= SKILL_MIN_DAYS 个"干净日期"（当天仅 1 次、
无当天重复追问）即固化为 skills/<name>.yaml（trigger_template + slots +
steps + param_resolver 语义，模板 + 槽位，不存死参数）；之后新 query 归一化
命中固化模板时，用 param_resolver 现填槽位直接顺序执行 steps，不调规划 LLM。

存储于全局库 EXPERIENCE_DB（与 episode_store 同库，复用
sqlite_store._get_conn_simple 低频全局库连接模式），新增表 skill_usage /
skill_fixations，与 per-session messages.db 隔离。
"""
import os
import re
import json
import datetime
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from memory.config import EXPERIENCE_DB
from memory.context_router import BM25
from memory.stores.sqlite_store import _get_conn_simple
from config.settings import (
    SKILL_FIXATION_ENABLED, SKILL_USAGE_ENABLED,
    SKILL_WINDOW_DAYS, SKILL_MIN_DAYS,
    SKILL_BM25_THRESHOLD, SKILL_BM25_ENABLED, SKILL_WHITELIST_TOOLS,
)
from tools import register_tool

# 固化技能存储目录（项目根新建 skills，与 agent_skills 主动式技能目录分离）
SKILLS_DIR = Path(__file__).parent.parent / "skills"

# 固化快路径低风险工具白名单：仅白名单内工具允许被固化为技能并在命中后直连执行。
# 内置成员 = 温和工具（L1 三预设路由工具） + 本模块只读辅助工具；
# 环境变量 SKILL_WHITELIST_TOOLS 可追加（逗号分隔）。
LOW_RISK_TOOL_WHITELIST = {
    "query_yesterday_weather",
    "set_system_volume",
    "set_desktop_icons_visible",
    "list_fixed_skills",
}
if SKILL_WHITELIST_TOOLS:
    for _extra in SKILL_WHITELIST_TOOLS.split(","):
        _extra = _extra.strip()
        if _extra:
            LOW_RISK_TOOL_WHITELIST.add(_extra)

# 相对日期词 -> 偏移（与 resolve 字符串保持一致）
_RELATIVE_DATE_MAP = {
    "今天": 0, "今日": 0,
    "昨天": -1, "昨日": -1,
    "前天": -2,
    "明天": 1, "明日": 1,
    "后天": 2,
}
# 相对日期槽位的 resolve 规则文本（写入 YAML，供 param_resolver 解析）
_RELATIVE_DATE_RESOLVE = (
    "今天->today-0,今日->today-0,昨天->today-1,昨日->today-1,"
    "前天->today-2,明天->today+1,明日->today+1,后天->today+2"
)
# 绝对日期 / 数字 / 文件名槽位的 resolve 规则文本
_ABSOLUTE_DATE_RESOLVE = "解析为实际日期 YYYY-MM-DD"
_NUM_RESOLVE = "int(槽位值)"
_NAME_RESOLVE = "原样使用文件名"

# 槽位类型常量
KIND_RELATIVE_DATE = "relative_date"
KIND_ABSOLUTE_DATE = "absolute_date"
KIND_NUM = "num"
KIND_NAME = "name"

_SKILL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT DEFAULT '',
    query TEXT NOT NULL,
    query_template TEXT NOT NULL,
    tool_seq TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    usage_date TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_tpl_date ON skill_usage(query_template, usage_date);
"""

_SKILL_FIXATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_fixations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL,
    name TEXT NOT NULL,
    trigger_template TEXT NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_fixations_tpl ON skill_fixations(template);
"""


def _now() -> str:
    """当前时间戳（本地时间字符串）。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _init_skill_tables() -> None:
    """建表（幂等）。"""
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        conn.executescript(_SKILL_USAGE_SCHEMA)
        conn.executescript(_SKILL_FIXATIONS_SCHEMA)


# ═══════════════════════════════════════════════
#  槽位抽象
# ═══════════════════════════════════════════════

# 相对日期词（长词优先，避免"明日"被"今"类误切）
_RELATIVE_DATE_RE = re.compile(r"(昨天|前天|今天|明日|明天|后天|昨日|今日)")
# 绝对日期：YYYY[-/.年]M[-/.月]D日? 或 M月D日（先长后短，避免残段再被数字槽位吞掉）
_ABS_DATE_RE = re.compile(
    r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
)
# 文件名（带扩展名，扩展名 1-8 个字母/数字；主体以 ASCII 字母/数字开头
# （可混中文/下划线/连字符），或为纯中文文件名，避免吞并前置动词如"播放demo.mp4"）
_NAME_RE = re.compile(
    r"(?:[A-Za-z0-9][\w\u4e00-\u9fff\-]*|[\u4e00-\u9fff]+)\.[A-Za-z0-9]{1,8}"
)
# 常见操作动词（长词优先）；消费式匹配"动词+文件名"，文件名直接进 <name> 槽位、
# 动词保留在模板文本，避免单字动词（测/试/查等）与中文文件名混淆导致文件名被肢解
_VERB_STRIP_RE = re.compile(
    r"(播放|打开|删除|查看|查找|搜索|复制|移动|生成|创建|发送|下载|上传|转换|翻译|"
    r"统计|整理|压缩|解压|编辑|修改|设置|安装|卸载|启动|停止|运行|读取|写入|导出|导入|"
    r"关闭|保存|清除|筛选|合并|拆分|重命名|打印|扫描|连接|断开|听|看|找|查|读|写|转|"
    r"发|存|删|开|关|放|搜|取|建|改|导|传|压|解|算|记|录|排|整|清|移|复|剪|贴|设|装|"
    r"卸|登|退|签|注|批|审|校|测|编|译|调|试|跑|启|停|挂|断|连|接|切|换|绘|制|截|图)"
    r"(?:[A-Za-z0-9][\w\u4e00-\u9fff\-]*|[\u4e00-\u9fff]+)\.[A-Za-z0-9]{1,8}"
)
# 动词占位符（不属于 \w / 中文 / 数字，不会被任何槽位正则吞并，最后还原）
_VERB_PLACEHOLDER = "\u25ef"
# 占位符序号用中文数字（避免 \d 被数字槽位误吞）
_CN_DIGITS = "零一二三四五六七八九"
# 独立数字（前后不为单词字符；绝对日期已被先替换，剩余数字视为参数）
_NUM_RE = re.compile(r"(?<!\w)\d+(?!\w)")

# 槽位类型 -> 统一占位符基底（相对/绝对日期都归并为 date）
_KIND_TO_BASE = {
    KIND_RELATIVE_DATE: "date",
    KIND_ABSOLUTE_DATE: "date",
    KIND_NUM: "num",
    KIND_NAME: "name",
}


def abstract_query(query: str) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """对用户 query 做槽位抽象。

    替换顺序：相对日期词 -> 绝对日期 -> 文件名（带扩展名）-> 数字。
    同类型多次出现时后续槽位加下标（<date>、<date_2>），位置敏感。

    返回：
        (query_template, slot_values)
        slot_values: {slot_name: {"value": 原值, "kind": 类型, "index": 出现序号}}
    """
    counters: Dict[str, int] = {}
    slot_values: Dict[str, Dict[str, Any]] = {}

    def _add_slot(kind: str, value: str) -> str:
        base = _KIND_TO_BASE.get(kind, kind)
        counters[base] = counters.get(base, 0) + 1
        idx = counters[base]
        name = base if idx == 1 else f"{base}_{idx}"
        slot_values[name] = {"value": value, "kind": kind, "index": idx}
        return f"<{name}>"

    text = query or ""
    # 常见动词 + 文件名：动词保留在模板文本、文件名直接进 <name> 槽位
    def _strip_verb(match: "re.Match[str]") -> str:
        verb = match.group(1)
        fname = match.group(0)[len(verb):]
        return verb + _add_slot(KIND_NAME, fname)
    text = _VERB_STRIP_RE.sub(_strip_verb, text)

    def _rel(match: "re.Match[str]") -> str:
        return _add_slot(KIND_RELATIVE_DATE, match.group(0))

    def _abs(match: "re.Match[str]") -> str:
        return _add_slot(KIND_ABSOLUTE_DATE, match.group(0).strip())

    def _name(match: "re.Match[str]") -> str:
        return _add_slot(KIND_NAME, match.group(0))

    def _num(match: "re.Match[str]") -> str:
        return _add_slot(KIND_NUM, match.group(0))

    text = _RELATIVE_DATE_RE.sub(_rel, text)
    text = _ABS_DATE_RE.sub(_abs, text)
    text = _NAME_RE.sub(_name, text)
    text = _NUM_RE.sub(_num, text)
    return text, slot_values


# ═══════════════════════════════════════════════
#  skill_usage 落库与固化判定
# ═══════════════════════════════════════════════

def log_skill_usage(query: str, tool_seq: List[dict], params: Optional[dict] = None,
                    session_id: str = "") -> dict:
    """记录一次成功执行（skill_usage 落库），并在满足固化条件时自动固化。

    参数：
        query: 用户原始 query
        tool_seq: 工具序列 [{tool, params}, ...]（与 episode 的 tool_sequence 同构）
        params: 可选参数兜底（默认从 tool_seq 提取，当前保留原样存储）
        session_id: 来源会话 ID

    返回：
        {"success": bool, "query_template": str, "fixated": bool,
         "fixation_file": str|""}
    """
    _init_skill_tables()
    if not SKILL_USAGE_ENABLED or not query or not tool_seq:
        return {"success": False, "query_template": "", "fixated": False, "fixation_file": ""}

    query_template, slot_values = abstract_query(query)
    usage_date = datetime.date.today().isoformat()
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            conn.execute(
                "INSERT INTO skill_usage "
                "(session_id, query, query_template, tool_seq, params, usage_date, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id or "", query, query_template,
                 json.dumps(tool_seq, ensure_ascii=False),
                 json.dumps(params or {}, ensure_ascii=False),
                 usage_date, _now()),
            )
    except Exception as e:
        from logger import get_logger
        get_logger().warning(f"[技能固化] skill_usage 落库失败: {e}")
        return {"success": False, "query_template": query_template,
                "fixated": False, "fixation_file": ""}

    fixated, fixation_file = False, ""
    try:
        _fix = _maybe_fixate(query_template, slot_values, tool_seq)
        if _fix:
            fixated = True
            fixation_file = _fix.get("file_path", "")
    except Exception as e:
        from logger import get_logger
        get_logger().warning(f"[技能固化] 固化判定/生成失败: {e}")

    return {"success": True, "query_template": query_template,
            "fixated": fixated, "fixation_file": fixation_file}


def get_usage_by_template(query_template: str, window_days: Optional[int] = None) -> List[dict]:
    """查询某模板在最近 window_days 天内的使用统计（按日期聚合）。

    返回：[{usage_date, count}, ...]，按日期升序。
    """
    _init_skill_tables()
    days = SKILL_WINDOW_DAYS if window_days is None else window_days
    window_start = (datetime.date.today()
                    - datetime.timedelta(days=days)).isoformat()
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            rows = conn.execute(
                "SELECT usage_date, COUNT(*) AS cnt FROM skill_usage "
                "WHERE query_template = ? AND usage_date >= ? "
                "GROUP BY usage_date ORDER BY usage_date ASC",
                (query_template, window_start),
            ).fetchall()
    except Exception as e:
        from logger import get_logger
        get_logger().warning(f"[技能固化] 模板统计失败: {e}")
        return []
    return [{"usage_date": r[0], "count": r[1]} for r in rows]


def distinct_clean_dates(query_template: str, window_days: Optional[int] = None) -> List[str]:
    """统计某模板的"干净日期"列表。

    干净日期 = 当天该模板只出现 1 次（无当天重复追问）。窗口外记录不计。
    """
    return [item["usage_date"] for item in get_usage_by_template(query_template, window_days)
            if item["count"] == 1]


def check_fixation(query_template: str,
                   window_days: Optional[int] = None,
                   min_days: Optional[int] = None) -> Tuple[bool, List[str]]:
    """固化判定：最近 window_days 天内干净日期数 >= min_days 即可固化。

    返回：
        (是否满足, 干净日期列表)
    """
    days = SKILL_WINDOW_DAYS if window_days is None else window_days
    minimum = SKILL_MIN_DAYS if min_days is None else min_days
    clean_dates = distinct_clean_dates(query_template, window_days=days)
    return (len(clean_dates) >= minimum), clean_dates


# ═══════════════════════════════════════════════
#  固化产物生成与登记
# ═══════════════════════════════════════════════

def _build_skill_name(query_template: str, tool_seq: List[dict]) -> str:
    """生成稳定可复用的技能名：<语义基名>_<模板哈希8>。

    基名取模板中最长连续汉字片段（截 10 字）；无汉字时取首个工具名去动词前缀。
    同一模板永远生成同一 name（哈希稳定）。
    """
    no_placeholder = re.sub(r"<[a-z]+(?:_\d+)?>", "", query_template)
    cn_segments = re.findall(r"[\u4e00-\u9fff]{2,}", no_placeholder)
    base = ""
    if cn_segments:
        base = max(cn_segments, key=len)[:10]
    else:
        first_tool = tool_seq[0].get("tool", "") if tool_seq else ""
        base = re.sub(r"^(get_|set_|query_|search_|run_)", "", first_tool)[:10]
    base = base.strip() or "skill"
    digest = hashlib.sha1(query_template.encode("utf-8")).hexdigest()[:8]
    return f"{base}_{digest}"


def _abs_date_to_iso(value: str) -> Optional[str]:
    """把绝对日期文本解析为 YYYY-MM-DD（支持 YYYY年M月D日 / YYYY-M-D / M月D日）。"""
    value = str(value).strip()
    m = re.fullmatch(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if m:
        return f"{datetime.date.today().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _slot_possible_values(meta: Dict[str, Any]) -> List[str]:
    """槽位原值对应的所有可匹配文本（含解析后的实际日期/数字，保证不存死参数）。"""
    values = [str(meta.get("value", "")).strip()]
    kind = meta.get("kind", "")
    if kind == KIND_NUM:
        try:
            int_val = str(int(meta["value"]))
            if int_val not in values:
                values.append(int_val)
        except (TypeError, ValueError):
            pass
    elif kind == KIND_RELATIVE_DATE:
        offset = _RELATIVE_DATE_MAP.get(str(meta.get("value", "")))
        if offset is not None:
            values.append((datetime.date.today()
                           + datetime.timedelta(days=offset)).isoformat())
    elif kind == KIND_ABSOLUTE_DATE:
        iso = _abs_date_to_iso(str(meta.get("value", "")))
        if iso and iso not in values:
            values.append(iso)
    return values


def _num_boundary_pattern(text: str) -> "re.Pattern[str]":
    """数字槽位值的边界正则：前后不允许字母/数字/小数点/连字符。

    防止数字槽位值（如 3）无差别子串替换污染 3.14、C:\\folder3、v2.0
    等包含该数字的相邻文本。与回填侧共享同一套边界约束（对称）。
    """
    return re.compile(
        r"(?<![A-Za-z0-9_.\-])" + re.escape(text) + r"(?![A-Za-z0-9_.\-])"
    )


def _params_to_template(params: dict, slot_values: Dict[str, Dict[str, Any]]) -> dict:
    """把具体参数值替换为 {{slot_name}} 占位符（值等于某槽位原值时替换）。

    递归处理嵌套 dict / list；非槽位值保留字面量（不存死参数仅针对槽位值）。
    日期类槽位同时接受"已解析后的实际日期"（如相对日期解析出的 ISO 日期），
    避免固化时 tool_seq 参数已解析导致模板残留死参数。
    数字类槽位按词边界替换（防 3 误中 3.14/C:\\folder3），文件名等其余槽位
    保持内嵌替换（允许路径/文本中嵌文件名），生成与回填两侧规则对称。
    """
    def _normalize(value: Any) -> str:
        return str(value).strip()

    # slot_texts: {text: [(name, kind), ...]}，同一文本可能映射多个槽位
    slot_texts = {}
    for name, meta in slot_values.items():
        kind = meta.get("kind", "")
        for text in _slot_possible_values(meta):
            slot_texts.setdefault(text, []).append((name, kind))

    # 长文本优先替换，避免短文本（如数字）先行吞掉长文本的公共前缀
    _ordered_texts = sorted((t for t in slot_texts if t), key=len, reverse=True)

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            out = value
            for text in _ordered_texts:
                entries = slot_texts[text]
                name, kind = entries[0]
                if kind == KIND_NUM:
                    out = _num_boundary_pattern(text).sub(
                        "{{" + name + "}}", out)
                elif text in out:
                    out = out.replace(text, "{{" + name + "}}")
            return out
        # int/float 等：与数字槽位值比较后替换
        if isinstance(value, (int, float)) and _normalize(value) in slot_texts:
            return "{{" + slot_texts[_normalize(value)][0][0] + "}}"
        return value

    return _walk(params or {})


def build_skill_yaml(query_template: str,
                     slot_values: Dict[str, Dict[str, Any]],
                     tool_seq: List[dict]) -> dict:
    """生成固化技能 YAML 并写入 skills/<name>.yaml。

    YAML 结构：
        trigger_template: 槽位化触发词（{slot_name} 形式）
        slots: 每个槽位的 kind 与 resolve（param_resolver 语义）
        steps: 固化工具序列（具体值替换为 {{slot_name}} 的参数模板）

    返回：{"name", "file_path", "trigger_template"}
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    name = _build_skill_name(query_template, tool_seq)
    file_path = SKILLS_DIR / f"{name}.yaml"

    # trigger_template：<slot> -> {slot}
    trigger_template = re.sub(
        r"<([a-z]+(?:_\d+)?)>", r"{\1}", query_template)

    slots = {}
    for slot_name, meta in slot_values.items():
        kind = meta["kind"]
        if kind == KIND_RELATIVE_DATE:
            resolve = _RELATIVE_DATE_RESOLVE
        elif kind == KIND_ABSOLUTE_DATE:
            resolve = _ABSOLUTE_DATE_RESOLVE
        elif kind == KIND_NUM:
            resolve = _NUM_RESOLVE
        else:
            resolve = _NAME_RESOLVE
        slots[slot_name] = {"kind": kind, "resolve": resolve}

    steps = []
    for step in tool_seq or []:
        tool = step.get("tool", "")
        params = step.get("params", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        steps.append({
            "tool": tool,
            "args": _params_to_template(params if isinstance(params, dict) else {}, slot_values),
        })

    doc = {
        "trigger_template": trigger_template,
        "slots": slots,
        "steps": steps,
    }
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False)
    return {"name": name, "file_path": str(file_path),
            "trigger_template": trigger_template}


def _register_fixation(query_template: str, built: dict) -> None:
    """把固化技能登记到 skill_fixations 表（template 唯一）。"""
    _init_skill_tables()
    with _get_conn_simple(EXPERIENCE_DB) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO skill_fixations "
            "(template, name, trigger_template, file_path, created_at) "
            "VALUES (?,?,?,?,?)",
            (query_template, built.get("name", ""),
             built.get("trigger_template", ""),
             built.get("file_path", ""), _now()),
        )


def _is_fixated(query_template: str) -> bool:
    """检查模板是否已固化登记。"""
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            row = conn.execute(
                "SELECT id FROM skill_fixations WHERE template = ? LIMIT 1",
                (query_template,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _tool_in_whitelist(tool: str) -> bool:
    """固化/直连前白名单校验：仅低风险工具允许被固化和命中后直连执行。"""
    return tool in LOW_RISK_TOOL_WHITELIST


def _maybe_fixate(query_template: str,
                  slot_values: Dict[str, Dict[str, Any]],
                  tool_seq: List[dict]) -> Optional[dict]:
    """固化自动触发：满足条件且未登记时生成 YAML 并登记。

    安全约束：tool_seq 中任一工具不在低风险白名单（LOW_RISK_TOOL_WHITELIST）
    时拒绝固化，避免危险工具（如 run_shell）被固化为技能后回放执行。
    """
    if not SKILL_FIXATION_ENABLED:
        return None
    if not query_template or _is_fixated(query_template):
        return None
    if any(not _tool_in_whitelist((step or {}).get("tool", ""))
           for step in (tool_seq or [])):
        return None
    ok, _clean_dates = check_fixation(query_template)
    if not ok:
        return None
    built = build_skill_yaml(query_template, slot_values, tool_seq)
    _register_fixation(query_template, built)
    return built


# ═══════════════════════════════════════════════
#  命中检索 / 槽位填值 / 多步直连执行
# ═══════════════════════════════════════════════

def _load_skill_yaml(file_path: str) -> Optional[dict]:
    """加载技能 YAML，失败返回 None。"""
    try:
        path = Path(file_path)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        from logger import get_logger
        get_logger().warning(f"[技能固化] YAML 加载失败 {file_path}: {e}")
        return None


def _template_slot_names(template: str) -> List[str]:
    """提取 query_template 中 <slot_name> 占位符的出现顺序。"""
    return re.findall(r"<([a-z]+(?:_\d+)?)>", template or "")


def _strip_slots(text: str) -> str:
    """去掉模板中的 <slot> 占位符，仅保留非槽位内容（BM25 打分用）。

    槽位 token（date/num/name 等）是模板共享骨架、无信息量，直接参与
    相似度会稀释真正的语义差异（如"<date>股票"与"查<date>天气"因 date
    子串高相似），故先剥离再打分。
    """
    return re.sub(r"<[a-z]+(?:_\d+)?>", " ", text or "").strip()


def _remap_slots_to_template(current_template: str,
                             slot_values: Dict[str, Dict[str, Any]],
                             target_template: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """把当前 query 的槽位值按槽位类型序列对齐到目标模板的槽位名。

    两侧 <slot> 类型序列一致（如 [date, num] vs [date, num]）时按序映射；
    序列不一致说明槽位结构不同，无法安全填槽，返回 None（放弃模糊命中）。
    槽位值按 abstract_query 生成的 index 升序取用，保证映射与出现顺序一致。
    """
    tpl_seq = _template_slot_names(target_template)
    cur_seq = _template_slot_names(current_template)
    if tpl_seq != cur_seq:
        return None
    cur_items = sorted(slot_values.values(), key=lambda m: m.get("index", 0))
    if len(cur_items) != len(tpl_seq):
        return None
    return {tpl_name: cur_items[i] for i, tpl_name in enumerate(tpl_seq)}


def match_fixation(query: str) -> Optional[Tuple[dict, Dict[str, Dict[str, Any]]]]:
    """新 query 归一化后匹配固化技能。

    返回：
        命中: (skill_yaml_dict, slot_values)
        未命中: None

    命中策略：先精确匹配（template 字符串相等，成本最低）；未命中时若启用
    SKILL_BM25_ENABLED，对全部固化模板做 BM25 模糊召回（复用 memory/context_router
    的 BM25，归一化相似度 = max_raw / self_score），top-1 >= SKILL_BM25_THRESHOLD
    即命中；命中后按模板槽位类型序列把当前 query 的槽位值映射对齐到模板槽位名。
    """
    if not SKILL_FIXATION_ENABLED:
        return None
    _init_skill_tables()
    query_template, slot_values = abstract_query(query)
    if not query_template:
        return None
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            row = conn.execute(
                "SELECT file_path FROM skill_fixations WHERE template = ? LIMIT 1",
                (query_template,),
            ).fetchone()
    except Exception:
        return None
    if row:
        skill_yaml = _load_skill_yaml(row[0])
        if skill_yaml and skill_yaml.get("steps"):
            return skill_yaml, slot_values
        return None

    # 精确匹配未命中 → BM25 模糊召回
    if not SKILL_BM25_ENABLED or SKILL_BM25_THRESHOLD <= 0:
        return None
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            rows = conn.execute(
                "SELECT template, trigger_template, file_path FROM skill_fixations"
            ).fetchall()
    except Exception:
        return None
    valid_rows = []
    docs = []
    for r in rows:
        content = _strip_slots(r[0])
        if content:
            docs.append(content)
            valid_rows.append(r)
    if not docs:
        return None
    query_content = _strip_slots(query_template)
    if not query_content:
        return None
    try:
        bm25 = BM25()
        bm25.fit(docs)
        scores = bm25.score_all(query_content)
        self_score = bm25.self_score(query_content)
    except Exception:
        return None
    if self_score <= 0:
        return None
    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    if scores[best_idx] <= 0:
        return None
    norm = min(scores[best_idx] / self_score, 1.0)
    if norm < SKILL_BM25_THRESHOLD:
        return None
    row = valid_rows[best_idx]
    remapped = _remap_slots_to_template(query_template, slot_values, row[0])
    if remapped is None:
        return None
    skill_yaml = _load_skill_yaml(row[2])
    if not skill_yaml or not skill_yaml.get("steps"):
        return None
    return skill_yaml, remapped


def _parse_relative_offset(value: str, resolve: str) -> Optional[int]:
    """从 resolve 规则文本解析相对日期词 -> 偏移。

    resolve 示例："昨天->today-1,今天->today-0,明天->today+1"
    """
    try:
        for rule in resolve.split(","):
            rule = rule.strip()
            if not rule or "->" not in rule:
                continue
            word, target = rule.split("->", 1)
            if word.strip() == value:
                match = re.search(r"today([+-]\d+)", target)
                if match:
                    return int(match.group(1))
    except Exception:
        return None
    return None


def resolve_slots(slots_schema: dict,
                  slot_values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """按槽位 schema（kind + resolve）把槽位值解析为实际参数。

    - relative_date: 相对日期词按 resolve 规则转实际日期（YYYY-MM-DD）
    - absolute_date: 原样使用绝对日期
    - num: int(槽位值)
    - name: 原样使用文件名

    返回：{slot_name: 实际值}
    """
    resolved: Dict[str, Any] = {}
    for slot_name, meta in slot_values.items():
        kind = meta.get("kind", "")
        value = meta.get("value", "")
        schema = (slots_schema or {}).get(slot_name, {})
        if kind == KIND_RELATIVE_DATE:
            offset = _parse_relative_offset(str(value), schema.get("resolve", ""))
            if offset is None:
                # resolve 缺失时按内置映射兜底
                offset = _RELATIVE_DATE_MAP.get(str(value))
            if offset is not None:
                resolved[slot_name] = (
                    datetime.date.today() + datetime.timedelta(days=offset)
                ).isoformat()
            else:
                resolved[slot_name] = str(value)
        elif kind == KIND_ABSOLUTE_DATE:
            iso = _abs_date_to_iso(str(value))
            resolved[slot_name] = iso if iso is not None else str(value)
        elif kind == KIND_NUM:
            try:
                resolved[slot_name] = int(value)
            except (TypeError, ValueError):
                resolved[slot_name] = value
        else:
            resolved[slot_name] = str(value)
    return resolved


def _fill_args_template(args: dict, resolved: Dict[str, Any]) -> dict:
    """把 args 模板中的 {{slot_name}} 替换为实际值（深拷贝，不污染模板）。

    与 _params_to_template 生成侧对称：
      - 整串占位符（"{{name}}"）快路径直接返回解析值；
      - 内嵌占位符（如 "D:\\data\\{{name}}"、"把音量调到{{num}}"）按
        {{slot_name}} 精确文本 replace 回填，占位符名按长度降序避免前缀吞并。
    """
    def _fill(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_fill(v) for v in value]
        if isinstance(value, str):
            match = re.fullmatch(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value)
            if match and match.group(1) in resolved:
                return resolved[match.group(1)]
            if "{{" in value:
                out = value
                for name in sorted(resolved, key=len, reverse=True):
                    out = out.replace("{{" + name + "}}", str(resolved[name]))
                return out
            return value
        return value

    return _fill(dict(args or {}))


def execute_fixed_steps(steps: List[dict], resolved_args: Dict[str, Any],
                        enforce_whitelist: bool = True) -> str:
    """顺序执行固化技能的 steps 工具序列（命中即 L1 直连，不调规划 LLM）。

    安全约束：enforce_whitelist=True（默认）时，每步工具必须位于低风险白名单
    （LOW_RISK_TOOL_WHITELIST）；非白名单工具拒绝执行并返回精简错误，防止
    固化技能回放危险工具（run_shell 等）。白名单内工具经 TOOL_REGISTRY 查找
    调用（与 _exec_l1_tool 一致）；任一步失败即停止，返回精简错误文本，
    保证不抛异常、不进 ReAct。

    返回：各步骤结果文本拼接（JSON 或自然文本）。
    """
    if not steps:
        return "固化技能无可执行步骤"
    try:
        from tools import TOOL_REGISTRY
    except Exception as e:
        return f"固化技能执行失败：工具注册表不可用（{e}）"

    parts = []
    for idx, step in enumerate(steps, start=1):
        tool = (step or {}).get("tool", "")
        if enforce_whitelist and not _tool_in_whitelist(tool):
            parts.append(
                f"步骤{idx}[{tool}] 已拒绝：工具不在低风险白名单，固化快路径不执行")
            break
        args = _fill_args_template((step or {}).get("args", {}), resolved_args)
        entry = TOOL_REGISTRY.get(tool)
        func = entry.get("function") if entry else None
        if not callable(func):
            parts.append(f"步骤{idx}[{tool}] 失败：未找到工具 {tool}")
            break
        try:
            raw = func(**args)
        except TypeError:
            try:
                raw = func(args)
            except Exception as e:
                parts.append(f"步骤{idx}[{tool}] 失败：{e}")
                break
        except Exception as e:
            parts.append(f"步骤{idx}[{tool}] 失败：{e}")
            break
        if isinstance(raw, str):
            parts.append(raw.strip())
        else:
            parts.append(str(raw))
    return "\n\n".join(p for p in parts if p)


# ═══════════════════════════════════════════════
#  只读辅助工具（register_tool 风格，无副作用）
# ═══════════════════════════════════════════════

@register_tool(timeout=30)
def list_fixed_skills() -> str:
    """列出当前已固化的技能（只读，不修改任何状态）。"""
    _init_skill_tables()
    try:
        with _get_conn_simple(EXPERIENCE_DB) as conn:
            rows = conn.execute(
                "SELECT name, trigger_template, file_path, created_at "
                "FROM skill_fixations ORDER BY created_at DESC"
            ).fetchall()
    except Exception as e:
        return f"查询固化技能失败：{e}"
    items = [{
        "name": r[0], "trigger_template": r[1],
        "file_path": r[2], "created_at": r[3],
    } for r in rows]
    return json.dumps({"success": True, "count": len(items),
                       "skills": items}, ensure_ascii=False, indent=2)
