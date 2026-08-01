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

层级设计：
- Session：会话元信息（不含系统提示词）
- Turn：系统提示词（每轮可能不同）、用户任务、用户时间、AI回复、AI时间、路由层级、reasoning_trace
- SubTurn（ChildTurn）：工具调用详情 + 近3轮结构化记忆快照（context_memory）
  ※ SubTurn 不记录 user_input / ai_reply / timestamp 等轮次级信息
"""
# NOTE: 当前为单进程部署，RotatingFileHandler 安全。
# 若未来转为多进程，需改用 concurrent_log_handler.ConcurrentRotatingFileHandler
# 或按 PID 分文件（如 f"memory_{os.getpid()}.log"）。

import os
import re
import sys
import json
import time
import uuid
import traceback
import threading
import tempfile
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
_FILE_LEVEL = os.environ.get("MEMORY_LOG_FILE_LEVEL", "DEBUG")
_CONSOLE_LEVEL = os.environ.get("MEMORY_LOG_CONSOLE_LEVEL", "INFO")
MIN_LEVEL = _FILE_LEVEL

# ── 记忆窗口 ──
MEMORY_WINDOW = 3  # SubTurn 拼接最近 N 轮结构化记忆


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
        global LOG_DIR
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            LOG_DIR = Path(tempfile.gettempdir())
        self._file_locks = {}
        self._file_locks_lock = threading.RLock()
        self._llm_file_write_lock = threading.RLock()

        self._llm_buffer: list[tuple] = []
        self._llm_buffer_lock = threading.Lock()
        self._llm_flush_event = threading.Event()
        self._MAX_LLM_BUFFER = 500
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
            try:
                self._llm_flush_event.wait(timeout=self._llm_flush_interval)
                self._llm_flush_event.clear()
                self._llm_flush()
            except Exception as e:
                print(f"[ERROR] LLM日志线程异常（已恢复）: {e}")
                import time as _time
                _time.sleep(1)

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
                # 诊断：将原本只 print 到 stdout 的错误写入主日志，便于定位 _do_log_llm_call 失败原因
                import traceback as _tb
                self.error(f"LLM日志写入失败: {e}", op_type=op_type, traceback=_tb.format_exc())

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
                     user_task_time: str = "",
                     route_level: str = "",
                     context_memory: list = None):
        with self._llm_buffer_lock:
            self._llm_buffer.append(("log_llm_call", (
                trace_id, session_id, turn, child_turn, user_task,
                response, model_name, provider, elapsed_ms, token_estimate,
                session_name, reasoning, system_prompt, user_input,
                messages, user_task_time, route_level, context_memory
            )))
            buffer_len = len(self._llm_buffer)
        self._llm_flush_event.set()
        # 立即刷盘（每次 LLM 调用都即时写，不等待后台线程）
        # 以前的异步刷盘在高并发下会导致 log_turn_complete 找不到文件
        self._llm_flush()

    @staticmethod
    def _merge_llm_record(data, turn, child_turn, record):
        """增量 merge 子轮次记录到 data 的 turns 中，不覆盖已有 children。"""
        turns = data.setdefault("turns", {})
        turn_key = str(turn)
        turn_block = turns.get(turn_key)
        if turn_block is None:
            turn_block = {"turn": turn, "child_turns": {}}
            turns[turn_key] = turn_block
        children = turn_block.setdefault("child_turns", {})
        children[str(child_turn)] = record
        return data

    @staticmethod
    def _atomic_write(path, data):
        """写入 JSON 数据。

        为兼容 TRAE 沙箱（禁止在 llm_calls 等业务目录下创建「.」开头的临时文件），
        默认走直接写，仅在检测到目标文件被占用时退化为「同目录非点前缀 tmp + replace」，
        仍失败则再次退化为直接写，绝不因为写临时文件而丢数据。
        """
        dir_ = os.path.dirname(path) or "."
        os.makedirs(dir_, exist_ok=True)

        # 策略 A：先尝试直接写（99% 场景能成功，对沙箱最友好）
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return
        except PermissionError:
            pass  # 文件被占用：进入 A' 临时文件方案
        except Exception as e:
            # 其他错误（编码等）不重试
            import sys as _sys
            print(f"[LOGGER][FATAL] 写入 {path} 失败: {e}", file=_sys.stderr)
            return

        # 策略 A'：同目录 tmp + replace（前缀不能以「.」开头，避免 TRAE 沙箱拦截）
        import errno, random
        pid = os.getpid()
        ts = int(time.time() * 1000)
        tmp = None
        fd = None
        for attempt in range(50):
            rand = random.randint(0, 999999)
            candidate = os.path.join(dir_, f"aw_{pid}_{ts}_{rand:06d}.tmp")
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                tmp = candidate
                break
            except FileExistsError:
                continue
            except PermissionError:
                # 沙箱/系统对 .tmp 也有限制 → 再换一种后缀名
                candidate2 = os.path.join(dir_, f"aw_{pid}_{ts}_{rand:06d}.part")
                try:
                    fd = os.open(candidate2, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                    tmp = candidate2
                    break
                except (FileExistsError, PermissionError):
                    continue
        if tmp is None or fd is None:
            StructuredLogger._direct_write(path, data)
            return

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            for _retry in range(5):
                try:
                    os.replace(tmp, path)
                    tmp = None
                    return
                except PermissionError:
                    time.sleep(0.05)
                except OSError:
                    break
            raise OSError("tmp replace failed")
        except Exception:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            StructuredLogger._direct_write(path, data)

    @staticmethod
    def _direct_write(path, data):
        """非原子直接写：原子写失败时的最终兜底。"""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            # 最后仍然失败 → 打印到 stderr，不静默丢失
            import sys as _sys
            print(f"[LOGGER][FATAL] 写入 {path} 最终失败: {e2}", file=_sys.stderr)

    def _do_log_llm_call(self, trace_id: str, session_id: str, turn: int,
                         child_turn: int, user_task: str,
                         response: str,
                         model_name: str = "", provider: str = "",
                         elapsed_ms: int = 0, token_estimate: int = 0,
                         session_name: str = "", reasoning: str = "",
                         system_prompt: str = "", user_input: str = "",
                         messages: list = None,
                         user_task_time: str = "",
                         route_level: str = "",
                         context_memory: list = None):

        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        # 如果 session_name 为空，尝试从 sessions DB 获取
        if not session_name:
            try:
                from memory import get_session
                session_data = get_session(session_id)
                if session_data and session_data.get("title"):
                    safe_name = session_data["title"].strip()
                    for ch in r'<>:"/\|?*':
                        safe_name = safe_name.replace(ch, '_')
                    if not safe_name:
                        safe_name = "untitled"
                    self.info(f"log_llm_call: 从DB获取session_name={safe_name}", session_id=session_id)
            except Exception:
                pass

        llm_root_dir = LOG_DIR / "llm_calls"
        try:
            llm_root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.warning(f"无法创建LLM日志目录 {llm_root_dir}: {e}")
            return

        file_path = llm_root_dir / f"{session_id}_{safe_name}.json"

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

        # ── 子轮次数据 ──
        # messages 是实际发给 LLM 的完整请求体（包含 user_content 拼接的所有内容：
        #   静态 system_prompt + 动态上下文 + 记忆摘要 + 工具结果 + 推理历史）
        # 为防止日志过大，对每条消息的 content 做截断（保留前 2000 字符）
        _MAX_MSG_CONTENT = 2000
        _truncated_messages = None
        if messages is not None:
            try:
                _truncated_messages = []
                for msg in messages:
                    m = dict(msg)
                    if isinstance(m.get("content"), str) and len(m["content"]) > _MAX_MSG_CONTENT:
                        m["content"] = m["content"][: _MAX_MSG_CONTENT] + f"\n... [截断，原始长度 {len(msg['content'])} 字符]"
                    elif isinstance(m.get("content"), list):
                        # 多模态消息：图片等只保留类型摘要
                        for block in m["content"]:
                            if isinstance(block, dict) and block.get("type") == "image_url":
                                block["_truncated"] = True
                                block.pop("url", None)
                    _truncated_messages.append(m)
            except Exception:
                _truncated_messages = str(messages)[:500]

        child_turn_data = {
            "child_turn": child_turn,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ai_thinking": reasoning.strip() if reasoning else "",
            "ai_output": response.strip() if response else "",
            "tool_calls": ai_tool_calls,
            "tool_result": "",
            "models": f"{provider}/{model_name}",
            "elapsed_ms": elapsed_ms,
            # 🆕 近3轮结构化记忆快照
            "context_memory": context_memory or [],
            # 🆕 完整请求体 messages（实际发给 LLM 的 user_content 拼接结果）
            "messages": _truncated_messages,
        }

        # ── 读取 + 修改 + 写入 整体放入锁内，避免读-改-写竞态（并发子轮次不互相覆盖） ──
        turn_key = str(turn)
        record = {k: v for k, v in child_turn_data.items() if k != "child_turn"}
        with self._llm_file_write_lock:
            # 构建/读取 turns 数据（dict 格式，兼容增量 merge）
            existing_data = {"session_id": session_id, "session_name": safe_name, "turns": {}}
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                        if isinstance(existing_data.get("turns"), list):
                            existing_data["turns"] = {}
                        elif "turns" not in existing_data:
                            existing_data["turns"] = {}
                except (json.JSONDecodeError, IOError):
                    existing_data = {"session_id": session_id, "session_name": safe_name, "turns": {}}

            turn_block = existing_data["turns"].get(turn_key)
            if turn_block is None:
                turn_block = {
                    "turn": turn,
                    "user_task": user_task,
                    "user_task_time": user_task_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "system_prompt": system_prompt or "",
                    "system_prompt_hash": self._hash_prompt(system_prompt) if system_prompt else "",
                    "route_level": route_level,
                    "ai_reply": "",
                    "ai_reply_time": "",
                    "reasoning_trace": "",
                    "child_turns": {}
                }
                existing_data["turns"][turn_key] = turn_block
            else:
                if user_task and turn_block.get("user_task", "") != user_task:
                    turn_block["user_task"] = user_task
                if system_prompt and not turn_block.get("system_prompt"):
                    turn_block["system_prompt"] = system_prompt
                    turn_block["system_prompt_hash"] = self._hash_prompt(system_prompt)
                if route_level and not turn_block.get("route_level"):
                    turn_block["route_level"] = route_level

            # 增量 merge 子轮次记录
            existing_data = self._merge_llm_record(existing_data, turn, child_turn, record)

            self._atomic_write(str(file_path), existing_data)

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
            buffer_len = len(self._llm_buffer)
        self._llm_flush_event.set()
        if buffer_len >= self._MAX_LLM_BUFFER // 2:
            self._llm_flush()

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

        # 读-改-写整体加锁，防止并发 update_tool_result / log_turn_complete 相互覆盖
        with self._llm_file_write_lock:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self.debug(f"update_tool_result: 文件解析失败 {file_path}: {e}")
                return

            t = data.get("turns", {}).get(str(turn))
            if t is not None:
                ct = t.get("child_turns", {}).get(str(child_turn))
                if ct is not None:
                    ct["tool_result"] = tool_result[:5000] if tool_result else ""
                else:
                    self.debug(f"update_tool_result: 未找到子轮次 {child_turn}")
                    return
            else:
                self.debug(f"update_tool_result: 未找到轮次 {turn}")
                return

            self._atomic_write(str(file_path), data)

    def log_turn_complete(self, session_id: str, session_name: str, turn: int,
                          ai_reply: str, ai_reply_time: str = "",
                          reasoning_trace: str = ""):
        with self._llm_buffer_lock:
            if len(self._llm_buffer) >= self._MAX_LLM_BUFFER:
                self._llm_buffer.pop(0)
            self._llm_buffer.append(("log_turn_complete", (
                session_id, session_name, turn, ai_reply, ai_reply_time, reasoning_trace
            )))
            buffer_len = len(self._llm_buffer)
        self._llm_flush_event.set()
        # 立即刷盘，确保 log_llm_call 写入的文件已存在
        self._llm_flush()

    def _do_log_turn_complete(self, session_id: str, session_name: str, turn: int,
                              ai_reply: str, ai_reply_time: str = "",
                              reasoning_trace: str = ""):
        safe_name = session_name.strip() if session_name else "untitled"
        for ch in r'<>:"/\|?*':
            safe_name = safe_name.replace(ch, '_')
        if not safe_name:
            safe_name = "untitled"

        # 如果 session_name 为空，尝试从 sessions DB 获取
        if not session_name:
            try:
                from memory import get_session
                session_data = get_session(session_id)
                if session_data and session_data.get("title"):
                    safe_name = session_data["title"].strip()
                    for ch in r'<>:"/\|?*':
                        safe_name = safe_name.replace(ch, '_')
                    if not safe_name:
                        safe_name = "untitled"
            except Exception:
                pass

        llm_root_dir = LOG_DIR / "llm_calls"
        file_path = llm_root_dir / f"{session_id}_{safe_name}.json"

        # ── 如果精确文件名不存在，尝试按 session_id 前缀匹配 ──
        if not file_path.exists():
            self.debug(f"log_turn_complete: 精确文件不存在 {file_path}，尝试前缀匹配")
            if llm_root_dir.exists():
                prefix = f"{session_id}_"
                matches = [f for f in llm_root_dir.iterdir()
                          if f.is_file() and f.name.startswith(prefix)]
                if matches:
                    file_path = matches[0]
                    self.info(f"log_turn_complete: 前缀匹配到文件 {file_path.name}")
                else:
                    self.debug(f"log_turn_complete: 无匹配文件 prefix={prefix}，尝试创建空文件")
                    # 没有日志文件也创建一个基础结构，确保后续可以写入
                    try:
                        from memory import get_session
                        session_data = get_session(session_id)
                        sn = safe_name
                        if session_data and session_data.get("title"):
                            sn = session_data["title"].strip()
                            for ch in r'<>:"/\|?*':
                                sn = sn.replace(ch, '_')
                        if not sn:
                            sn = "untitled"
                        init_data = {"session_id": session_id, "session_name": sn, "turns": {}}
                        self._atomic_write(str(file_path), init_data)
                        self.info(f"log_turn_complete: 创建基础文件 {file_path.name}")
                    except Exception as e:
                        self.warning(f"log_turn_complete: 创建基础文件失败 {e}")
                    # 不 return，继续尝试读取

        # 读-改-写整体加锁，避免与 _do_update_tool_result 并发覆盖
        with self._llm_file_write_lock:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self.debug(f"log_turn_complete: 文件解析失败 {file_path}: {e}")
                return

            t = data.get("turns", {}).get(str(turn))
            if t is not None:
                t["ai_reply"] = ai_reply or ""
                t["ai_reply_time"] = ai_reply_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if reasoning_trace:
                    t["reasoning_trace"] = reasoning_trace
            else:
                self.debug(f"log_turn_complete: 未找到轮次 {turn}")
                return

            self._atomic_write(str(file_path), data)

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
                turns = data.get("turns")
                if turns is None:
                    return None
                # 兼容 dict 格式（key 是 str(turn)），也兼容旧的 list 格式
                if isinstance(turns, dict):
                    return turns.get(str(turn))
                if isinstance(turns, list):
                    for t in turns:
                        if isinstance(t, dict) and t.get("turn") == turn:
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
        if LEVELS.get(level, 0) < LEVELS.get(_CONSOLE_LEVEL, 0):
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra_str = ""
        if extra:
            extra_str = " " + json.dumps(extra, ensure_ascii=False, default=str)
        print(f"[{level}] {timestamp} {message}{extra_str}")

    @staticmethod
    def _hash_prompt(text: str) -> str:
        """计算系统提示词的 SHA-256 摘要（用于完整性校验）"""
        import hashlib
        if not text:
            return ""
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def close(self):
        self._flush_event.set()
        self._llm_flush_event.set()
        if self._writer_thread:
            self._writer_thread.join(timeout=5)
        if self._llm_writer_thread:
            self._llm_writer_thread.join(timeout=5)
        self._shutdown_event.set()
        self._llm_shutdown_event.set()
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


# ═══════════════════════════════════════════════════════════════
# 🆕 TankLogger — Trace 管理 + Turn/ChildTurn 结构化记录
# ═══════════════════════════════════════════════════════════════


class TankLogger:
    """
    Trace 全生命周期管理：start_trace → log_turn_start → log_child_turn
    → log_tool_call_end → log_context_summary → end_trace

    层级设计：
    - Trace（≈Session）：会话元信息，不含系统提示词
    - Turn：系统提示词（每轮可能不同）、用户输入、路由层级
    - ChildTurn（≈SubTurn）：工具调用 + LLM详情 + context_memory（近3轮记忆）
      ※ ChildTurn 不记录 user_input（轮次级字段）

    输出：logs/traces/{trace_id}.json
    """

    def __init__(self):
        self._trace: dict | None = None
        self._current_turn: dict | None = None
        self._trace_lock = threading.Lock()
        self._trace_log_dir = LOG_DIR / "traces"
        self._trace_log_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Trace 生命周期 ──

    def start_trace(self, session_id: str, task_type: str = ""):
        """开始新的 trace"""
        with self._trace_lock:
            self._trace = {
                "trace_id": f"{session_id}_{uuid.uuid4().hex[:8]}",
                "session_id": session_id,
                "task_type": task_type,
                "start_time": self._now(),
                "turns": [],
                # 汇总字段（end_trace 时填入）
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tool_calls": 0,
                "total_search_calls": 0,
                "search_providers_used": [],
                "search_relevance_pass_rate": 0,
            }
            self._current_turn = None

    def end_trace(self, success: bool, summary: str = ""):
        """结束 trace，计算汇总统计并写入文件。"""
        with self._trace_lock:
            if not self._trace:
                return

            self._trace["end_time"] = self._now()
            self._trace["success"] = success
            self._trace["summary"] = summary

            # ── 汇总统计 ──
            total_pt = 0
            total_ct = 0
            search_total = 0
            search_passed = 0
            providers: set = set()

            for turn in self._trace.get("turns", []):
                for ct in turn.get("child_turns", []):
                    llm = ct.get("llm_call", {})
                    total_pt += llm.get("prompt_tokens", 0)
                    total_ct += llm.get("completion_tokens", 0)

                    tool_name_val = ct.get("tool_name", "")
                    tool_meta = ct.get("tool_meta", {})
                    if tool_name_val.startswith("search") or tool_name_val == "web_search":
                        search_total += 1
                        if tool_meta.get("relevance_passed", True):
                            search_passed += 1
                        if tool_meta.get("provider_used"):
                            providers.add(tool_meta["provider_used"])

            self._trace["total_prompt_tokens"] = total_pt
            self._trace["total_completion_tokens"] = total_ct
            self._trace["total_tool_calls"] = sum(
                1 for t in self._trace.get("turns", [])
                for ct in t.get("child_turns", [])
                if ct.get("tool_name")
            )
            self._trace["total_search_calls"] = search_total
            self._trace["search_providers_used"] = list(providers)
            self._trace["search_relevance_pass_rate"] = (
                round(search_passed / search_total, 2) if search_total > 0 else 1.0
            )

            # 写入文件
            try:
                trace_path = self._trace_log_dir / f"{self._trace['trace_id']}.json"
                with open(trace_path, "w", encoding="utf-8") as f:
                    json.dump(self._trace, f, ensure_ascii=False, indent=2, default=str)
                get_logger().info(f"Trace 已保存: {trace_path}",
                                  trace_id=self._trace["trace_id"],
                                  session_id=self._trace["session_id"])
            except OSError as e:
                get_logger().warning(f"Trace 写入失败: {e}",
                                     trace_id=self._trace.get("trace_id", "?"))

            self._current_turn = None

    # ── Turn 管理 ──

    def log_turn_start(self, turn_index: int, user_input: str,
                       system_prompt: str = "", route_level: str = ""):
        """
        开始一个新 turn。
        系统提示词记录在 Turn 级（每轮可能不同）。
        """
        with self._trace_lock:
            if not self._trace:
                return

            turn = {
                "turn_index": turn_index,
                "timestamp": self._now(),
                "user_input": user_input,
                "route_level": route_level,
                "child_turns": [],
                # ── 系统提示词（Turn 级，每轮独立） ──
                "system_prompt": system_prompt,
                "system_prompt_hash": self._hash_prompt(system_prompt),
                "system_prompt_tokens": self._estimate_tokens(system_prompt),
                # ── 轮次结束时回填 ──
                "ai_reply": "",
                "ai_reply_time": "",
                "reasoning_trace": "",
            }
            self._trace["turns"].append(turn)
            self._current_turn = turn

    def log_turn_end(self, ai_reply: str = "", reasoning_trace: str = ""):
        """结束当前 turn，回填 AI 回复和 reasoning_trace"""
        with self._trace_lock:
            if self._current_turn:
                self._current_turn["ai_reply"] = ai_reply
                self._current_turn["ai_reply_time"] = self._now()
                if reasoning_trace:
                    self._current_turn["reasoning_trace"] = reasoning_trace

    # ── ChildTurn 记录 ──

    def log_child_turn(
        self,
        child_index: int,
        ai_thinking: str,
        ai_output: str,
        tool_name: str = "",
        tool_input: dict = None,
        tool_result: str = "",
        # LLM 调用详情
        messages_sent: list = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        llm_latency_ms: float = 0,
        time_to_first_token_ms: float = 0,
        model_name: str = "",
        context_breakdown: dict = None,
        # 🆕 近3轮结构化记忆
        context_memory: list = None,
        context_memory_note: str = "",
    ):
        """
        记录一次完整的 child_turn：LLM 调用 + 工具调用 + 上下文记忆。
        ※ 不记录 user_input / system_prompt（这些是 Turn 级字段）
        """
        with self._trace_lock:
            child = {
                "child_turn": child_index,
                "timestamp": self._now(),
                "ai_thinking": ai_thinking,
                "ai_output": ai_output,
                "tool_name": tool_name,
                "tool_input": tool_input or {},
                "tool_result": tool_result[:2000] if tool_result else "",
                # LLM 调用详情（不含 system_prompt，已在 Turn 级记录）
                "llm_call": {
                    "model": model_name,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
                    "latency_ms": round(llm_latency_ms, 1),
                    "time_to_first_token_ms": round(time_to_first_token_ms, 1),
                    "context_breakdown": context_breakdown or {},
                },
                # 🆕 近3轮结构化记忆快照
                "context_memory": context_memory or [],
                "context_memory_note": context_memory_note,
            }

            # messages_sent 单独快照（避免主日志过大）
            if messages_sent:
                child["messages_sent_ref"] = self._save_messages_snapshot(
                    child_index, messages_sent
                )

            if self._current_turn:
                self._current_turn["child_turns"].append(child)

            return child

    # ── 工具调用记录 ──

    def log_tool_call_end(
        self,
        tool_name: str,
        success: bool,
        result: str,
        latency_ms: float,
        # 搜索专用元信息
        provider_used: str = "",
        raw_result_count: int = 0,
        filtered_result_count: int = 0,
        relevance_passed: bool = True,
        http_status: int = 0,
    ):
        """记录工具调用结束（回填执行结果和搜索元信息）"""
        with self._trace_lock:
            if self._current_turn and self._current_turn["child_turns"]:
                last_child = self._current_turn["child_turns"][-1]
                last_child["tool_success"] = success
                last_child["tool_latency_ms"] = round(latency_ms, 1)

                _SEARCH_TOOLS = {"web_search", "search_images", "search_news", "search_academic"}
                if tool_name in _SEARCH_TOOLS:
                    last_child["tool_meta"] = {
                        "provider_used": provider_used,
                        "raw_result_count": raw_result_count,
                        "filtered_result_count": filtered_result_count,
                        "relevance_passed": relevance_passed,
                        "http_status": http_status,
                    }

    # ── LLM 计时 ──

    def log_llm_timing(
        self,
        child_index: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        ttft_ms: float = 0,
    ):
        """记录 LLM 调用的 Token 用量和耗时（回填已有 child_turn）"""
        with self._trace_lock:
            if self._current_turn and self._current_turn["child_turns"]:
                last_child = self._current_turn["child_turns"][-1]
                last_child["llm_call"]["prompt_tokens"] = prompt_tokens
                last_child["llm_call"]["completion_tokens"] = completion_tokens
                last_child["llm_call"]["total_tokens"] = prompt_tokens + completion_tokens
                last_child["llm_call"]["latency_ms"] = round(latency_ms, 1)
                last_child["llm_call"]["time_to_first_token_ms"] = round(ttft_ms, 1)

        get_logger().info(
            f"[LLM_TIMING] child={child_index} "
            f"tokens={prompt_tokens}+{completion_tokens} "
            f"latency={latency_ms:.0f}ms ttft={ttft_ms:.0f}ms"
        )

    # ── 上下文摘要 ──

    def log_context_summary(self, turn_index: int, summary_text: str, trigger_reason: str = ""):
        """记录中期记忆摘要事件（超过 3 轮 / token 超限时触发）"""
        with self._trace_lock:
            if self._current_turn:
                self._current_turn["context_summary"] = {
                    "trigger_reason": trigger_reason,
                    "summary_text": summary_text,
                    "timestamp": self._now(),
                }

        get_logger().info(
            f"[CONTEXT_SUMMARY] turn={turn_index} reason={trigger_reason} len={len(summary_text)}"
        )

    # ── 结构化记忆构建 ──

    def build_context_memory(self, window: int = None) -> list:
        """
        从已完成的轮次中构建近 N 轮结构化记忆。
        在 log_child_turn 前调用，返回值传入 context_memory 参数。

        Returns:
            List[dict]，按时间正序（最旧→最新），每项包含：
            - turn_index, route_level, user_intent(摘要),
              tool_calls_summary, ai_reply_summary, status
        """
        if window is None:
            window = MEMORY_WINDOW

        with self._trace_lock:
            if not self._trace:
                return []

            # 收集已完成的轮次（排除当前正在进行的轮次）
            completed_turns = [
                t for t in self._trace["turns"]
                if t is not self._current_turn and t.get("ai_reply")
            ]

            recent = completed_turns[-window:]
            memory_items = []
            for turn in recent:
                tool_names = [
                    ct.get("tool_name", "") for ct in turn.get("child_turns", [])
                    if ct.get("tool_name")
                ]
                tool_summary = " → ".join(tool_names) if tool_names else "(无工具调用)"
                ai_summary = (turn.get("ai_reply") or "")[:200]

                memory_items.append({
                    "turn_index": turn["turn_index"],
                    "route_level": turn.get("route_level", ""),
                    "user_input_summary": (turn.get("user_input") or "")[:100],
                    "tool_calls_summary": tool_summary,
                    "ai_reply_summary": ai_summary,
                    "status": "completed",
                })

            return memory_items

    # ── 性能计时 ──

    def start_timer(self) -> float:
        """返回高精度当前时间（秒），供 stop_timer 计算差值。"""
        return time.perf_counter()

    @staticmethod
    def stop_timer(start: float) -> float:
        """返回 start 到现在的毫秒数。"""
        return (time.perf_counter() - start) * 1000

    # ── 辅助方法 ──

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（中文约 1.5 字/token，英文约 4 字符/token）。"""
        if not text:
            return 0
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    @staticmethod
    def _hash_prompt(text: str) -> str:
        """计算提示词 SHA-256 摘要"""
        import hashlib
        if not text:
            return ""
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def _save_messages_snapshot(self, child_index: int, messages: list) -> str:
        """将完整的 messages 数组保存到单独文件（避免主日志过大）。"""
        if not self._trace:
            return ""
        snapshot_dir = self._trace_log_dir / "snapshots" / self._trace["trace_id"]
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        filepath = snapshot_dir / f"child_{child_index}_messages.json"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2, default=str)
            return str(filepath)
        except (OSError, TypeError) as e:
            get_logger().warning(f"保存 messages 快照失败: {e}")
            return ""

    # ── 输出/序列化 ──

    def to_dict(self) -> dict | None:
        """返回当前 trace 的完整字典（只读视图）。"""
        with self._trace_lock:
            return dict(self._trace) if self._trace else None

    def to_json(self, indent: int = 2) -> str:
        """返回当前 trace 的 JSON 字符串。"""
        with self._trace_lock:
            if not self._trace:
                return "{}"
            return json.dumps(self._trace, ensure_ascii=False, indent=indent, default=str)

    @property
    def trace_id(self) -> str | None:
        with self._trace_lock:
            return self._trace["trace_id"] if self._trace else None


