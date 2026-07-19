"""
结构化日志模块 — JSON Lines 格式，按天轮转，永久保留。

特性：
- 分级：DEBUG / INFO / WARNING / ERROR
- 每条日志一个 JSON 对象，一行写入
- 按天切分文件：logs/agent_YYYY-MM-DD.jsonl
- 日志总大小超 500MB 时启动弹窗提醒（不再自动删除旧日志）
- 内存缓冲 + 异步批量刷盘，减少 I/O
- 线程安全
- 全局异常钩子：捕获未处理异常写入 crash.log
- 控制台日志：开发环境实时输出
"""

import os
import re
import sys
import json
import time
import traceback
import threading
from datetime import datetime, timedelta
from pathlib import Path

try:
    from config.settings import DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')

# ── 配置 ──
LOG_DIR = Path(DATA_DIR) / "logs"
CRASH_LOG_PATH = LOG_DIR / "crash.log"
FLUSH_INTERVAL = 5          # 秒，缓冲刷盘间隔
MAX_BUFFER_SIZE = 50        # 条，缓冲条数达此值立即刷盘
MAX_LOG_SIZE_MB = 500       # MB，日志总大小超此值时提醒
ENABLE_CONSOLE_LOG = os.environ.get('MYAGENT_DEBUG', '').lower() == 'true'

# ── 日志级别 ──
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
MIN_LEVEL = "DEBUG" if ENABLE_CONSOLE_LOG else "INFO"


