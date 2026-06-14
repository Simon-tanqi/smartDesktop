import json
import os
import time
import datetime
import uuid
import sqlite3
import requests  # --- NEW: 用于 Ollama 调用 ---

# ==================== 目录结构 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 记忆数据
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

# 会话数据
SESSION_DIR = os.path.join(BASE_DIR, "session")
os.makedirs(SESSION_DIR, exist_ok=True)

# 日志文件
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# --- memory/ ---
MEMORY_FILE = os.path.join(MEMORY_DIR, "user_memory.json")
COMPRESSED_FILE = os.path.join(MEMORY_DIR, "compressed_memory.json")
SHORT_TERM_FILE = os.path.join(MEMORY_DIR, "short_term_memory.json")

# --- session/ ---
SESSION_LOG_FILE = os.path.join(SESSION_DIR, "session_log.json")
CHAT_DB = os.path.join(SESSION_DIR, "chat_history.db")
ACTIVE_SESSION_FILE = os.path.join(SESSION_DIR, "active_session.txt")
USER_ID_FILE = os.path.join(SESSION_DIR, "user_id.txt")

# --- 升级长期记忆结构 ---
DEFAULT_LONG_TERM = {
    "user_info": {
        "name": "",
        "job_title": "",
        "company": ""
    },
    "core_knowledge": [],  # 经验/技能
    "projects": [],  # 项目信息
    "preferences": [],  # 偏好
    "facts": []  # 一般事实
}

# --- logs/ ---
OPERATION_LOG_DB = os.path.join(LOGS_DIR, "operation_log.db")
TRACE_DB = os.path.join(LOGS_DIR, "agent_trace.db")


# ==================== 用户标识 ====================

def get_user_id():
    """获取或生成持久用户ID。单用户桌面环境使用固定ID。"""
    if os.path.exists(USER_ID_FILE):
        with open(USER_ID_FILE, "r") as f:
            uid = f.read().strip()
            if uid:
                return uid
    uid = f"user_{uuid.uuid4().hex[:8]}"
    with open(USER_ID_FILE, "w") as f:
        f.write(uid)
    return uid


def generate_trace_id():
    """生成每次请求的追踪ID。"""
    return uuid.uuid4().hex[:12]


# ==================== Agent 节点追踪日志 (SQLite) ====================