# ── TankLogger 全局单例 ──
_tank_logger: TankLogger | None = None
_tank_logger_lock = threading.Lock()


def get_tank_logger() -> TankLogger:
    global _tank_logger
    if _tank_logger is None:
        with _tank_logger_lock:
            if _tank_logger is None:
                _tank_logger = TankLogger()
    return _tank_logger


# ═══════════════════════════════════════════════════════════════
# SessionLogger — Session/Turn/SubTurn 生命周期管理
# ═══════════════════════════════════════════════════════════════


class SessionLogger:
    """
    Session 全生命周期管理：start_session → start_turn → start_sub_turn
    → end_sub_turn → end_turn → end_session → _save_session

    层级设计：
    - Session：会话元信息（不含系统提示词）
    - Turn：system_prompt_hash/version、user_input、user_task_time、
            ai_reply、ai_reply_time、route_level、reasoning_trace
    - SubTurn：tool_name、tool_params、tool_result、reasoning、耗时、
              context_memory（近3轮结构化记忆快照）
      ※ SubTurn 不记录 user_input / ai_reply / timestamp 等轮次级字段

    输出：logs/sessions/{session_id}.json
    """

    def __init__(self):
        self._current_session: dict | None = None
        self._current_turn: dict | None = None
        self._current_sub_turn: dict | None = None
        self._sessions_dir = LOG_DIR / "sessions"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._turn_counter = -1
        self._sub_turn_counter = -1

    @staticmethod
    def _generate_id(prefix: str = "") -> str:
        """生成唯一 ID"""
        uid = uuid.uuid4().hex[:8]
        if prefix:
            return f"{prefix}_{uid}"
        return uid

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _hash_prompt(text: str) -> str:
        """计算提示词 SHA-256 摘要"""
        import hashlib
        if not text:
            return ""
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ── Session 生命周期 ──

    def start_session(self, session_name: str = "", metadata: dict = None) -> str:
        """开始新的 session（不含系统提示词，已下沉到 Turn 级）"""
        # 如果有未关闭的会话，先关闭
        if self._current_session is not None:
            get_logger().warning(f"存在未关闭的会话，自动关闭: {self._current_session['session_id']}")
            self.end_session()

        session_id = self._generate_id("session")
        self._current_session = {
            "session_id": session_id,
            "session_name": session_name,
            "start_time": self._now_iso(),
            "end_time": "",
            "status": "active",
            "turns": [],
            "metadata": metadata or {},
        }
        self._current_turn = None
        self._current_sub_turn = None
        self._turn_counter = -1
        self._sub_turn_counter = -1
        get_logger().info(f"Session 开始: {session_id}")
        return session_id

    def end_session(self, status: str = "completed") -> None:
        """结束当前 session"""
        if self._current_session is None:
            get_logger().warning("end_session: 无活跃 session")
            return

        # 关闭未完成的轮次（内部会关闭未完成的子轮次）
        try:
            if self._current_turn is not None:
                self.end_turn(ai_reply="(会话结束，轮次中断)", status="interrupted")
        except Exception as e:
            get_logger().error(f"end_turn 异常，将继续保存 session: {e}")

        self._current_session["end_time"] = self._now_iso()
        self._current_session["status"] = status

        try:
            self._save_session()
        except Exception as e:
            get_logger().error(f"_save_session 异常: {e}")

        session_id = self._current_session["session_id"]
        self._current_session = None
        self._current_turn = None
        self._current_sub_turn = None
        get_logger().info(f"Session 结束: {session_id} [{status}]")

    def _save_session(self) -> None:
        """保存 session 到文件"""
        if not self._current_session:
            return
        file_path = self._sessions_dir / f"{self._current_session['session_id']}.json"
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self._current_session, f, ensure_ascii=False, indent=2, default=str)
        except OSError as e:
            get_logger().error(f"保存 session 失败: {e}")

    # ── Turn 管理（记录轮次级固定信息） ──

    def start_turn(self, user_input: str = "", user_intent: str = "",
                   route_level: str = "", system_prompt: str = "",
                   system_prompt_version: str = "",
                   reasoning_trace: str = "", metadata: dict = None) -> None:
        """
        开始新的 turn。

        Args:
            user_input: 用户原始输入
            user_intent: 意图分类
            route_level: 路由层级（L2 / L3）
            system_prompt: 本轮使用的完整系统提示词
            system_prompt_version: 提示词版本标识（如 "core_v2.1+safety_v1.3"）
            reasoning_trace: reasoning_trace JSON（可在 end_turn 时回填）
        """
        self._turn_counter += 1
        if not self._current_session:
            get_logger().warning("start_turn: 无活跃 session，自动创建")
            self.start_session()

        # 如果有未关闭的轮次，先关闭
        if self._current_turn is not None:
            get_logger().warning(f"存在未关闭的轮次，自动关闭")
            self.end_turn(ai_reply="(轮次被新轮次中断)", status="interrupted")

        turn = {
            "turn_index": self._turn_counter,
            # ── 轮次级固定信息（SubTurn 不重复记录） ──
            "user_input": user_input,
            "user_intent": user_intent,
            "user_task_time": self._now_iso(),
            "ai_reply": "",
            "ai_reply_time": "",
            "route_level": route_level,
            "reasoning_trace": reasoning_trace,
            # ── 系统提示词（Turn 级，每轮可能不同） ──
            "system_prompt_hash": self._hash_prompt(system_prompt),
            "system_prompt_version": system_prompt_version,
            # ── 状态 ──
            "status": "in_progress",
            "duration_ms": 0,
            # ── 子轮次列表 ──
            "sub_turns": [],
            "metadata": metadata or {},
        }
        self._current_session["turns"].append(turn)
        self._current_turn = turn
        self._current_sub_turn = None

    def end_turn(self, ai_reply: str = "", status: str = "completed",
                 error: str = "", reasoning_trace: str = "",
                 route_level: str = "") -> None:
        """
        结束当前 turn，记录 AI 回复。

        Args:
            ai_reply: AI 最终回复
            status: completed | failed | interrupted
            error: 错误信息
            reasoning_trace: 完整 reasoning_trace（如 start_turn 时未传入）
            route_level: 若发生 L2→L3 升级，在此更新
        """
        if self._current_turn is None:
            get_logger().warning("end_turn: 无活跃轮次")
            return

        # ★ 先关闭未完成的子轮次
        if self._current_sub_turn is not None:
            get_logger().warning("存在未关闭的子轮次，自动关闭")
            self.end_sub_turn(tool_result="(轮次结束，子轮次中断)", status="interrupted")

        now = self._now_iso()
        self._current_turn["ai_reply"] = ai_reply
        self._current_turn["ai_reply_time"] = now
        self._current_turn["status"] = status
        if error:
            self._current_turn["error"] = error
        if reasoning_trace:
            self._current_turn["reasoning_trace"] = reasoning_trace
        if route_level:
            self._current_turn["route_level"] = route_level

        # 计算轮次总耗时
        try:
            start = datetime.fromisoformat(self._current_turn["user_task_time"])
            end = datetime.fromisoformat(now)
            self._current_turn["duration_ms"] = (end - start).total_seconds() * 1000
        except (ValueError, TypeError, KeyError):
            self._current_turn["duration_ms"] = 0

        self._current_turn = None
        self._current_sub_turn = None
        self._save_session()

    # ── SubTurn 管理（仅记录工具调用 + 上下文记忆） ──

    def start_sub_turn(self, tool_name: str = "", tool_params: dict = None,
                       reasoning: str = "", context_memory: list = None,
                       context_memory_note: str = "",
                       metadata: dict = None) -> dict | None:
        """
        开始新的 sub-turn（一次工具调用）。

        Args:
            tool_name: 工具名称
            tool_params: 工具参数
            reasoning: 调用前推理（三问自检）
            context_memory: 近3轮结构化记忆快照
            context_memory_note: 记忆拼接说明
        """
        if self._sub_turn_counter < 14:
            self._sub_turn_counter += 1
        if not self._current_turn:
            get_logger().warning("start_sub_turn: 无活跃 turn，自动创建")
            self.start_turn(user_input="(auto-created turn)")

        # 如果有未关闭的子轮次，先关闭
        if self._current_sub_turn is not None:
            get_logger().warning("存在未关闭的子轮次，自动关闭")
            self.end_sub_turn(tool_result="(被新子轮次中断)", status="interrupted")

        sub_turn = {
            "sub_turn_index": self._sub_turn_counter,
            # ── 工具调用信息 ──
            "tool_name": tool_name,
            "tool_params": tool_params or {},
            "tool_result": "",
            "reasoning": reasoning,
            "status": "in_progress",
            "error": "",
            "start_time": self._now_iso(),  # ★ 修复 BUG 1：创建时即赋值
            "end_time": "",
            "duration_ms": 0,
            # ── 近3轮结构化记忆快照 ──
            "context_memory": context_memory or [],
            "context_memory_note": context_memory_note,
            # ── 扩展 ──
            "metadata": metadata or {},
        }
        self._current_sub_turn = sub_turn
        return sub_turn

    def end_sub_turn(self, tool_result: str = "", status: str = "completed",
                     error: str = "") -> None:
        """
        结束当前 sub-turn，记录工具返回结果。

        Args:
            tool_result: 工具返回结果（截断至 5000 字符）
            status: completed | failed | timeout | interrupted
            error: 错误信息
        """
        if self._current_sub_turn is None:
            get_logger().warning("end_sub_turn: 无活跃子轮次")
            return

        now = self._now_iso()
        self._current_sub_turn["tool_result"] = self._truncate(tool_result, 5000)
        self._current_sub_turn["status"] = status
        self._current_sub_turn["error"] = error
        self._current_sub_turn["end_time"] = now

        # 计算耗时
        try:
            start = datetime.fromisoformat(self._current_sub_turn["start_time"])
            end = datetime.fromisoformat(now)
            self._current_sub_turn["duration_ms"] = (end - start).total_seconds() * 1000
        except (ValueError, TypeError, KeyError) as e:
            get_logger().warning(f"计算子轮次耗时失败: {e}")
            self._current_sub_turn["duration_ms"] = 0

        # ★ 追加到当前轮次的 sub_turns 列表
        if self._current_turn is not None:
            self._current_turn["sub_turns"].append(self._current_sub_turn)

        self._current_sub_turn = None

    # ── 结构化记忆构建 ──

    def build_context_memory(self, window: int = None) -> list:
        """
        从已完成的轮次中构建近 N 轮结构化记忆。
        在 start_sub_turn 前调用，返回值传入 context_memory 参数。

        Returns:
            List[dict]，按时间正序，每项包含：
            turn_index, route_level, user_intent, tool_calls_summary,
            ai_reply_summary, status
        """
        if window is None:
            window = MEMORY_WINDOW

        if not self._current_session:
            return []

        # 收集已完成的轮次（排除当前轮次）
        completed_turns = [
            t for t in self._current_session["turns"]
            if t is not self._current_turn
            and t.get("status") in ("completed", "interrupted")
        ]

        recent = completed_turns[-window:]
        memory_items = []
        for turn in recent:
            tool_names = [
                st.get("tool_name", "") for st in turn.get("sub_turns", [])
                if st.get("tool_name")
            ]
            tool_summary = " → ".join(tool_names) if tool_names else "(无工具调用)"
            ai_summary = (turn.get("ai_reply") or "")[:200]

            memory_items.append({
                "user_intent": turn.get("user_intent", ""),
                "route_level": turn.get("route_level", ""),
                "user_input_summary": (turn.get("user_input") or "")[:100],
                "tool_calls_summary": tool_summary,
                "ai_reply_summary": ai_summary,
                "status": turn.get("status", ""),
            })

        return memory_items

    # ── 查询 ──

    def to_dict(self) -> dict | None:
        """返回当前 session 的完整字典"""
        return dict(self._current_session) if self._current_session else None

    def get_session_summary(self) -> dict:
        """获取当前会话摘要"""
        if not self._current_session:
            return {}
        return {
            "session_id": self._current_session["session_id"],
            "status": self._current_session["status"],
            "turn_count": len(self._current_session["turns"]),
            "total_sub_turns": sum(
                len(t.get("sub_turns", [])) for t in self._current_session["turns"]
            ),
            "prompt_versions_used": list(set(
                t.get("system_prompt_version", "") for t in self._current_session["turns"]
                if t.get("system_prompt_version")
            )),
        }

    @staticmethod
    def _truncate(text: str, max_len: int = 5000) -> str:
        """截断过长文本"""
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"\n...[truncated, total {len(text)} chars]"