class StructuredLogger:
    """结构化日志单例"""

    def __init__(self):
        self._buffer: list[str] = []
        self._buffer_lock = threading.Lock()
        self._current_date = ""
        self._fh = None
        self._flush_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._writer_thread = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._file_locks = {}
        self._file_locks_lock = threading.Lock()
        
        self._llm_buffer: list[tuple] = []
        self._llm_buffer_lock = threading.Lock()
        self._llm_flush_event = threading.Event()
        self._llm_shutdown_event = threading.Event()
        self._llm_writer_thread = None
        self._llm_flush_interval = 2
        
        self._size_warned = False
        self._last_size_check = 0
        
        self._start_writer_thread()
        self._start_llm_writer_thread()
        
        self._register_global_exception_hook()

    def _start_writer_thread(self):
        """启动后台写入线程"""
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _start_llm_writer_thread(self):
        """启动LLM日志后台写入线程"""
        self._llm_writer_thread = threading.Thread(target=self._llm_writer_loop, daemon=True)
        self._llm_writer_thread.start()

    def _llm_writer_loop(self):
        """LLM日志后台写入线程主循环"""
        while not self._llm_shutdown_event.is_set():
            self._llm_flush_event.wait(timeout=self._llm_flush_interval)
            self._llm_flush_event.clear()
            self._llm_flush()

    def _llm_flush(self):
        """LLM日志缓冲刷盘"""
        buffer = []
        with self._llm_buffer_lock:
            if not self._llm_buffer:
                return
            buffer = self._llm_buffer[:]
            self._llm_buffer.clear()

        for op_type, args in buffer:
            try:
                if op_type == "log_llm_call":
                    self._do_log_llm_call(*args)
                elif op_type == "update_tool_result":
                    self._do_update_tool_result(*args)
                elif op_type == "log_turn_complete":
                    self._do_log_turn_complete(*args)
            except Exception as e:
                print(f"[WARNING] LLM日志写入失败: {e}")

    def _check_log_size(self):
        """检查日志目录大小，超过阈值时提醒"""
        now = time.time()
        if now - self._last_size_check < 300:
            return
        self._last_size_check = now

        if self._size_warned:
            return

        total_size = 0
        try:
            for f in LOG_DIR.rglob("*"):
                if f.is_file():
                    total_size += f.stat().st_size
        except Exception:
            return

        total_mb = total_size / (1024 * 1024)
        if total_mb > MAX_LOG_SIZE_MB:
            self._size_warned = True
            self.warning(f"日志目录总大小已超过 {MAX_LOG_SIZE_MB}MB，当前大小 {total_mb:.1f}MB")

    def _writer_loop(self):
        """后台写入线程主循环"""
        while not self._shutdown_event.is_set():
            self._flush_event.wait(timeout=FLUSH_INTERVAL)
            self._flush_event.clear()
            self._flush()

    def _open_file(self, date_str: str):
        """打开当天的日志文件（二进制模式）"""
        if self._fh:
            self._fh.close()

        log_path = LOG_DIR / f"agent_{date_str}.jsonl"
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = open(log_path, "ab")
            self._current_date = date_str
        except OSError as e:
            print(f"[WARNING] 无法打开日志文件 {log_path}: {e}")
            self._fh = None

    def _get_file_lock(self, file_path: str):
        """获取文件级锁，确保同一文件的读写操作互斥"""
        with self._file_locks_lock:
            self._cleanup_file_locks()
            if file_path not in self._file_locks:
                self._file_locks[file_path] = threading.Lock()
            return self._file_locks[file_path]

    def _cleanup_file_locks(self):
        """清理不存在文件对应的锁"""
        to_remove = []
        for path in self._file_locks:
            if not os.path.exists(path):
                to_remove.append(path)
        for path in to_remove:
            del self._file_locks[path]

    def _rotate_if_needed(self):
        """检查日期变化，切换文件"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._open_file(today)

    def _flush(self):
        """缓冲刷盘，同时写入 byte-offset 索引以加速后续查询"""
        buffer = []
        with self._buffer_lock:
            if not self._buffer:
                return
            buffer = self._buffer[:]
            self._buffer.clear()

        if not buffer:
            return

        self._rotate_if_needed()
        if self._fh:
            idx_entries = []
            data_bytes = []
            offset = self._fh.tell()

            for line in buffer:
                line_bytes = line.encode("utf-8")
                data_bytes.append(line_bytes)

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    offset += len(line_bytes)
                    continue
                level = entry.get("level", "")
                extra = entry.get("extra", {})
                idx_entries.append(
                    f"{offset}\t{level}\t{extra.get('session_id', '')}\t{extra.get('trace_id', '')}"
                )
                offset += len(line_bytes)

            if idx_entries:
                idx_path = LOG_DIR / f"agent_{self._current_date}.idx"
                need_header = not idx_path.exists()
                with open(idx_path, "a", encoding="utf-8") as idx_fh:
                    if need_header:
                        idx_fh.write("#count=0\n")
                    for entry in idx_entries:
                        idx_fh.write(entry + "\n")
                self._update_idx_count(idx_path, len(idx_entries))

            self._fh.write(b"".join(data_bytes))
            self._fh.flush()
            
            self._check_log_size()

    def _update_idx_count(self, idx_path, added_count):
        """更新索引计数（使用单独文件存储）"""
        count_path = idx_path.parent / f"{idx_path.stem}.cnt"
        try:
            current_count = 0
            if count_path.exists():
                with open(count_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content.isdigit():
                        current_count = int(content)
            current_count += added_count
            with open(count_path, "w", encoding="utf-8") as f:
                f.write(str(current_count))
        except (IOError, ValueError):
            pass

    def _trigger_flush(self):
        """触发刷盘"""
        self._flush_event.set()

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

        with self._buffer_lock:
            self._buffer.append(line)
            need_flush = len(self._buffer) >= MAX_BUFFER_SIZE

        if need_flush:
            self._trigger_flush()

        self._log_to_console(level, message, **extra)

    def flush(self):
        """立即刷盘，确保日志写入磁盘"""
        self._flush_event.set()
        self._flush()
        self._llm_flush_event.set()
        self._llm_flush()

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
        with self._llm_buffer_lock:
            self._llm_buffer.append(("log_llm_call", (trace_id, session_id, turn, child_turn, user_task,
                                                      response, model_name, provider, elapsed_ms, token_estimate,
                                                      session_name, reasoning, system_prompt, user_input,
                                                      messages, user_task_time)))
        self._llm_flush_event.set()

    def _do_log_llm_call(self, trace_id: str, session_id: str, turn: int,
                     child_turn: int, user_task: str,
                     response: str,
                     model_name: str = "", provider: str = "",
                     elapsed_ms: int = 0, token_estimate: int = 0,
                     session_name: str = "", reasoning: str = "",
                     system_prompt: str = "", user_input: str = "",
                     messages: list = None,
                     user_task_time: str = ""):

        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        llm_root_dir = LOG_DIR / "llm_calls"
        try:
            llm_root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.warning(f"无法创建LLM日志目录 {llm_root_dir}: {e}")
            return

        file_path = llm_root_dir / f"{session_id}_{safe_name}.json"
        file_lock = self._get_file_lock(str(file_path))

        ai_tool_calls = []
        ai_reply = ""
        try:
            clean_response = response
            if isinstance(response, str):
                action_match = re.search(r'```(?:action|json)\s*([\s\S]*?)\s*```', response)
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
            ai_reply = response.strip() if response else ""

        child_turn_data = {
            "child_turn": child_turn,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_input": user_input,
            "ai_thinking": reasoning.strip() if reasoning else "",
            "ai_output": response.strip() if response else "",
            "tool_calls": ai_tool_calls,
            "tool_result": "",
            "models": f"{provider}/{model_name}",
            "elapsed_ms": elapsed_ms,
        }

        existing_data = {"session_id": session_id, "session_name": safe_name, "turns": []}
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                    if "turns" not in existing_data:
                        existing_data["turns"] = []
            except (json.JSONDecodeError, IOError):
                existing_data = {"session_id": session_id, "session_name": safe_name, "turns": []}

        turn_data = None
        for t in existing_data["turns"]:
            if t["turn"] == turn:
                turn_data = t
                break

        if turn_data is None:
            turn_data = {
                "turn": turn,
                "user_task": user_task,
                "system_prompt": system_prompt or "",
                "user_task_time": user_task_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ai_reply": "",
                "ai_reply_time": "",
                "child_turns": []
            }
            existing_data["turns"].append(turn_data)
        else:
            if user_task and turn_data.get("user_task", "") != user_task:
                turn_data["user_task"] = user_task
            if system_prompt and not turn_data.get("system_prompt"):
                turn_data["system_prompt"] = system_prompt

        turn_data["child_turns"].append(child_turn_data)

        with file_lock:
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

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
        with self._llm_buffer_lock:
            self._llm_buffer.append(("update_tool_result", (session_id, session_name, turn, child_turn, tool_result)))
        self._llm_flush_event.set()

    def _do_update_tool_result(self, session_id: str, session_name: str, turn: int,
                           child_turn: int, tool_result: str):
        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        llm_root_dir = LOG_DIR / "llm_calls"
        file_path = llm_root_dir / f"{session_id}_{safe_name}.json"
        if not file_path.exists():
            self.debug(f"update_tool_result: 文件不存在 {file_path}")
            return

        file_lock = self._get_file_lock(str(file_path))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.debug(f"update_tool_result: 文件解析失败 {file_path}: {e}")
            return

        for t in data.get("turns", []):
            if t["turn"] == turn:
                for ct in t.get("child_turns", []):
                    if ct["child_turn"] == child_turn:
                        ct["tool_result"] = tool_result[:5000] if tool_result else ""
                        break
                else:
                    self.debug(f"update_tool_result: 未找到子轮次 {child_turn}")
                break
        else:
            self.debug(f"update_tool_result: 未找到轮次 {turn}")
            return

        with file_lock:
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

    def log_turn_complete(self, session_id: str, session_name: str, turn: int,
                          ai_reply: str, ai_reply_time: str = ""):
        with self._llm_buffer_lock:
            self._llm_buffer.append(("log_turn_complete", (session_id, session_name, turn, ai_reply, ai_reply_time)))
        self._llm_flush_event.set()

    def _do_log_turn_complete(self, session_id: str, session_name: str, turn: int,
                          ai_reply: str, ai_reply_time: str = ""):
        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        llm_root_dir = LOG_DIR / "llm_calls"
        file_path = llm_root_dir / f"{session_id}_{safe_name}.json"
        if not file_path.exists():
            self.debug(f"log_turn_complete: 文件不存在 {file_path}")
            return

        file_lock = self._get_file_lock(str(file_path))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.debug(f"log_turn_complete: 文件解析失败 {file_path}: {e}")
            return

        for t in data.get("turns", []):
            if t["turn"] == turn:
                t["ai_reply"] = ai_reply or ""
                t["ai_reply_time"] = ai_reply_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
        else:
            self.debug(f"log_turn_complete: 未找到轮次 {turn}")
            return

        with file_lock:
            import os as _os
            temp_path = str(file_path) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(temp_path, file_path)

    def get_llm_call_file(self, session_id: str, session_name: str = "") -> str | None:
        llm_dir = LOG_DIR / "llm_calls"
        if not llm_dir.exists():
            return None

        if session_name:
            safe_name = session_name.strip()
            for ch in r'<>:"/\|?*':
                safe_name = safe_name.replace(ch, '_')
            candidate = llm_dir / f"{session_id}_{safe_name}.json"
            if candidate.exists():
                return str(candidate)
        else:
            for f in llm_dir.iterdir():
                if f.is_file() and f.name.startswith(f"{session_id}_") and f.suffix == ".json":
                    return str(f)
        return None

    def get_llm_turn(self, session_id: str, turn: int, session_name: str = "") -> dict | None:
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
        if date_from:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        else:
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
            return self._query_scan(log_path, level, None, session_id, trace_id, limit)

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
                if len(matched_offsets) >= limit * 2:
                    break

        if not matched_offsets:
            return []

        results = []
        with open(log_path, "rb") as f:
            for offset in matched_offsets:
                f.seek(offset)
                line_bytes = f.readline()
                if not line_bytes:
                    continue
                try:
                    line = line_bytes.decode("utf-8")
                    entry = json.loads(line.strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break

            if len(results) < limit:
                try:
                    f.seek(0)
                    seen_offsets = set(matched_offsets)
                    for line_bytes in f:
                        try:
                            line = line_bytes.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            continue
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if level and entry.get("level") != level:
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
                except Exception:
                    pass

        return results

    @staticmethod
    def _query_scan(log_path, level, keyword, session_id, trace_id, limit):
        """全量扫描（无索引或需要关键词搜索时使用）"""
        results = []
        with open(log_path, "rb") as f:
            for line_bytes in f:
                try:
                    line = line_bytes.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
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

    def _register_global_exception_hook(self):
        """注册全局异常钩子，捕获所有未处理异常"""
        original_hook = sys.excepthook

        def global_exception_hook(exc_type, exc_value, exc_tb):
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*80}\n")
                    f.write(f"[CRASH] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Exception Type: {exc_type.__name__}\n")
                    f.write(f"Exception Value: {exc_value}\n")
                    f.write("Traceback:\n")
                    traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                    f.write(f"\n{'='*80}\n")

                print(f"\n[CRITICAL] 未处理异常已写入 crash.log: {exc_type.__name__}: {exc_value}")

                self.error(f"未处理异常", exc_type=exc_type.__name__, exc_value=str(exc_value))
            except Exception as e:
                print(f"[ERROR] 写入 crash.log 失败: {e}")

            original_hook(exc_type, exc_value, exc_tb)

        sys.excepthook = global_exception_hook

    def _log_to_console(self, level: str, message: str, **extra):
        """输出到控制台（开发环境）"""
        if not ENABLE_CONSOLE_LOG:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra_str = ""
        if extra:
            extra_str = " " + json.dumps(extra, ensure_ascii=False, default=str)
        print(f"[{level}] {timestamp} {message}{extra_str}")

    def close(self):
        self._shutdown_event.set()
        self._flush_event.set()
        self._llm_shutdown_event.set()
        self._llm_flush_event.set()
        if self._writer_thread:
            self._writer_thread.join(timeout=5)
        if self._llm_writer_thread:
            self._llm_writer_thread.join(timeout=5)
        if self._fh:
            self._fh.close()


# ── 全局单例 ──
_logger: StructuredLogger | None = None
_logger_lock = threading.Lock()


def get_logger() -> StructuredLogger:
    global _logger
    if _logger is None:
        with _logger_lock:
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