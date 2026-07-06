"""
结构化日志模块 — JSON Lines 格式，按天轮转，永久保留。

特性：
- 分级：DEBUG / INFO / WARNING / ERROR
- 每条日志一个 JSON 对象，一行写入
- 按天切分文件：logs/agent_YYYY-MM-DD.jsonl
- 日志总大小超 500MB 时启动弹窗提醒（不再自动删除旧日志）
- 内存缓冲 + 批量刷盘，减少 I/O
- 线程安全
"""

import os,re
import json
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

# ── 配置 ──
LOG_DIR = Path(__file__).parent / "logs"
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
        # LLM调用日志文件锁：文件路径 -> 锁对象
        self._file_locks = {}
        self._file_locks_lock = threading.Lock()

    def _open_file(self, date_str: str):
        """打开当天的日志文件"""
        if self._fh:
            self._fh.close()

        log_path = LOG_DIR / f"agent_{date_str}.jsonl"
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)
        self._current_date = date_str

    def _get_file_lock(self, file_path: str):
        """获取文件级锁，确保同一文件的读写操作互斥"""
        with self._file_locks_lock:
            if file_path not in self._file_locks:
                self._file_locks[file_path] = threading.Lock()
            return self._file_locks[file_path]

    def _rotate_if_needed(self):
        """检查日期变化，切换文件"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._open_file(today)

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

                # 原子写入索引文件：先写临时文件，再 os.replace；首行 #count=N 校验行数
                if idx_entries:
                    idx_path = LOG_DIR / f"agent_{self._current_date}.idx"
                    existing = []
                    header_line = ""
                    if idx_path.exists():
                        with open(idx_path, "r", encoding="utf-8") as idx_fh:
                            for line in idx_fh:
                                stripped = line.strip()
                                if stripped.startswith("#count="):
                                    header_line = stripped
                                elif stripped:
                                    existing.append(stripped)
                    all_lines = [f"#count={len(existing) + len(idx_entries)}"] + existing + idx_entries
                    tmp_idx = idx_path.with_suffix(".idx.tmp")
                    with open(tmp_idx, "w", encoding="utf-8") as idx_fh:
                        idx_fh.write("\n".join(all_lines) + "\n")
                    os.replace(tmp_idx, idx_path)

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
    def log_llm_call(self, trace_id: str, session_id: str, turn: int,
                     child_turn: int, user_task: str,
                     response: str,
                     model_name: str = "", provider: str = "",
                     elapsed_ms: int = 0, token_estimate: int = 0,
                     session_name: str = "", reasoning: str = "",
                     system_prompt: str = "", user_input: str = "",
                     messages: list = None,
                     user_task_time: str = ""):
        """
        记录 LLM 调用：一个会话一个文件，每轮追加到 turns 数组。

        文件路径：llm_calls/{session_id}/{session_id}_{session_name}.json
        内容结构：
        {
            "session_id": "...",
            "session_name": "...",
            "turns": [
                {
                    "turn": 0,                          // 对话轮次（全局）
                    "user_task": "帮我把迅雷软件卸载一下", // 用户任务，turn级别只保存一次
                    "system_prompt": "...",              // 系统提示词，turn级别只保存一次
                    "user_task_time": "2026-07-05 08:12:49", // 任务开始时间
                    "ai_reply": "已收到您的确认...",       // 最终AI回复（turn完成时填写）
                    "ai_reply_time": "2026-07-05 08:14:07", // 最终回复时间（turn完成时填写）
                    "child_turns": [                    // child_turn 列表，每个元素是一次LLM调用
                        {
                            "child_turn": 0,
                            "timestamp": "...",
                            "user_input": "...",        // 实际发送给LLM的完整内容
                            "ai_thinking": "...",        // AI思考过程
                            "ai_tool_calls": [...],      // AI调用的工具
                            "tool_result": "",           // 系统执行结果（工具执行后通过update_tool_result填写）
                            "model": "...",
                            "elapsed_ms": ...,
                        },
                        ...
                    ]
                },
                ...
            ]
        }

        设计原则：
        - 一个会话一个文件，不按轮次拆分
        - turn 索引从 0 开始递增，无上限（可达100、1000等），每个新用户消息开启一个新turn
        - child_turn 索引从 0 开始，每个turn内部重置，每次LLM请求+回复为一个child_turn，最多15（0-14）
        - turn级别信息只保存一次：user_task, system_prompt, user_task_time, ai_reply, ai_reply_time
        - child_turn级别保存每次LLM请求的完整信息：user_input, ai_thinking, ai_output, ai_tool_calls, tool_result, model, elapsed_ms
        - system_prompt只在turn级别首次创建时保存，后续child_turn不再重复保存
        - 跨天会话的数据在同一文件中
        """

        # ── 为兼容文件系统，session_name 做安全处理 ──
        safe_name = session_name.strip() if session_name else "untitled"
        # 替换文件系统非法字符
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        # ── session 目录：{session_id}，文件：{session_id}_{session_name}.json ──
        session_dir = LOG_DIR / "llm_calls" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        file_path = session_dir / f"{session_id}_{safe_name}.json"
        
        # 文件锁：确保对同一文件的读写操作互斥
        file_lock = self._get_file_lock(str(file_path))

        # ── 3. 解析 response 判断类型 ──
        ai_tool_calls = []
        ai_reply = ""
        try:
            # 先尝试从 ```action 或 ```json 代码块中提取 JSON
            clean_response = response
            if isinstance(response, str):
                action_match = re.search(r'```(?:action|json)?\s*([\s\S]*?)\s*```', response)
                if action_match:
                    clean_response = action_match.group(1).strip()

            parsed = json.loads(clean_response) if isinstance(clean_response, str) else clean_response
            if isinstance(parsed, dict):
                if "tool_name" in parsed or "tool_call" in parsed:
                    tool_name = parsed.get("tool_name") or parsed.get("tool_call", "")
                    ai_tool_calls = [{
                        "tool": tool_name,
                        "arguments": parsed.get("arguments", {})
                    }]
                elif "final_message" in parsed:
                    ai_reply = parsed["final_message"]
                elif "progress" in parsed:
                    ai_reply = parsed["progress"]
        except (json.JSONDecodeError, TypeError, AttributeError):
            # 非 JSON 响应，视为纯文本回复
            ai_reply = response.strip() if response else ""

        # ── 4. 构建 child_turn 数据（不再保存 full_messages） ──
        child_turn_data = {
            "child_turn": child_turn,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_input": user_input,
            "ai_thinking": reasoning.strip() if reasoning else "",
            "ai_output": response.strip() if response else "",
            "ai_tool_calls": ai_tool_calls,
            "tool_result": "",
            "model": f"{provider}/{model_name}",
            "elapsed_ms": elapsed_ms,
        }

        # ── 5. 读取现有数据 ──
        existing_data = {"session_id": session_id, "session_name": safe_name, "turns": []}
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                    if "turns" not in existing_data:
                        existing_data["turns"] = []
            except (json.JSONDecodeError, IOError):
                existing_data = {"session_id": session_id, "session_name": safe_name, "turns": []}

        # ── 6. 查找或创建 turn 级别数据 ──
        turn_data = None
        for t in existing_data["turns"]:
            if t["turn"] == turn:
                turn_data = t
                break

        if turn_data is None:
            # 新 turn：记录 turn 级别信息（只保存一次）
            turn_data = {
                "turn": turn,
                "user_task": user_task,
                "system_prompt": system_prompt or "",
                "user_task_time": user_task_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ai_reply": "",          # turn 完成时填写
                "ai_reply_time": "",     # turn 完成时填写
                "child_turns": []
            }
            existing_data["turns"].append(turn_data)
        else:
            # 更新 turn 级别的 user_task（确保最新）
            if user_task and turn_data.get("user_task", "") != user_task:
                turn_data["user_task"] = user_task
            # 系统提示词只在 turn 级别首次记录时保存，后续不覆盖
            if system_prompt and not turn_data.get("system_prompt"):
                turn_data["system_prompt"] = system_prompt

        # 追加 child_turn 数据
        turn_data["child_turns"].append(child_turn_data)

        # ── 7. 原子写入文件 ──
        with file_lock:
            # 使用临时文件 + os.replace 确保原子性
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

        # ── 7. 元信息写入检索层 ──
        prompt_chars = len(system_prompt) + len(user_input)
        response_chars = len(response)
        self.info("LLM调用",
                  trace_id=trace_id, session_id=session_id,
                  turn=turn, child_turn=child_turn, user_task=user_task,
                  model=f"{provider}/{model_name}",
                  elapsed_ms=elapsed_ms, tokens=token_estimate,
                  prompt_chars=prompt_chars, response_chars=response_chars,
                  full_path=str(file_path))

    def update_tool_result(self, session_id: str, session_name: str, turn: int,
                           child_turn: int, tool_result: str):
        """工具执行后更新当前 child_turn 的 tool_result 字段。

        在 ReAct 循环中，LLM 调用工具后，工具执行结果通过此方法回填到日志。
        """
        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        session_dir = LOG_DIR / "llm_calls" / session_id
        file_path = session_dir / f"{session_id}_{safe_name}.json"
        if not file_path.exists():
            return
        
        # 获取文件锁以保护读写操作
        file_lock = self._get_file_lock(str(file_path))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        # 定位 turn
        for t in data.get("turns", []):
            if t["turn"] == turn:
                # 定位 child_turn
                for ct in t.get("child_turns", []):
                    if ct["child_turn"] == child_turn:
                        ct["tool_result"] = tool_result[:5000] if tool_result else ""  # 截断防止过大
                        break
                break
        else:
            return

        # 原子写入更新后的数据
        with file_lock:
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

    def log_turn_complete(self, session_id: str, session_name: str, turn: int,
                          ai_reply: str, ai_reply_time: str = ""):
        """turn 完成时记录最终 AI 回复和回复时间。

        在 ReAct 循环结束（final_message 或任务终止）时调用。
        """
        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        session_dir = LOG_DIR / "llm_calls" / session_id
        file_path = session_dir / f"{session_id}_{safe_name}.json"
        if not file_path.exists():
            return
        
        # 获取文件锁以保护读写操作
        file_lock = self._get_file_lock(str(file_path))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        for t in data.get("turns", []):
            if t["turn"] == turn:
                t["ai_reply"] = ai_reply or ""
                t["ai_reply_time"] = ai_reply_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
        else:
            return

        # 原子写入更新后的数据
        with file_lock:
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

    def get_llm_call_file(self, session_id: str, session_name: str = "") -> str | None:
        """根据 session_id 和可选的 session_name 定位 LLM 调用文件。

        文件路径：`llm_calls/{session_id}/{session_id}_{session_name}.json`
        若不传 session_name，扫描目录下匹配 session_id 前缀的文件。
        """
        llm_dir = LOG_DIR / "llm_calls"
        if not llm_dir.exists():
            return None

        session_dir = llm_dir / session_id
        if not session_dir.exists():
            return None

        if session_name:
            # 精确匹配
            safe_name = session_name.strip()
            for ch in r'<>:"/\|?*':
                safe_name = safe_name.replace(ch, '_')
            candidate = session_dir / f"{session_id}_{safe_name}.json"
            if candidate.exists():
                return str(candidate)
        else:
            # 无 session_name 时扫描匹配 session_id 前缀的文件
            for f in session_dir.iterdir():
                if f.name.startswith(f"{session_id}_") and f.suffix == ".json":
                    return str(f)
        return None

    def get_llm_turn(self, session_id: str, turn: int, session_name: str = "") -> dict | None:
        """获取指定 turn 的 LLM 调用数据。

        返回：turn 数据字典，或 None
        """
        file_path = self.get_llm_call_file(session_id, session_name)
        if not file_path:
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                turns = data.get("turns", [])
                for t in turns:
                    if t.get("turn") == turn:
                        return t
        except (json.JSONDecodeError, IOError):
            pass
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
            # 不再默认限制7天：扫描 LOG_DIR 中最早日志文件日期
            log_files = sorted(LOG_DIR.glob("agent_*.jsonl"))
            if log_files:
                earliest = log_files[0].stem.replace("agent_", "")
                start = datetime.strptime(earliest, "%Y-%m-%d")
            else:
                start = datetime.now() - timedelta(days=1)

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

        # 1. 读取索引，跳过 #count=N 头行，收集匹配的 byte offset
        matched_offsets = []
        with open(idx_path, "r", encoding="utf-8") as idx_fh:
            for idx_line in idx_fh:
                idx_line = idx_line.strip()
                if not idx_line or idx_line.startswith("#count="):
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