def _get_trace_db():
    conn = sqlite3.connect(TRACE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT,
            parent_span_id INTEGER,
            span_name TEXT NOT NULL,
            node_type TEXT NOT NULL,
            provider TEXT,
            model_name TEXT,
            model_params TEXT,
            input_summary TEXT,
            output_summary TEXT,
            error TEXT,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success',
            extra TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_trace_id ON agent_trace(trace_id)")
    conn.commit()
    return conn


def log_trace(trace_id, span_name, node_type, duration_ms,
              session_id=None, parent_span_id=None,
              provider=None, model_name=None, model_params=None,
              input_summary=None, output_summary=None,
              error=None, status="success", extra=None):
    """写入一条节点追踪记录"""
    conn = _get_trace_db()
    trunc = lambda s: (str(s)[:500] + "...(截断)") if s and len(str(s)) > 500 else (str(s) if s else None)
    conn.execute("""
        INSERT INTO agent_trace
            (trace_id, session_id, parent_span_id, span_name, node_type,
             provider, model_name, model_params, input_summary, output_summary,
             error, duration_ms, status, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trace_id, session_id, parent_span_id, span_name, node_type,
          provider, model_name, model_params,
          trunc(input_summary), trunc(output_summary),
          error, duration_ms, status, extra))
    conn.commit()
    conn.close()


# ==================== 操作日志 (SQLite) ====================

def _get_op_log_db():
    conn = sqlite3.connect(OPERATION_LOG_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT,
            operation TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_trace ON operation_log(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_user ON operation_log(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_session ON operation_log(session_id)")
    conn.commit()
    return conn


def log_operation(trace_id, user_id, session_id, operation, detail=""):
    """写入结构化操作日志。detail 中的 \\r\\n 会被替换为空格。"""
    detail = _sanitize(detail)
    conn = _get_op_log_db()
    conn.execute(
        "INSERT INTO operation_log (trace_id, user_id, session_id, operation, detail) VALUES (?, ?, ?, ?, ?)",
        (trace_id, user_id, session_id, operation, detail)
    )
    conn.commit()
    conn.close()


def _sanitize(text):
    """清洗日志内容：移除回车/换行/制表符，压缩多余空白。"""
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


# --- NEW: Ollama 调用逻辑 ---
def _ollama_call(messages, model="qwen2:7b"):  # 默认使用 qwen2:7b，你可以根据本地模型名调整
    """
    调用本地 Ollama 模型。
    messages: List[Dict], 例如 [{"role": "user", "content": "..."}]
    model: Str, 本地模型名称
    """
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.3}  # 低温度保证摘要稳定性
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[Ollama] 请求失败: {e}")
        return None


def load_long_term():
    if not os.path.exists(MEMORY_FILE):
        return DEFAULT_LONG_TERM.copy()
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 合并默认值，确保所有字段都存在（兼容旧文件）
    merged = DEFAULT_LONG_TERM.copy()
    # 递归合并 user_info 字典
    if "user_info" in data:
        merged["user_info"].update(data["user_info"])
    # 其他列表字段直接合并（保留旧数据+默认空列表）
    for key in ["core_knowledge", "projects", "preferences", "facts"]:
        if key in data and isinstance(data[key], list):
            merged[key].extend(data[key])  # 或者直接 merged[key] = data[key] 覆盖
        elif key in data:
            merged[key] = data[key]  # 非列表情况直接赋值
    return merged


def save_long_term(memory):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


def load_compressed():
    if not os.path.exists(COMPRESSED_FILE):
        return []
    with open(COMPRESSED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_compressed(compressed_list):
    with open(COMPRESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(compressed_list, f, ensure_ascii=False, indent=2)


def add_compressed_summary(summary, source_type="auto"):
    """
    保存压缩摘要。
    source_type: auto(自动轮次压缩) | manual(手动保存)
    """
    compressed = load_compressed()
    compressed.append({
        "summary": summary,
        "timestamp": time.time(),
        "source": source_type
    })
    save_compressed(compressed)
    # --- NEW: 限制压缩记忆数量，防止过大 ---
    if len(compressed) > 50:
        save_compressed(compressed[-50:])


def get_recent_compressed(days=7):
    compressed = load_compressed()
    cutoff = time.time() - days * 86400
    recent = [c for c in compressed if c["timestamp"] >= cutoff]
    if not recent:
        return ""
    return "【历史对话摘要】\n" + "\n".join(f"- {c['summary']}" for c in recent)


def log_session_turn(role, content, trace_id="", user_id="", session_id=""):
    """记录会话轮次到操作日志（结构化SQL）"""
    log_operation(trace_id, user_id, session_id,
                  operation=f"chat.{role}",
                  detail=content)


def _load_short_term_db():
    if not os.path.exists(SHORT_TERM_FILE):
        return {}
    with open(SHORT_TERM_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_short_term_db(db):
    with open(SHORT_TERM_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def generate_session_id():
    """生成会话ID：年月日小时_序号，例如 2025121514_1"""
    now = datetime.datetime.now()
    base = now.strftime("%Y%m%d%H")
    db = _load_short_term_db()
    max_num = 0
    for sid in db:
        if sid.startswith(base):
            try:
                num = int(sid.split("_")[1])
                if num > max_num:
                    max_num = num
            except:
                continue
    return f"{base}_{max_num + 1}"


def create_session(title="新会话"):
    session_id = generate_session_id()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = _load_short_term_db()
    db[session_id] = {
        "id": session_id,
        "title": title,
        "created": now,
        "messages": []
    }
    _save_short_term_db(db)
    set_active_session(session_id)
    return session_id


def get_active_session_id():
    if os.path.exists(ACTIVE_SESSION_FILE):
        with open(ACTIVE_SESSION_FILE, "r") as f:
            sid = f.read().strip()
        db = _load_short_term_db()
        if sid and sid in db:
            return sid
    return create_session("新会话")


def set_active_session(session_id):
    with open(ACTIVE_SESSION_FILE, "w") as f:
        f.write(session_id)


def get_session_messages(session_id):
    db = _load_short_term_db()
    return db.get(session_id, {}).get("messages", [])

def compress_old_conversations(session_id, keep_rounds=8, summary_model="qwen2:7b"):
    """压缩会话历史：保留最近 keep_rounds 轮用户对话，其余生成摘要后删除"""
    db = _load_short_term_db()
    if session_id not in db:
        return
    messages = db[session_id]["messages"]
    # 找到所有用户消息的索引
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) <= keep_rounds:
        return
    keep_start = user_indices[-keep_rounds]   # 保留从此索引开始的所有消息
    old_msgs = messages[:keep_start]
    new_msgs = messages[keep_start:]

    # 生成摘要（使用已有的 _ollama_call）
    if old_msgs:
        # 将旧消息转为文本片段，限制长度
        old_text = json.dumps(old_msgs, ensure_ascii=False)[:1500]
        sys_prompt = {"role": "system", "content": f"请用一句话（不超过50字）概括以下对话的核心内容：\n{old_text}"}
        resp = _ollama_call([sys_prompt], model=summary_model)
        summary = resp["message"]["content"].strip() if resp and "message" in resp else ""
        if summary:
            # 将摘要存入长期压缩记忆（已有函数 add_compressed_summary）
            add_compressed_summary(summary, source_type="auto")
            # 在保留的消息最前面插入一条 system 消息，内容为摘要
            new_msgs.insert(0, {"role": "system", "content": f"【历史摘要】{summary}"})
    db[session_id]["messages"] = new_msgs
    _save_short_term_db(db)

# --- CORE: 核心改造函数 ---
def append_session_message(session_id, role, content, trace_id="", user_id="", max_turns=8, summary_model="qwen2:7b", skip_compress=False):
    """
    智能记忆写入：
    1. 闲聊过滤
    2. 事实/项目提取 (利用 Ollama)
    3. 超过 max_turns 自动压缩 (利用 Ollama 提炼)
    """
    db = _load_short_term_db()
    content = content.strip()
    if not content:
        return session_id

    # --- 1. 闲聊过滤 (快速规则) ---
    noise_phrases = ['你好', '在吗', '谢谢', '再见', '哦', '嗯', '哈哈', '哈喽', 'hey', 'hi','你是谁']
    if (content in noise_phrases) or (len(content) < 3 and any(p in content for p in ['好的', '没问题', '知道了'])):
        log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
        return session_id

    # --- 确保会话存在（兼容前端生成的 session_id 格式） ---
    if session_id not in db:
        db[session_id] = {
            "id": session_id,
            "title": content[:25] if content else "新会话",
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "messages": []
        }

    if not skip_compress and role == "user":
        user_turns = sum(1 for m in db[session_id]["messages"] if m["role"] == "user")
        if user_turns > max_turns:
            compress_conversation(session_id, keep_rounds=max_turns, summary_model=summary_model,
                                  trace_id=trace_id, user_id=user_id)

    if role == "assistant":
        if "tool_name" in content or "progress" in content or "final_message" in content:
            pass
        else:
            noise_phrases = ['你好', '在吗', '谢谢', '再见', '哦', '嗯', '哈哈']
            if (content in noise_phrases) or (
                    len(content) < 6 and any(p in content for p in ['好的', '没问题', '知道了'])):
                log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
                return session_id
    # --- 2. 消息追加 ---
    if role == "assistant":
        # 确保会话结构存在
        if session_id not in db:
            db[session_id] = {
                "id": session_id,
                "title": "新会话",
                "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "messages": []
            }
        db[session_id]["messages"].append({"role": role, "content": content})
    elif role == "user":

        # A. 利用本地模型提取事实/项目/经验
        # 构建 Prompt
        system_prompt = {
            "role": "system",
            "content": """
                你是一个记忆提取专家。请分析用户的输入，判断并提取以下信息：
                1. 是否包含【项目信息】（如开发、负责、做了XX系统）？
                2. 是否包含【经验/技能】（如擅长、精通、熟悉XX技术）？
                3. 是否包含【一般事实】（如名字、年龄、喜好，经历）？

                如果包含，请用 JSON 格式输出，只包含非空的字段。
                格式：
                {"project": "项目描述", "knowledge": "技能描述", "fact": "事实描述"}
                如果都不包含，输出：{"none": true}
            """.strip()
        }

        user_prompt = {"role": "user", "content": content}
        ai_response = _ollama_call([system_prompt, user_prompt], summary_model)

        if ai_response and "message" in ai_response:
            try:
                analysis = json.loads(ai_response["message"]["content"])

                # 更新长期记忆
                long_term = load_long_term()
                updated = False

                if "project" in analysis:
                    proj_str = f"[{datetime.datetime.now().strftime('%m-%d')}] {analysis['project']}"
                    if proj_str not in long_term["projects"]:
                        long_term["projects"].append(proj_str)
                        updated = True

                if "knowledge" in analysis:
                    if analysis["knowledge"] not in long_term["core_knowledge"]:
                        long_term["core_knowledge"].append(analysis["knowledge"])
                        updated = True

                if "fact" in analysis and analysis["fact"] is not None:
                    if analysis["fact"] not in long_term["facts"]:
                        long_term["facts"].append(analysis["fact"])
                        updated = True

                if updated:
                    save_long_term(long_term)
                    log_operation(trace_id, user_id, session_id,
                                  "memory.extract", f"long_term updated: {analysis}")

            except json.JSONDecodeError:
                log_operation(trace_id, user_id, session_id,
                              "memory.extract.error", "JSON parse failed")

        # B. 初始化会话结构
        if session_id not in db:
            db[session_id] = {
                "id": session_id,
                "title": content[:20] if content else "无标题",
                "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "messages": []
            }

        # C. 检查轮次并触发压缩
        messages = db[session_id]["messages"]
        # 追加当前消息（临时）
        messages.append({"role": role, "content": content})

        # 统计用户轮数
        user_turns = sum(1 for m in messages if m["role"] == "user")

        if user_turns > max_turns:
            log_operation(trace_id, user_id, session_id,
                          "memory.compress.start", f"turns {user_turns} > {max_turns}")

            # --- 压缩逻辑 ---
            # 1. 找到需要压缩的边界 (保留最近 max_turns 轮)
            keep_index = 0
            turn_count = 0
            for i, msg in enumerate(messages):
                if msg["role"] == "user":
                    turn_count += 1
                    if turn_count <= (user_turns - max_turns):
                        keep_index = i + 1  # 下一条开始保留
                    else:
                        break

            # 2. 提取需要压缩的旧对话
            old_dialogue = messages[:keep_index]
            if old_dialogue:
                # 构建摘要 Prompt
                summary_system = {
                    "role": "system",
                    "content": f"""
                        你是一个对话摘要专家。请阅读以下对话，提取核心信息，生成一段简洁的摘要（不超过50字）。
                        重点：保留用户提到的关键事实、项目名称或需求，忽略客套话。
                        对话记录：
                        {json.dumps(old_dialogue, ensure_ascii=False, )[:1000]} # 限制长度防止爆 Token
                    """.strip()
                }

                summary_res = _ollama_call([summary_system], summary_model)
                if summary_res and "message" in summary_res:
                    summary_text = summary_res["message"]["content"].strip()
                    # 保存到压缩记忆
                    add_compressed_summary(f"【自动压缩】{summary_text}", source_type="auto")
                    log_operation(trace_id, user_id, session_id,
                                  "memory.compress.done", summary_text)

            # 3. 修剪消息列表
            # 保留 system 消息（如果有）+ 最新对话
            system_msg = next((m for m in messages if m["role"] == "system"), None)
            recent_messages = messages[keep_index:]

            final_messages = []
            if system_msg:
                final_messages.append(system_msg)
            final_messages.extend(recent_messages)

            db[session_id]["messages"] = final_messages

        else:
            # 没超限，正常追加
            db[session_id]["messages"] = messages

        # D. 更新标题 (如果还是默认标题)
        if db[session_id]["title"] == "新会话" or db[session_id]["title"].startswith("Session_"):
            db[session_id]["title"] = content[:25]

    # --- 3. 保存 & 日志 ---
    _save_short_term_db(db)
    log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)

    return session_id

def _generate_summary(messages, model="qwen2:7b"):
    """调用 Ollama 生成对话摘要"""
    if not messages:
        return ""
    # 截取前 2000 字符避免过长
    dialogue = json.dumps(messages, ensure_ascii=False)[:2000]
    sys_prompt = {
        "role": "system",
        "content": f"请用一句话（不超过50字）总结以下对话的核心内容，保留用户需求、项目或事实：\n{dialogue}"
    }
    resp = _ollama_call([sys_prompt], model=model)
    if resp and "message" in resp:
        return resp["message"]["content"].strip()
    return ""

def compress_conversation(session_id, keep_rounds=8, summary_model="qwen2:7b", trace_id="", user_id=""):
    """压缩指定会话的历史消息，保留最近 keep_rounds 个用户轮次"""
    db = _load_short_term_db()
    if session_id not in db:
        return
    messages = db[session_id]["messages"]
    # 找出所有用户消息的索引
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indices) <= keep_rounds:
        return
    # 保留从第 (len - keep_rounds) 个用户消息开始
    keep_start = user_indices[-keep_rounds]
    old_msgs = messages[:keep_start]
    new_msgs = messages[keep_start:]

    summary = _generate_summary(old_msgs, model=summary_model)
    if summary:
        add_compressed_summary(summary, source_type="auto")
        new_msgs.insert(0, {"role": "system", "content": f"【历史摘要】{summary}"})
        log_operation(trace_id, user_id, session_id, "memory.compress.done", summary)
    db[session_id]["messages"] = new_msgs
    _save_short_term_db(db)

def list_sessions():
    db = _load_short_term_db()
    sessions = []
    for sid, data in db.items():
        sessions.append({
            "id": sid,
            "title": data.get("title", sid),
            "created_at": data.get("created", "")
        })
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    return sessions


def delete_session(session_id):
    db = _load_short_term_db()
    if session_id in db:
        del db[session_id]
        _save_short_term_db(db)
        delete_chat_session(session_id)
        if os.path.exists(ACTIVE_SESSION_FILE):
            with open(ACTIVE_SESSION_FILE, "r") as f:
                if f.read().strip() == session_id:
                    os.remove(ACTIVE_SESSION_FILE)


def get_log_dir():
    return LOGS_DIR


def save_ai_log(content: str, filename: str = None):
    if not content.strip():
        return
    if filename is None:
        filename = f"ai_log{datetime.datetime.now().strftime('%Y%m%d')}.md"
    log_path = os.path.join(get_log_dir(), filename)
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"\n---\n**{timestamp}**\n\n{content}\n")



def update_last_turn(session_id, turn):
    db = _load_short_term_db()
    if session_id in db:
        db[session_id]["last_turn"] = turn
        _save_short_term_db(db)

def get_last_turn(session_id):
    db = _load_short_term_db()
    return db.get(session_id, {}).get("last_turn", 0)


# ==================== SQLite 聊天记录 ====================

def _get_chat_db():
    """获取数据库连接，自动初始化表结构"""
    conn = sqlite3.connect(CHAT_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 兼容旧表：追加缺失列
    for col, col_type in [("msg_type", "TEXT DEFAULT 'chat'"),
                           ("trace_id", "TEXT NOT NULL DEFAULT ''"),
                           ("user_id", "TEXT NOT NULL DEFAULT ''")]:
        try:
            conn.execute(f"ALTER TABLE chat_history ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_trace ON chat_history(trace_id)"
    )
    conn.commit()
    return conn


def add_chat_message(session_id, role, content, msg_type="chat", trace_id="", user_id=""):
    """写入聊天记录（仅 user 和 assistant）"""
    if role not in ("user", "assistant"):
        return
    conn = _get_chat_db()
    conn.execute(
        "INSERT INTO chat_history (trace_id, user_id, session_id, role, content, msg_type) VALUES (?, ?, ?, ?, ?, ?)",
        (trace_id, user_id, session_id, role, content, msg_type)
    )
    conn.commit()
    conn.close()


def get_chat_messages(session_id, limit=200, offset=0):
    """读取聊天记录，按时间正序，支持分页"""
    conn = _get_chat_db()
    rows = conn.execute(
        "SELECT role, content, msg_type FROM chat_history "
        "WHERE session_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (session_id, limit, offset)
    ).fetchall()
    conn.close()
    return [{"role": row[0], "content": row[1], "msg_type": row[2]} for row in rows]


def count_chat_messages(session_id):
    """统计会话聊天记录总数"""
    conn = _get_chat_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM chat_history WHERE session_id = ?",
        (session_id,)
    ).fetchone()[0]
    conn.close()
    return count


def delete_chat_session(session_id):
    """删除指定会话的全部聊天记录"""
    conn = _get_chat_db()
    conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
