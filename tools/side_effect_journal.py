"""
副作用操作日志与补偿机制（P0-2）。

为副作用工具（注册表修改、文件删除、配置变更等）提供：
1. 操作日志：记录每次有副作用工具调用的参数与补偿动作（含原始值快照，供恢复）
2. 补偿/事务：多工具串行执行中后续失败时自动回滚已执行副作用，避免半配置状态
3. undo 入口：提供显式回滚工具 undo_side_effect（见 tools/system_tools.py）

设计原则：
- 快照必须在副作用发生前抓取（set 前读旧值、delete 前读值/键树），补偿动作闭包持有快照。
- 补偿按逆序（LIFO）执行，保证"后发生的先撤销"。
- 进程级单例 + 线程安全；记录带自增序号，便于审计。

持久化（P1-3）：
- 每次 record 同时追加写一行 JSONL 到运行时数据目录
  （沿用项目约定：%APPDATA%\\smartDesktop\\side_effect_journal.jsonl）。
- 启动时加载已有记录：内存补偿队列（可执行补偿回调）与磁盘审计轨迹
  （无补偿回调）合并去重展示（按 sequence 去重、取最近 max_records 条）。
- rollback_all / undo_last 仅回滚内存补偿队列；磁盘记录作为审计轨迹保留
  （进程重启后补偿回调不可恢复，不做虚假补偿）。
- 磁盘写入失败时降级为纯内存模式（不阻断主流程）。
"""
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional


