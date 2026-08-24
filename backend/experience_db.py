"""
渐进式工具加载 - 经验数据库
通过 JSON 文件存储任务→工具映射经验，支持渐进式路由。
"""

import json
import os
import re
from datetime import datetime
from typing import Optional


# ============================================================
# 任务签名提取
# ============================================================

TASK_PATTERNS = [
    # (动词正则, 对象正则, 标签)
    (r"(设置|调整|改成|调到|改为|设成|调到|调到|降为|升为|拉到|拉到)", r"(音量|声音|喇叭|耳机|扬声器|播放设备)", "set_volume"),
    (r"(静音|取消静音|关闭声音|消音|开启声音|打开声音)", r"", "mute"),
    (r"(切换|更换|改为|改成)", r"(音频设备|输出设备|播放设备|声音设备|扬声器)", "switch_audio_device"),
    (r"(设置|调整|改成|切换|改为)", r"(分辨率|屏幕分辨率)", "set_resolution"),
    (r"(设置|调整|改成|改为)", r"(亮度|屏幕亮度)", "set_brightness"),
    (r"(设置|调整|改成|改为)", r"(麦克风|话筒|mic)", "set_mic_volume"),
    (r"(修改|设置|更改)", r"(注册表|reg)", "modify_registry"),
    (r"(搜索|查找|搜索一下|查一下)", r"(文件|文档|图片|视频|音乐|照片)", "search_files"),
    (r"(卸载|删除|移除)", r"", "uninstall"),
]


def extract_task_signature(user_message: str, domains: list = None) -> str:
    """从用户消息中提取任务签名，格式：{tag}:{domain}。domains 参数已废弃。"""
    for verb_pat, obj_pat, tag in TASK_PATTERNS:
        if re.search(verb_pat, user_message) and (not obj_pat or re.search(obj_pat, user_message)):
            domain = (domains[0] if domains else "") or "general"
            return f"{tag}:{domain}"
    
    # 兜底：简单关键词匹配
    domain = (domains[0] if domains else "") or "general"
    return f"general:{domain}"


# ============================================================
# 经验数据库
# ============================================================

class ExperienceDB:
    """基于 JSON 文件的任务→工具经验存储"""

    def __init__(self, db_path: str = None):
        """初始化经验数据库；未指定路径时默认位于 %APPDATA%\\smartDesktop\\experience\\experience_db.json"""
        if db_path is None:
            try:
                from config.settings import DATA_DIR
            except ImportError:
                DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'smartDesktop')
            db_dir = os.path.join(DATA_DIR, "experience")
            os.makedirs(db_dir, exist_ok=True)
            db_path = os.path.join(db_dir, "experience_db.json")
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        """数据库文件不存在时创建空结构并预置标记"""
        if not os.path.exists(self.db_path):
            self._write({"records": {}, "pre_seeded": True})

    def _read(self) -> dict:
        """读取 JSON 数据库文件；文件缺失或解析失败时返回空记录"""
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"records": {}}

    def _write(self, data: dict):
        """将数据以 UTF-8 写入 JSON 数据库文件"""
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def lookup(self, task_sig: str) -> Optional[dict]:
        """查询某类任务的经验。返回 None 或经验记录。"""
        data = self._read()
        rec = data["records"].get(task_sig)
        if rec is None:
            return None
        rec["task_signature"] = task_sig
        return rec

    def is_strong(self, record: dict) -> bool:
        """判断是否为强经验（可信度足够高，可跳过基础层直连工具）"""
        return record["success_count"] >= 3 and record["fail_count"] == 0

    def upsert_success(self, task_sig: str, tool_name: str):
        """记录一次成功调用"""
        data = self._read()
        rec = data["records"].get(task_sig)
        if rec is None:
            rec = {
                "recommended_tool": tool_name,
                "recommended_params": {"level": "tool"},  # 直连工具级
                "success_count": 0,
                "fail_count": 0,
                "last_used": "",
                "failed_tools": []
            }
        rec["recommended_tool"] = tool_name
        rec["success_count"] += 1
        rec["last_used"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        # 成功后从 failed_tools 中移除（如果有）
        if tool_name in rec.get("failed_tools", []):
            rec.setdefault("failed_tools", []).remove(tool_name)
        data["records"][task_sig] = rec
        self._write(data)

    def record_failure(self, task_sig: str, tool_name: str, error_info: str = ""):
        """记录一次工具失败"""
        data = self._read()
        rec = data["records"].get(task_sig)
        if rec is None:
            rec = {
                "recommended_tool": "",
                "recommended_params": {"level": "domain"},
                "success_count": 0,
                "fail_count": 0,
                "last_used": "",
                "failed_tools": []
            }
        rec["fail_count"] += 1
        if tool_name and tool_name not in rec.get("failed_tools", []):
            rec.setdefault("failed_tools", []).append(tool_name)
        rec["last_used"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        data["records"][task_sig] = rec
        self._write(data)

    def seed_defaults(self):
        """预置已知的经验映射，部署时即生效"""
        defaults = {
            "set_volume:system_config": "system_audio",
            "mute:system_config": "system_audio",
            "set_mic_volume:system_config": "system_audio",
            "switch_audio_device:system_config": "system_audio",
            "set_resolution:system_config": "display_config",
            "set_brightness:system_config": "display_config",
            "modify_registry:system_config": "modify_registry",
        }
        data = self._read()
        for task_sig, tool_name in defaults.items():
            if task_sig not in data["records"]:
                data["records"][task_sig] = {
                    "recommended_tool": tool_name,
                    "recommended_params": {"level": "tool"},
                    "success_count": 0,
                    "fail_count": 0,
                    "last_used": "",
                    "failed_tools": []
                }
        self._write(data)


# 单例
_experience_db: Optional[ExperienceDB] = None


def get_experience_db() -> ExperienceDB:
    """获取经验数据库全局单例，首次调用时初始化并预置默认经验映射"""
    global _experience_db
    if _experience_db is None:
        _experience_db = ExperienceDB()
        _experience_db.seed_defaults()
    return _experience_db
