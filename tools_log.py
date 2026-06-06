import os
import time
import uuid
import csv
import inspect
from functools import wraps

class AgentLogger:
    def __init__(self, log_root="run_log"):
        self.log_root = log_root
        self.dir_input = os.path.join(log_root, "raw_input")
        self.dir_output = os.path.join(log_root, "ai_output")
        self.dir_code = os.path.join(log_root, "code_snap")
        self.index_file = os.path.join(log_root, "log_index.csv")
        self._init_dirs()
        self._init_index()

    def _init_dirs(self):
        for d in [self.dir_input, self.dir_output, self.dir_code]:
            os.makedirs(d, exist_ok=True)

    def _init_index(self):
        if not os.path.exists(self.index_file):
            with open(self.index_file, "w", encoding="utf-8", newline="") as f:
                wr = csv.writer(f)
                wr.writerow(["timestamp", "session_id", "input_path", "output_path", "code_path"])

    def save_input(self, session_id:str, user_input:str):
        fname = f"{session_id}_input.txt"
        path = os.path.join(self.dir_input, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(user_input)
        return path

    def save_output(self, session_id:str, ai_out:str):
        fname = f"{session_id}_output.txt"
        path = os.path.join(self.dir_output, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(ai_out)
        return path

    def save_code_snap(self, session_id:str, source_code:str):
        fname = f"{session_id}_code.py"
        path = os.path.join(self.dir_code, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source_code)
        return path

    def add_index(self, session_id, in_path, out_path, code_path):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.index_file, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([ts, session_id, in_path, out_path, code_path])

    def record_one_round(self, user_input, ai_output, code_str):
        sid = str(uuid.uuid4())[:16]
        ip = self.save_input(sid, user_input)
        op = self.save_output(sid, ai_output)
        cp = self.save_code_snap(sid, code_str)
        self.add_index(sid, ip, op, cp)
        return sid

# ====== 关键点：先实例化logger，再定义装饰器 ======
agent_logger = AgentLogger()

# 装饰器定义放在实例之后，IDE不再标红未解析
def log_execution(logger=None):
    # 不传参默认使用全局agent_logger
    if logger is None:
        logger = agent_logger
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            input_str = f"args:{args}\nkwargs:{kwargs}"
            result = func(*args, **kwargs)
            code_src = inspect.getsource(func)
            logger.record_one_round(input_str, str(result), code_src)
            return result
        return wrapper
    return decorator