class SideEffectRecord:
    """单条副作用操作记录。"""

    __slots__ = ("tool_name", "arguments", "description", "compensate", "created_at", "sequence")

    def __init__(self, tool_name: str, arguments: Dict[str, Any], description: str,
                 compensate: Callable[[], Optional[str]], sequence: int):
        """初始化副作用记录：工具名、参数、描述、补偿回调与序号。"""
        self.tool_name = tool_name
        self.arguments = dict(arguments)
        self.description = description
        self.compensate = compensate
        self.created_at = time.time()
        self.sequence = sequence

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可审计字典（参数截断展示，避免超大值撑爆上下文）。"""
        def _shorten(v: Any) -> Any:
            """将参数值截断为可读短文本，避免日志过长。"""
            if isinstance(v, (int, float, bool)) or v is None:
                return v
            s = str(v)
            return s if len(s) <= 200 else s[:200] + "..."
        return {
            "tool_name": self.tool_name,
            "arguments": {k: _shorten(v) for k, v in self.arguments.items()},
            "description": self.description,
            "created_at": self.created_at,
            "sequence": self.sequence,
        }


class SideEffectJournal:
    """副作用操作日志（进程级单例，线程安全）。

    记录顺序 = 执行顺序；补偿按逆序执行（LIFO），保证"后发生的先撤销"。
    内存 _records 保存可补偿记录（含补偿回调）；磁盘 JSONL 保存审计轨迹
    （仅元数据，无回调）；list_records / count 对两者合并去重后返回。
    """

    def __init__(self, max_records: int = 200, journal_path: Optional[str] = None):
        """初始化副作用日志：记录列表、线程锁、序号计数器与持久化路径。

        Args:
            max_records: 内存/合并展示的最大记录数（超出丢弃最旧）
            journal_path: JSONL 持久化路径；None 时使用项目运行时数据目录
                          （%APPDATA%\\smartDesktop\\side_effect_journal.jsonl）
        """
        self._records: List[SideEffectRecord] = []
        self._lock = threading.RLock()
        self._sequence = 0
        self._max_records = max_records
        if journal_path is None:
            try:
                from config.settings import DATA_DIR
                journal_path = os.path.join(DATA_DIR, "side_effect_journal.jsonl")
            except Exception:
                journal_path = os.path.join(os.path.expanduser("~"), ".smartDesktop_side_effect_journal.jsonl")
        self._journal_path = journal_path
        self._persisted_records: List[Dict[str, Any]] = []
        self._load_persisted()

    def record(self, tool_name: str, arguments: Dict[str, Any],
               description: str, compensate: Callable[[], Optional[str]]) -> int:
        """记录一次有副作用的操作。

        Args:
            tool_name: 工具名（如 modify_registry）
            arguments: 工具调用参数（用于审计）
            description: 人类可读的操作描述
            compensate: 补偿函数（恢复原值/删除新增值等），返回可读结果或 None

        Returns:
            记录序号
        """
        with self._lock:
            self._sequence += 1
            rec = SideEffectRecord(tool_name, arguments, description, compensate, self._sequence)
            self._records.append(rec)
            if len(self._records) > self._max_records:
                # 只丢弃最旧记录（超出容量后旧补偿已无法可靠恢复，丢弃避免误导）
                self._records = self._records[-self._max_records:]
            # P1-3：同步追加写审计轨迹（失败降级为纯内存，不阻断主流程）
            self._append_persisted(rec)
            return rec.sequence

    # ── P1-3：JSONL 持久化 ──

    def _load_persisted(self) -> None:
        """启动时加载磁盘审计轨迹并推进序号，保证跨进程序号单调。"""
        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    seq = item.get("sequence")
                    if isinstance(seq, int) and seq > self._sequence:
                        self._sequence = seq
                    self._persisted_records.append(item)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _append_persisted(self, rec: SideEffectRecord) -> None:
        """将记录追加写入 JSONL（含目录自动创建）；失败时静默降级。"""
        try:
            os.makedirs(os.path.dirname(self._journal_path), exist_ok=True)
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _merged_records(self) -> List[Dict[str, Any]]:
        """内存与磁盘合并去重（按 sequence），取最近 max_records 条。"""
        merged: Dict[int, Dict[str, Any]] = {}
        for rec in self._records:
            d = rec.to_dict()
            merged[d["sequence"]] = d
        for item in self._persisted_records:
            seq = item.get("sequence")
            if isinstance(seq, int):
                merged.setdefault(seq, item)
        ordered = [merged[k] for k in sorted(merged)]
        return ordered[-self._max_records:]

    def rollback_all(self) -> List[Dict[str, Any]]:
        """逆序执行全部已记录补偿动作（后续工具失败时自动调用）。

        Returns:
            每条补偿的执行结果列表
        """
        with self._lock:
            to_rollback = list(reversed(self._records))
            self._records = []
        results = []
        for rec in to_rollback:
            results.append(self._execute_compensate(rec))
        return results

    def undo_last(self) -> Optional[Dict[str, Any]]:
        """显式撤销最近一次副作用操作（undo_side_effect 工具入口）。

        Returns:
            补偿执行结果；无记录时返回 None
        """
        with self._lock:
            if not self._records:
                # 内存补偿队列为空（可能已回滚或仅剩磁盘审计轨迹）时无可撤销记录
                return None
            rec = self._records.pop()
        return self._execute_compensate(rec)

    def _execute_compensate(self, rec: SideEffectRecord) -> Dict[str, Any]:
        """执行单条记录的补偿回调，返回执行结果。"""
        try:
            detail = rec.compensate()
            return {
                "sequence": rec.sequence,
                "tool_name": rec.tool_name,
                "description": rec.description,
                "status": "compensated",
                "detail": detail,
            }
        except Exception as e:
            return {
                "sequence": rec.sequence,
                "tool_name": rec.tool_name,
                "description": rec.description,
                "status": "compensate_failed",
                "error": str(e)[:300],
            }

    def list_records(self) -> List[Dict[str, Any]]:
        """列出当前全部操作日志（只读，不执行补偿）。

        返回内存补偿队列与磁盘审计轨迹合并去重后的记录（按序号升序）。
        """
        with self._lock:
            return self._merged_records()

    def count(self) -> int:
        """返回当前已记录的副作用数量（内存+磁盘合并去重后）。"""
        with self._lock:
            return len(self._merged_records())

    def clear(self) -> int:
        """清空记录（不执行补偿）。返回被清空数量。磁盘审计文件一并清空。"""
        with self._lock:
            n = len(self._merged_records())
            self._records = []
            self._persisted_records = []
            try:
                with open(self._journal_path, "w", encoding="utf-8") as f:
                    f.write("")
            except OSError:
                pass
            return n


# 进程级单例：所有副作用工具共用，保证跨工具串行执行时能统一回滚
side_effect_journal = SideEffectJournal()
