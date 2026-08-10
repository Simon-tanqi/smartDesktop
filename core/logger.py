"""
上下文构建器专用日志模块

特性：
- RotatingFileHandler：按大小轮转（10MB / 5 个备份）
- 自动创建日志目录
- 统一格式：[时间] [级别] [模块] 消息内容
"""
import os
import logging
import logging.handlers
from pathlib import Path


def get_context_logger() -> logging.Logger:
    """返回配置好的上下文构建器 Logger 实例。

    日志文件：logs/context_builder.log（相对于 DATA_DIR）
    格式：[HH:MM:SS] [LEVEL] [module] message
    级别：INFO 正常流程 / WARNING 降级 / ERROR 异常
    """
    logger = logging.getLogger("smartDesktop.context_builder")

    # 单例：已配置过则直接返回
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 确定日志目录
    try:
        from memory.config import DATA_DIR
        log_dir = Path(DATA_DIR) / "logs"
    except ImportError:
        log_dir = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "smartDesktop" / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "context_builder.log"

    # RotatingFileHandler：最大 10MB，保留 5 个备份
    handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(module)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False

    return logger


# ── 项目级日志工厂（写 %APPDATA%\smartDesktop\logs\） ──

def _get_project_log_dir() -> Path:
    """确定项目级 logs/ 目录路径（统一写入数据目录，与项目源码分离）。"""
    try:
        from memory.config import DATA_DIR
        return Path(DATA_DIR) / "logs"
    except ImportError:
        return Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "smartDesktop" / "logs"


def _make_project_logger(name: str, filename: str) -> logging.Logger:
    """创建项目级 RotatingFileHandler logger。"""
    logger = logging.getLogger(f"smartDesktop.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    _get_project_log_dir().mkdir(parents=True, exist_ok=True)
    log_file = _get_project_log_dir() / filename

    handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(module)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_llm_request_logger() -> logging.Logger:
    """LLM 请求日志：logs/llm_requests.log，10MB/5备份。"""
    return _make_project_logger("llm_requests", "llm_requests.log")


def get_validation_logger() -> logging.Logger:
    """四项任务验证日志：logs/four_tasks_validation.log。"""
    return _make_project_logger("validation", "four_tasks_validation.log")


def get_tool_execution_logger() -> logging.Logger:
    """工具执行日志：logs/tool_execution.log。"""
    return _make_project_logger("tool_exec", "tool_execution.log")


def get_refactor_logger() -> logging.Logger:
    """返回重构专用 Logger 实例。

    日志文件：logs/refactor.log（相对于 DATA_DIR）
    格式：[YYYY-MM-DD HH:MM:SS] [LEVEL] [阶段] 消息内容
    用途：记录 ReActEngine 重构的所有步骤、测试结果、异常
    """
    logger = logging.getLogger("smartDesktop.refactor")

    # 单例：已配置过则直接返回
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 确定日志目录
    try:
        from memory.config import DATA_DIR
        log_dir = Path(DATA_DIR) / "logs"
    except ImportError:
        log_dir = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "smartDesktop" / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "refactor.log"

    # RotatingFileHandler：最大 10MB，保留 5 个备份
    handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False

    return logger
