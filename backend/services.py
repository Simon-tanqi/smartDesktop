"""Service 层：把路由层（Flask transport）中的业务逻辑沉淀为可复用服务。

对齐"Controller 只做参数解析与响应、业务逻辑下沉 Service"的分层规范。
本模块当前承载三类持久化/查询业务：
- SettingsService：模型配置（models.json）、激活模型、用户画像的读写与同步。

路由层（backend/chat.py）只负责 HTTP 参数/状态码，真正读盘、写盘、同步记忆的
逻辑统一收敛到这里，便于单测与复用。
"""
import os
import json
from typing import Any, Dict

from config.settings import MODELS_CONFIG_PATH, USER_PROFILE_PATH


class SettingsService:
    """配置与用户画像持久化服务。

    所有方法均幂等、无 HTTP 依赖，可直接在非请求上下文（脚本/测试）调用。
    """

    # ── 模型配置 ──

    @staticmethod
    def load_models_config() -> Dict[str, Any]:
        """读取模型配置文件；文件不存在时返回空结构。"""
        if os.path.exists(MODELS_CONFIG_PATH):
            with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"models": [], "active_model_id": ""}

    @staticmethod
    def save_models_config(config: Dict[str, Any]) -> None:
        """将模型配置写入配置文件（UTF-8 缩进 JSON）。"""
        os.makedirs(os.path.dirname(MODELS_CONFIG_PATH), exist_ok=True)
        with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    @staticmethod
    def set_active_model(model_id: str) -> None:
        """设置当前激活模型 id（读改写，保留其余配置）。"""
        config = SettingsService.load_models_config()
        config["active_model_id"] = model_id
        SettingsService.save_models_config(config)

    # ── 用户画像 ──

    @staticmethod
    def load_user_profile() -> Dict[str, Any]:
        """读取用户画像文件；文件不存在时返回空字典。"""
        if os.path.exists(USER_PROFILE_PATH):
            with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    @staticmethod
    def save_user_profile(profile: Dict[str, Any]) -> None:
        """持久化用户画像，并同步更新用户信息 Markdown 与长期事实记忆。"""
        from memory import append_fact
        from memory.long_term import _rewrite_md, _read_lines
        from memory.config import USER_MD, FACTS_MD

        os.makedirs(os.path.dirname(USER_PROFILE_PATH), exist_ok=True)
        with open(USER_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)

        name = profile.get("name") or profile.get("nickname") or ""
        parts = []
        if name:
            parts.append(f"- 姓名: {name}")
        if profile.get("nickname") and profile.get("name") and profile["nickname"] != profile["name"]:
            parts.append(f"- 称呼偏好: {profile['nickname']}")
        if parts:
            _rewrite_md(USER_MD, "# 用户信息\n\n" + "\n".join(parts) + "\n\n# 偏好\n\n(无)\n")

        for key, label in (("background", "职业背景"), ("hobbies", "兴趣爱好")):
            text = profile.get(key)
            if text:
                existing = _read_lines(FACTS_MD)
                fact = f"- [{label}] {text}"
                if fact not in existing:
                    append_fact(fact)

    @staticmethod
    def is_first_run() -> bool:
        """判断是否为首次运行（用户画像文件不存在即视为首次）。"""
        return not os.path.exists(USER_PROFILE_PATH)
