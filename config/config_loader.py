"""
配置热加载工具类

特性：
- 单例模式，模块加载时解析一次
- 通过文件 mtime 自动检测 YAML 变更，下次访问时无缝切换
- 线程安全

用法：
    from config.config_loader import get_safety_config
    cfg = get_safety_config()
    cfg.delete_command_patterns      → list[str]
    cfg.protected_path_patterns      → list[(pattern, desc)]
    cfg.danger_challenge_phrase      → str
    cfg.danger_patterns              → list[(pattern, desc, level)]
    cfg.always_blocked               → list[str]
    cfg.file_skip_dirs               → set[str]
    cfg.registry_allowed_root_keys   → set[str]
    ...
"""
import os
import threading
import yaml
from pathlib import Path
from dataclasses import dataclass, field


# YAML 文件路径
_YAML_PATH = Path(__file__).parent / "safety_rules.yaml"


# ═══════════════════════════════════════════════
#  结构化配置容器
# ═══════════════════════════════════════════════

@dataclass
class SafetyConfig:
    """安全规则配置容器 — 所有访问通过属性，支持热加载透明切换"""
    # safe_exec
    delete_command_patterns: list = field(default_factory=list)
    protected_path_patterns: list = field(default_factory=list)  # list[(pattern, desc)]
    danger_challenge_phrase: str = ""
    danger_patterns: list = field(default_factory=list)          # list[(pattern, desc, level)]
    danger_path_patterns: list = field(default_factory=list)     # list[(pattern, desc)] — L2 挑战路径
    always_blocked: list = field(default_factory=list)

    # file_tools
    file_skip_dirs: set = field(default_factory=set)
    forbidden_patterns: list = field(default_factory=list)
    registry_write_commands: list = field(default_factory=list)
    skip_confirm_programs: set = field(default_factory=set)
    readonly_patterns: set = field(default_factory=set)
    low_risk_registry_patterns: list = field(default_factory=list)
    command_anti_patterns: list = field(default_factory=list)

    # search_tools
    search_skip_dirs: set = field(default_factory=set)
    downloadable_exts: set = field(default_factory=set)

    # registry_tools
    registry_allowed_root_keys: set = field(default_factory=set)
    registry_allowed_path_prefixes: list = field(default_factory=list)
    registry_allowed_operations: set = field(default_factory=set)
    registry_forbidden_values: set = field(default_factory=set)
    registry_forbidden_operations: set = field(default_factory=set)

    # web_download
    risky_extensions: set = field(default_factory=set)

    # ── 预留接口（只读占位，不接入任何确认逻辑） ──
    # confirm_override：未来"原始路径 → 复制到工作区 → 修改副本 → 用户确认 →
    # 覆写原始路径"方案的确认开关。用户设置打开后可跳过人工确认。
    # 当前仅占位可读：恒为 False/关闭，不改变任何现有行为。
    confirm_override: bool = False


# ═══════════════════════════════════════════════
#  解析逻辑
# ═══════════════════════════════════════════════

def _parse_yaml() -> SafetyConfig:
    """从 YAML 文件解析安全配置"""
    with open(_YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    cfg = SafetyConfig()

    # ── safe_exec ──
    se = data.get("safe_exec", {})
    cfg.delete_command_patterns = se.get("delete_command_patterns", [])
    cfg.protected_path_patterns = [
        (item["pattern"], item["desc"])
        for item in se.get("protected_path_patterns", [])
    ]
    cfg.danger_challenge_phrase = se.get("danger_challenge_phrase", "")
    cfg.danger_patterns = [
        (item["pattern"], item["desc"], item["level"])
        for item in se.get("danger_patterns", [])
    ]
    cfg.danger_path_patterns = [
        (item["pattern"], item["desc"])
        for item in se.get("danger_path_patterns", [])
    ]
    cfg.always_blocked = se.get("always_blocked", [])

    # ── file_tools ──
    ft = data.get("file_tools", {})
    cfg.file_skip_dirs = set(ft.get("skip_dirs", []))
    cfg.forbidden_patterns = ft.get("forbidden_patterns", [])
    cfg.registry_write_commands = ft.get("registry_write_commands", [])
    cfg.skip_confirm_programs = set(ft.get("skip_confirm_programs", []))
    # 合并所有 readonly 子列表为一个 set
    rp = ft.get("readonly_patterns", {})
    readonly_set = set()
    for sublist in rp.values():
        if isinstance(sublist, list):
            readonly_set.update(sublist)
    cfg.readonly_patterns = readonly_set
    cfg.low_risk_registry_patterns = ft.get("low_risk_registry_patterns", [])
    cfg.command_anti_patterns = [
        (item["pattern"], item["message"])
        for item in ft.get("command_anti_patterns", [])
    ]

    # ── search_tools ──
    st = data.get("search_tools", {})
    cfg.search_skip_dirs = set(st.get("skip_dirs", []))
    cfg.downloadable_exts = set(st.get("downloadable_exts", []))

    # ── registry_tools ──
    rt = data.get("registry_tools", {})
    cfg.registry_allowed_root_keys = set(rt.get("allowed_root_keys", []))
    cfg.registry_allowed_path_prefixes = rt.get("allowed_path_prefixes", [])
    cfg.registry_allowed_operations = set(rt.get("allowed_operations", []))
    cfg.registry_forbidden_values = set(rt.get("forbidden_values", []))
    cfg.registry_forbidden_operations = set(rt.get("forbidden_operations", []))

    # ── web_download ──
    wd = data.get("web_download", {})
    cfg.risky_extensions = set(wd.get("risky_extensions", []))

    # ── 预留接口（只读占位）：confirm_override 确认开关 ──
    # 未来"复制到工作区→修改→确认→覆写原路径"方案的确认开关；当前仅占位可读，
    # 无论 YAML 是否配置均不接入任何确认逻辑（默认 False/关闭）。
    cfg.confirm_override = bool(ft.get("confirm_override", False))

    return cfg


# ═══════════════════════════════════════════════
#  热加载单例
# ═══════════════════════════════════════════════

class _ConfigManager:
    """线程安全的热加载配置管理器"""

    def __init__(self):
        """初始化线程锁、配置缓存与配置文件时间戳"""
        self._lock = threading.Lock()
        self._config: SafetyConfig | None = None
        self._last_mtime: float = 0.0

    def _reload_if_changed(self):
        """检测 YAML mtime，变更时重新解析"""
        try:
            current_mtime = os.path.getmtime(_YAML_PATH)
        except OSError:
            return  # 文件不存在，保持现有配置

        if current_mtime != self._last_mtime:
            self._config = _parse_yaml()
            self._last_mtime = current_mtime

    def get(self) -> SafetyConfig:
        """获取当前配置（自动热加载）"""
        if self._config is None:
            with self._lock:
                if self._config is None:
                    self._config = _parse_yaml()
                    try:
                        self._last_mtime = os.path.getmtime(_YAML_PATH)
                    except OSError:
                        self._last_mtime = 0.0

        self._reload_if_changed()
        return self._config


_manager = _ConfigManager()


def get_safety_config() -> SafetyConfig:
    """获取安全配置单例"""
    return _manager.get()
