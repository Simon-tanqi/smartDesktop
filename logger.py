"""
结构化日志模块 — JSON Lines 格式，按天轮转，保留 30 天。

特性：
- 分级：DEBUG / INFO / WARNING / ERROR
- 每条日志一个 JSON 对象，一行写入
- 按天切分文件：logs/agent_YYYY-MM-DD.jsonl
- 自动清理超过 30 天的旧日志
- 内存缓冲 + 批量刷盘，减少 I/O
- 线程安全
"""

import os
import json
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

# ── 配置 ──
LOG_DIR = Path(__file__).parent / "logs"
RETENTION_DAYS = 30
FLUSH_INTERVAL = 5          # 秒，缓冲刷盘间隔
MAX_BUFFER_SIZE = 50        # 条，缓冲条数达此值立即刷盘

# ── 日志级别 ──
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
MIN_LEVEL = "INFO"


class StructuredLogger:
    """结构化日志单例"""

    def __init__(self):
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._current_date = ""
        self._fh = None
        self._flush_timer: threading.Timer | None = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _open_file(self, date_str: str):
        """打开当天的日志文件"""
        if self._fh:
            self._fh.close()

        log_path = LOG_DIR / f"agent_{date_str}.jsonl"
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)
        self._current_date = date_str

    def _rotate_if_needed(self):
        """检查日期变化，切换文件"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._open_file(today)
            # 清理旧日志
            self._cleanup()

    def _cleanup(self):
        """删除超过保留天数的日志文件及索引"""
        cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
        for f in list(LOG_DIR.glob("agent_*.jsonl")) + list(LOG_DIR.glob("agent_*.idx")):
            try:
                date_part = f.stem.replace("agent_", "")
                file_date = datetime.strptime(date_part, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                pass

    def _flush(self):
        """缓冲刷盘，同时写入 byte-offset 索引以加速后续查询"""
        with self._lock:
            if not self._buffer:
                return
            self._rotate_if_needed()
            if self._fh:
                idx_entries = []
                offset = self._fh.tell()

                for line in self._buffer:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        offset += len(line.encode("utf-8"))
                        continue
                    level = entry.get("level", "")
                    extra = entry.get("extra", {})
                    idx_entries.append(
                        f"{offset}\t{level}\t{extra.get('session_id', '')}\t{extra.get('trace_id', '')}"
                    )
                    offset += len(line.encode("utf-8"))

                self._fh.write("".join(self._buffer))
                self._fh.flush()

                # 追加写入索引文件
                if idx_entries:
                    idx_path = LOG_DIR / f"agent_{self._current_date}.idx"
                    with open(idx_path, "a", encoding="utf-8") as idx_fh:
                        idx_fh.write("\n".join(idx_entries) + "\n")

            self._buffer.clear()

    def _schedule_flush(self):
        """安排定时刷盘"""
        if self._flush_timer:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(FLUSH_INTERVAL, self._flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def log(self, level: str, message: str, **extra):
        """写入一条日志"""
        if LEVELS.get(level, 0) < LEVELS.get(MIN_LEVEL, 0):
            return

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": level,
            "message": message,
        }
        if extra:
            entry["extra"] = extra

        line = json.dumps(entry, ensure_ascii=False) + "\n"

        with self._lock:
            self._buffer.append(line)
            need_flush = len(self._buffer) >= MAX_BUFFER_SIZE

        if need_flush:
            self._flush()
        else:
            self._schedule_flush()

    def debug(self, msg, **extra):
        self.log("DEBUG", msg, **extra)

    def info(self, msg, **extra):
        self.log("INFO", msg, **extra)

    def warning(self, msg, **extra):
        self.log("WARNING", msg, **extra)

    def error(self, msg, **extra):
        self.log("ERROR", msg, **extra)

    # ── LLM 调用日志 ──
    def log_llm_call(self, trace_id: str, session_id: str, turn_index: int,
                     messages: list[dict], response: str,
                     model_name: str = "", provider: str = "",
                     elapsed_ms: int = 0, token_estimate: int = 0):
        """
        记录 LLM 调用：
        - 元信息（摘要）写入 JSON Lines 检索层
        - 完整 messages + response 写入独立文件，按 trace_id 定位
        """
        date_str = datetime.now().strftime("%Y-%m-%d")

        # ── 完整内容另存 ──
        llm_dir = LOG_DIR / "llm_calls" / date_str
        llm_dir.mkdir(parents=True, exist_ok=True)
        full_path = llm_dir / f"{trace_id}_turn{turn_index}.json"

        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        response_chars = len(response)

        with open(full_path, "w", encoding="utf-8") as f:
            # Token优化：完整 messages 写入磁盘较耗 I/O（每轮数 KB）
            # 仅当 LOG_FULL_LLM_MESSAGES=1 时全量保存；默认只保存摘要
            payload = {
                "trace_id": trace_id,
                "session_id": session_id,
                "turn": turn_index,
                "model": f"{provider}/{model_name}",
                "elapsed_ms": elapsed_ms,
                "token_estimate": token_estimate,
                "response": response,
            }
            if os.environ.get("LOG_FULL_LLM_MESSAGES") == "1":
                payload["messages"] = messages
            else:
                payload["messages_count"] = len(messages)
                payload["messages_keys"] = [m.get("role") for m in messages[:20]]
                payload["messages_sample"] = messages[:2] if len(messages) >= 2 else messages
            json.dump(payload, f, ensure_ascii=False, indent=2)

        # ── 元信息写入检索层 ──
        self.info("LLM调用",
                  trace_id=trace_id, session_id=session_id,
                  turn=turn_index, model=f"{provider}/{model_name}",
                  elapsed_ms=elapsed_ms, tokens=token_estimate,
                  prompt_chars=prompt_chars, response_chars=response_chars,
                  full_path=str(full_path))

    def get_llm_call_file(self, trace_id: str, turn: int) -> str | None:
        """根据 trace_id 和 turn 定位完整 LLM 调用文件。扫描最近 7 天的 llm_calls 目录。"""
        for days_ago in range(7):
            date_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            candidate = LOG_DIR / "llm_calls" / date_str / f"{trace_id}_turn{turn}.json"
            if candidate.exists():
                return str(candidate)
        return None

    # ── 查询接口 ──
    def query(self, date_from: str = None, date_to: str = None,
              level: str = None, keyword: str = None,
              session_id: str = None, trace_id: str = None,
              limit: int = 200) -> list[dict]:
        """
        按条件查询日志。有索引（.idx）时利用 byte-offset 精准跳转；无索引时降级为全量扫描。

        date_from / date_to: "YYYY-MM-DD"
        level: "ERROR" / "WARNING" / "INFO" / "DEBUG"
        keyword: 搜索 message 字段和 extra 值（触发全量扫描）
        session_id: 精确匹配 extra.session_id
        trace_id: 精确匹配 extra.trace_id
        limit: 最多返回条数
        """
        if date_from:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        else:
            start = datetime.now() - timedelta(days=7)

        if date_to:
            end = datetime.strptime(date_to, "%Y-%m-%d")
        else:
            end = datetime.now()

        # 关键词搜索无法利用索引，走全量扫描
        use_index = (keyword is None)

        results = []
        current = start
        while current <= end and len(results) < limit:
            date_str = current.strftime("%Y-%m-%d")
            log_path = LOG_DIR / f"agent_{date_str}.jsonl"
            if not log_path.exists():
                current += timedelta(days=1)
                continue

            if use_index:
                idx_results = self._query_with_index(
                    log_path, date_str, level, session_id, trace_id, limit - len(results)
                )
                results.extend(idx_results)
            else:
                idx_results = self._query_scan(
                    log_path, level, keyword, session_id, trace_id, limit - len(results)
                )
                results.extend(idx_results)

            current += timedelta(days=1)

        return results

    def _query_with_index(self, log_path, date_str, level, session_id, trace_id, limit):
        """利用 .idx 索引文件按 byte offset 精准读取匹配行"""
        idx_path = LOG_DIR / f"agent_{date_str}.idx"
        if not idx_path.exists():
            # 无索引，降级为全量扫描
            return self._query_scan(log_path, level, None, session_id, trace_id, limit)

        # 1. 读取索引，收集匹配的 byte offset
        matched_offsets = []
        with open(idx_path, "r", encoding="utf-8") as idx_fh:
            for idx_line in idx_fh:
                idx_line = idx_line.strip()
                if not idx_line:
                    continue
                parts = idx_line.split("\t", 3)
                if len(parts) < 4:
                    continue
                offset_str, idx_level, idx_sid, idx_tid = parts
                if level and idx_level != level:
                    continue
                if session_id and idx_sid != session_id:
                    continue
                if trace_id and idx_tid != trace_id:
                    continue
                matched_offsets.append(int(offset_str))
                if len(matched_offsets) >= limit:
                    break

        if not matched_offsets:
            return []

        # 2. 按 offset 精准读取匹配行
        results = []
        with open(log_path, "r", encoding="utf-8") as f:
            for offset in matched_offsets:
                f.seek(offset)
                line = f.readline()
                if not line:
                    continue
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    @staticmethod
    def _query_scan(log_path, level, keyword, session_id, trace_id, limit):
        """全量扫描（无索引或需要关键词搜索时使用）"""
        results = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if level and entry.get("level") != level:
                    continue
                if keyword:
                    msg = entry.get("message", "")
                    if keyword.lower() not in msg.lower():
                        extra = entry.get("extra", {})
                        found = False
                        for v in extra.values():
                            if keyword.lower() in str(v).lower():
                                found = True
                                break
                        if not found:
                            continue
                if session_id:
                    extra = entry.get("extra", {})
                    if extra.get("session_id") != session_id:
                        continue
                if trace_id:
                    extra = entry.get("extra", {})
                    if extra.get("trace_id") != trace_id:
                        continue

                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def close(self):
        self._flush()
        if self._fh:
            self._fh.close()


# ── 全局单例 ──
_logger: StructuredLogger | None = None


def get_logger() -> StructuredLogger:
    global _logger
    if _logger is None:
        _logger = StructuredLogger()
    return _logger


# ── 便捷函数 ──
def log_debug(msg, **extra):
    get_logger().debug(msg, **extra)


def log_info(msg, **extra):
    get_logger().info(msg, **extra)


def log_warning(msg, **extra):
    get_logger().warning(msg, **extra)


def log_error(msg, **extra):
    get_logger().error(msg, **extra)