# ── SessionLogger 全局单例 ──
_session_logger: SessionLogger | None = None
_session_logger_lock = threading.Lock()


def get_session_logger() -> SessionLogger:
    global _session_logger
    if _session_logger is None:
        with _session_logger_lock:
            if _session_logger is None:
                _session_logger = SessionLogger()
    return _session_logger


# ═══════════════════════════════════════════════════════════════
# 🆕 Per-Session 标准日志 —— 每个 session 独立的 .log 文件
# ═══════════════════════════════════════════════════════════════

import logging as _logging

_LOG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent', 'logs')
os.makedirs(_LOG_DIR, exist_ok=True)

_session_std_loggers: dict[str, _logging.Logger] = {}
_session_std_loggers_lock = threading.Lock()


def get_per_session_logger(session_id: str | int, level=_logging.DEBUG) -> _logging.Logger:
    """
    每个 session 独立的 logging.Logger，写入独立文本日志文件。
    解决 logging.getLogger 单例模式下不同 session 共用 handler 导致的问题。

    输出路径：logs/session_{session_id}_YYYY-MM-DD.log
    与控制台保持相同的格式化风格（带时间/级别/消息）。
    """
    sid = str(session_id)
    logger_name = f"MyAgent.session.{sid}"

    # 首先尝试从缓存拿，避免重复创建 handler
    with _session_std_loggers_lock:
        if sid in _session_std_loggers:
            return _session_std_loggers[sid]

    logger = _logging.getLogger(logger_name)

    # 单例保护：同一 logger_name 的 handlers 已存在则直接返回
    if logger.handlers:
        with _session_std_loggers_lock:
            _session_std_loggers[sid] = logger
        return logger

    logger.setLevel(level)

    # 文件 handler：每个 session 一个文件，按天切分
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(_LOG_DIR, f"session_{sid}_{date_str}.log")
    file_handler = _logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(_logging.DEBUG)

    # 控制台 handler
    console_handler = _logging.StreamHandler()
    console_handler.setLevel(_logging.INFO)

    formatter = _logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # 不向上传播，避免重复输出到 root logger
    logger.propagate = False

    with _session_std_loggers_lock:
        _session_std_loggers[sid] = logger

    return logger


def get_global_logger() -> _logging.Logger:
    """非 session 相关的全局日志，写入 logs/global.log"""
    logger_name = "MyAgent.global"
    logger = _logging.getLogger(logger_name)

    if logger.handlers:
        return logger

    os.makedirs(_LOG_DIR, exist_ok=True)
    handler = _logging.FileHandler(
        os.path.join(_LOG_DIR, "global.log"), encoding="utf-8"
    )
    handler.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.setLevel(_logging.DEBUG)
    logger.propagate = False
    return logger