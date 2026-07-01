import re as _re

from flask import (Flask, render_template, request, jsonify, Response,
                     stream_with_context, send_from_directory)
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os, json, datetime, difflib, hashlib
from concurrent.futures import ThreadPoolExecutor
from llm_client import get_available_models, ai_concat
from memory import (append_session_message, set_active_session, get_active_session_id,
                    list_sessions, add_chat_message, get_chat_messages,
                    delete_session, get_user_id, generate_trace_id,
                    log_operation, rename_session, append_fact)
from memory.long_term import _rewrite_md, _read_lines
from memory.config import USER_MD, FACTS_MD
from memory.session import ensure_session_titles, create_session
from utils.error_mapper import translate_error
from logger import get_logger
from backend.upload_api import upload_bp, cleanup_session_uploads
from backend.agent import run_agent


# ── 附件标记常量 ──
_ATTACHMENT_START = "<attachments>"
_ATTACHMENT_END = "</attachments>"


def _extract_attachments(user_message: str):
    """从用户消息末尾提取 <attachments> 块，返回 (clean_message, file_list)。

    file_list: [(file_name, absolute_path), ...]
    """
    start = user_message.find(_ATTACHMENT_START)
    end = user_message.find(_ATTACHMENT_END)
    if start == -1 or end == -1 or end <= start:
        return user_message, []

    file_list = []
    block = user_message[start + len(_ATTACHMENT_START):end]
    for line in block.strip().split("\n"):
        line = line.strip()
        # 支持 `- [文件名](<路径>)` 和 `[文件名](<路径>)` 两种格式
        m = _re.search(r"\[(.+?)\]\(<(.+?)>\)", line)
        if m:
            file_list.append((m.group(1), m.group(2)))

    clean = (user_message[:start] + user_message[end + len(_ATTACHMENT_END):]).strip()
    return clean, file_list


def _inject_attachment_context(user_message: str, file_list: list):
    """将上传文件信息注入到 user_message 末尾，供 LLM 感知。

    格式：
    <attachments>
    - [文件名](绝对路径)
    </attachments>
    """
    if not file_list:
        return user_message
    lines = ["", _ATTACHMENT_START]
    for name, path in file_list:
        lines.append(f"- [{name}]({path})")
    lines.append(_ATTACHMENT_END)
    return user_message + "\n".join(lines)


app = Flask(__name__,template_folder='web',static_folder='web/static')
app.register_blueprint(upload_bp)

# ── 截图静态服务 ──
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "workspace", "screenshots")

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOTS_DIR, filename)

# ── CORS ──
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# ── Rate Limiting (IP 级，全 API 路由) ──
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["30 per minute", "200 per hour"],
    storage_uri="memory://",
)

# ── 工具执行线程池单例（避免每次工具调用重复新建/销毁）──
import atexit
import threading
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-")
atexit.register(_TOOL_EXECUTOR.shutdown, wait=False)

# ── 命令确认机制：agent 循环检测到 confirmation_required 时挂起等待 ──
# token → {"event": threading.Event, "result": {"confirmed": bool}}
_pending_confirmations: dict = {}
_pending_lock = threading.Lock()

def wait_for_confirmation(confirmation_token: str, timeout: float = 300) -> dict:
    """挂起等待前端确认。返回 {"confirmed": True} 或 {"confirmed": False}。"""
    evt = threading.Event()
    result_holder = {"confirmed": False}
    with _pending_lock:
        _pending_confirmations[confirmation_token] = {"event": evt, "result": result_holder}
    got_signal = evt.wait(timeout)
    with _pending_lock:
        _pending_confirmations.pop(confirmation_token, None)
    if not got_signal:
        return {"confirmed": False, "timeout": True}
    return result_holder

# ---------- 工作空间配置 ----------
workspace_path = os.path.dirname(os.path.abspath(__file__))

# ---------- 三层路由：高频固化缓存文件 ----------
_DATA_DIR = os.path.join(workspace_path, "data")
_CACHED_ACTIONS_PATH = os.path.join(_DATA_DIR, "cached_actions.json")
os.makedirs(_DATA_DIR, exist_ok=True)

def _load_cached_actions() -> dict:
    """加载固化记录文件，损坏时返回空结构。"""
    if not os.path.exists(_CACHED_ACTIONS_PATH):
        return {"固化记录": {}}
    try:
        with open(_CACHED_ACTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        backup = _CACHED_ACTIONS_PATH + ".corrupted"
        os.replace(_CACHED_ACTIONS_PATH, backup)
        return {"固化记录": {}}

def _save_cached_actions(data: dict):
    """原子写入固化记录（先写临时文件再 os.replace）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                      dir=_DATA_DIR, delete=False, suffix=".tmp") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmpname = tf.name
    os.replace(tmpname, _CACHED_ACTIONS_PATH)
# ---------- 模型选择（可从前端传参） ----------
available_models = get_available_models()
default_model = available_models[0] if available_models else {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"} #默认使用本地模型

def get_model_choice(display_name=None, model_id=None):
    """根据 display_name 或 model_id 查找模型配置"""
    for m in available_models:
        if model_id and m.get("id") == model_id:
            return m
        if m["display"] == display_name:
            return m
    return default_model

# ---------- Flask 路由 ----------
@app.route('/api/sessions', methods=['POST'])
def save_session():
    try:
        data = request.json
        session_id = data.get("id")
        if session_id:
            set_active_session(session_id)
            # 确保会话在 JSON DB 中存在（本地创建 fallback 路径不会写入 JSON DB）
            from memory.session import load_sessions_db, save_sessions_db
            db = load_sessions_db()
            if session_id not in db:
                db[session_id] = {
                    "id": session_id,
                    "title": data.get("title", "新会话"),
                    "created": data.get("created_at", datetime.datetime.now().isoformat()),
                }
                save_sessions_db(db)
            # 同步保存前端传递的消息到 SQLite（处理前端缓存中的消息）
            messages = data.get("messages", [])
            if messages:
                from memory.stores.sqlite_store import get_chat_messages, add_chat_message
                existing = get_chat_messages(session_id)
                # 精确去重：优先使用 msgId（UUID），回退到 (role, msg_type, content) 完整匹配
                existing_ids = {m.get("msg_id", "") for m in existing if m.get("msg_id")}
                existing_keys = {(m["role"], m.get("msg_type", "chat"), m["content"]) for m in existing if not m.get("msg_id")}
                sender_to_role = {"user": "user", "ai": "assistant", "system": "system"}
                for msg in messages:
                    role = sender_to_role.get(msg.get("sender", "ai"), "assistant")
                    msg_id = msg.get("msgId", "")
                    # 精确去重：有 msgId 时优先用它判断
                    if msg_id and msg_id in existing_ids:
                        continue
                    # 回退到完整内容三元组去重（兼容无 msgId 的旧消息，避免 content[:80] 截断碰撞）
                    if not msg_id:
                        key = (role, msg.get("msgType", "chat"), (msg.get("text") or ""))
                        if key in existing_keys:
                            continue
                    # 使用前端携带的真实时间戳，而非 SQL CURRENT_TIMESTAMP
                    frontend_ts = msg.get("timestamp", "")
                    if frontend_ts:
                        # 前端 ISO 格式 "2026-06-15T15:11:10.000Z" → SQLite 兼容 "2026-06-15 23:11:10"
                        try:
                            from datetime import datetime, timezone, timedelta
                            dt = datetime.fromisoformat(frontend_ts.replace("Z", "+00:00"))
                            local_dt = dt.astimezone(timezone(timedelta(hours=8)))
                            frontend_ts = local_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            frontend_ts = frontend_ts.replace("T", " ")[:19]
                    add_chat_message(session_id,
                        "user" if msg.get("sender") == "user" else "assistant",
                        msg.get("text", ""),
                        msg_type=msg.get("msgType", "chat"),
                        msg_id=msg_id,
                        timestamp=frontend_ts)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/api/sessions/new', methods=['POST'])
def new_session():
    """创建新会话，由后端统一分配 ID 和名称"""
    try:
        session_id = create_session(title=None)
        sessions = list_sessions()
        session_data = next((s for s in sessions if s["id"] == session_id), None)
        return jsonify({"success": True, "session": session_data})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

# 2. 修复 /api/sessions GET 方法（实际获取会话列表，而非返回空）
@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    try:
        sessions = list_sessions()   # 调用 memory 中已有的函数
        return jsonify(sessions)
    except Exception as e:
        print(f"[API] 获取会话列表错误: {e}")
        return jsonify([])

# 3. 修复 /api/models 路由（捕获异常）
@app.route('/api/models', methods=['GET'])
def get_models():
    try:
        available_models = get_available_models()
        if not available_models:
            return jsonify([{
                "provider": "ollama",
                "model": "deepseek-r1:7b",
                "display": "本地 Ollama"
            }])
        return jsonify(available_models)
    except Exception as e:
        print(f"[API] 加载模型列表错误: {e}")
        # 兜底返回默认模型
        return jsonify([{
            "provider": "ollama",
            "model": "deepseek-r1:7b",
            "display": "本地 Ollama"
        }])
@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session_route(session_id):
    try:
        cleanup_session_uploads(session_id)
        delete_session(session_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/api/sessions/active', methods=['GET'])
def get_active_session():
    session_id = get_active_session_id()   # 若无活跃会话会自动创建
    return jsonify({"session_id": session_id})

@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    try:
        messages = get_chat_messages(session_id)
        formatted = []
        for msg in messages:
            sender = "user" if msg["role"] == "user" else "ai"
            formatted.append({
                "sender": sender,
                "text": msg["content"],
                "msgType": msg.get("msg_type", "chat"),
                "timestamp": msg.get("created_at", ""),
                "msgId": msg.get("msg_id", "")
            })
        return jsonify({"success": True, "messages": formatted})
    except Exception as e:
        return jsonify({"success": False, "message": translate_error(str(e))}), 500

@app.route('/')
def index():
    # 因为 template_folder 已经指向 'web'，这里直接写文件名即可
    return render_template('index.html')

@app.after_request
def add_no_cache(response):
    """禁止浏览器缓存 HTML/JS 静态资源，确保前端始终拉取最新版本。"""
    if request.path == '/' or request.path.endswith('.html') or request.path.endswith('.js'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ============================================================
# 三层路由架构：闲聊匹配 → 高频固化 → 复杂度判断 → LLM
# ============================================================

# ── 第1层：闲聊匹配字典 ──
_CHAT_MATCHES: dict[str, str] = {
    "谢谢": "不客气，随时为您服务。",
    "感谢": "不客气，随时为您服务。",
    "多谢": "不客气，随时为您服务。",
    "3q": "不客气，随时为您服务。",
    "thanks": "不客气，随时为您服务。",
    "你好": "您好，我在。请问有什么可以帮您？",
    "hi": "您好，我在。请问有什么可以帮您？",
    "hello": "您好，我在。请问有什么可以帮您？",
    "hey": "您好，我在。请问有什么可以帮您？",
    "在吗": "您好，我在。请问有什么可以帮您？",
    "早上好": "早上好！新的一天，有什么需要处理的吗？",
    "早安": "早上好！新的一天，有什么需要处理的吗？",
    "晚安": "晚安，早点休息。",
    "早点休息": "晚安，早点休息。",
    "再见": "再见，随时找我。",
    "拜拜": "再见，随时找我。",
    "bye": "再见，随时找我。",
    "回头见": "再见，随时找我。",
    "辛苦了": "不辛苦，分内之事。",
    "你是谁": "我是 MyAgent，您的本地智能助手。",
    "你叫什么": "我是 MyAgent，您的本地智能助手。",
}


def _check_chat_layer(user_message: str) -> str | None:
    """第1层：闲聊匹配。命中返回预设回复，未命中返回 None。"""
    msg_lower = user_message.strip().lower()
    # 精确匹配
    for keyword, reply in _CHAT_MATCHES.items():
        if msg_lower == keyword.lower():
            return reply
    # 模糊匹配：消息包含关键词（仅限长度≤8的短消息防误判）
    if len(user_message.strip()) <= 8:
        for keyword, reply in _CHAT_MATCHES.items():
            if len(keyword) >= 2 and keyword.lower() in msg_lower:
                return reply
    return None


# ── 第2层：高频固化层 ──

def _msg_hash(user_message: str) -> str:
    """对用户消息做归一化后计算 SHA256 前 12 位，用作缓存 key。"""
    normalized = user_message.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _check_frequent_cache(user_message: str) -> tuple[bool, str | None]:
    """第2层：高频固化匹配。
    返回 (命中, SSE格式响应文本或None)。
    命中时直接调用固化工具并套用模板，不调用LLM。
    """
    cache = _load_cached_actions()
    records = cache.get("固化记录", {})
    if not records:
        return False, None

    msg_hash_val = _msg_hash(user_message)

    # 先精确 hash 匹配
    for label, record in records.items():
        if record.get("query_hash") == msg_hash_val:
            return _execute_cached_action(record, user_message)

    # hash 未命中则用相似度匹配
    msg_normalized = user_message.strip().lower()
    for label, record in records.items():
        stored_query = label.lower()
        if len(stored_query) > 0 and len(msg_normalized) > 0:
            ratio = difflib.SequenceMatcher(None, msg_normalized, stored_query).ratio()
            if ratio >= 0.85:
                return _execute_cached_action(record, user_message)

    return False, None


def _execute_cached_action(record: dict, user_message: str) -> tuple[bool, str | None]:
    """执行固化记录中的工具调用并套用模板，返回 (True, SSE响应文本)。"""
    try:
        tool_name = record["固化参数"]["tool"]
        params = record["固化参数"]["params"]
        template = record.get("固化回复模板", "{result_summary}")

        from tools import TOOL_REGISTRY
        if tool_name not in TOOL_REGISTRY:
            return False, None

        tool_fn = TOOL_REGISTRY[tool_name]["function"]
        result = tool_fn(**params)

        # 解析工具返回
        result_str = ""
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    if parsed.get("success") is False:
                        return False, None  # 工具失败不算命中
                    result_str = json.dumps(parsed, ensure_ascii=False, indent=2)
                else:
                    result_str = str(result)
            except json.JSONDecodeError:
                result_str = result
        else:
            result_str = str(result)

        # 截断过长的结果
        if len(result_str) > 2000:
            result_str = result_str[:2000] + "\n\n... (内容过长已截断)"

        reply = template.replace("{result_summary}", result_str)
        return True, reply
    except Exception:
        return False, None


def _record_success(user_message: str, tool_name: str = "", params: dict | None = None):
    """累计成功次数，达到6次则写入固化记录。
    简化处理：每次LLM成功返回即调用，匹配同一query hash累计。
    """
    cache = _load_cached_actions()
    records = cache.setdefault("固化记录", {})
    msg_hash_val = _msg_hash(user_message)

    # 查找已有记录
    existing_key = None
    for label, record in records.items():
        if record.get("query_hash") == msg_hash_val:
            existing_key = label
            break

    if existing_key:
        records[existing_key]["success_count"] = records[existing_key].get("success_count", 0) + 1
    else:
        # 新建记录，success_count=1，不固化
        cache_key = user_message.strip()[:50]
        records[cache_key] = {
            "query_hash": msg_hash_val,
            "success_count": 1,
            "固化参数": {"tool": tool_name or "", "params": params or {}},
            "固化回复模板": "{result_summary}",
        }

    _save_cached_actions(cache)
    # 注意：新建记录 success_count=1，需要再成功5次才会达到6次写入固化


# ── 第3层：复杂度判断 ──

def _check_complexity(user_message: str, model_choice: dict) -> str:
    """第3层：调用LLM判断任务复杂度，返回 '复杂' 或 '简单'。
    LLM调用失败时兜底返回 '复杂'。
    """
    judge_prompt = (
        '请判断以下用户请求的复杂度，仅回复"复杂"或"简单"：\n'
        f'用户请求：{user_message}\n\n'
        '判断标准：\n'
        '- 复杂：涉及多步骤操作、需要搜索资料、需要修改系统配置、需要跨工具协作\n'
        '- 简单：单步操作、已知知识的直接回答、文件读写等直接可执行的任务'
    )

    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = ai_concat(
            messages=messages,
            model_name=model_choice.get("model"),
            provider=model_choice.get("provider"),
            trace_id=model_choice.get("model", "complexity"),
        )
        content = (result.get("message", {}).get("content", "") if isinstance(result, dict) else "")
        content = content.strip().lower()
        if "复杂" in content:
            return "复杂"
        return "简单"
    except Exception:
        return "复杂"  # 失败兜底走复杂模式


# ── SSE 快捷响应辅助 ──
def _sse_quick_reply(text: str, session_id: str, trace_id: str, user_id: str):
    """返回一个 SSE 流式生成器，产出 step→final 事件链。"""
    import datetime as dt
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'text': '处理中...', 'status': '进行中'}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'final', 'text': text, 'timestamp': now}, ensure_ascii=False)}\n\n"

    append_session_message(session_id, "assistant", text,
                           trace_id=trace_id, user_id=user_id,
                           skip_compress=True)
    return generate()


# ============================================================
@app.route("/api/chat", methods=["POST"])
def api_chat():
    trace_id = generate_trace_id()
    user_id = get_user_id()
    try:
        data = request.json
        user_input = data.get("message", "").strip()
        session_id = data.get("session_id")
        if not session_id:
            return jsonify({"success": False, "message": "缺少 session_id"}), 400

        # 从消息末尾提取附件块，解析文件列表，再注回消息体供 LLM 感知
        cleaned_msg, attachments = _extract_attachments(user_input)
        user_input = _inject_attachment_context(cleaned_msg, attachments)

        # 存储用户消息到记忆（含事实提取、压缩检查、SQLite 写入）
        append_session_message(session_id, "user", user_input, skip_compress=False,
                               trace_id=trace_id, user_id=user_id)

        model_choice = data.get("model_choice")
        # 前端可能只传 { "display": "..." }，缺少 provider/model 等必要字段，
        # 此时必须走 get_model_choice 补齐完整配置
        if (not model_choice or not isinstance(model_choice, dict)
                or "provider" not in model_choice or "model" not in model_choice):
            model_display = data.get("model", None)
            model_id = data.get("model_id", None)
            model_choice = get_model_choice(model_display, model_id)

        print(f"[DIAG /chat] session={session_id} model_choice={model_choice} msg_len={len(user_input)}", flush=True)

        # 查找会话名称用于日志文件名
        session_name = ""
        for s in list_sessions():
            if s["id"] == session_id:
                session_name = s.get("title", "")
                break

        # ============================================================
        # 三层路由架构：闲聊匹配 → 高频固化 → 复杂度判断 → LLM
        # ============================================================

        # ── 第1层：闲聊匹配 ──
        chat_reply = _check_chat_layer(user_input)
        if chat_reply is not None:
            print(f"[ROUTE L1] 闲聊命中 → 直接返回", flush=True)
            gen = _sse_quick_reply(chat_reply, session_id, trace_id, user_id)
            return Response(stream_with_context(gen),
                            mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        # ── 第2层：高频固化 ──
        cache_hit, cache_reply = _check_frequent_cache(user_input)
        if cache_hit and cache_reply:
            print(f"[ROUTE L2] 高频固化命中 → 直接调用工具返回", flush=True)
            gen = _sse_quick_reply(cache_reply, session_id, trace_id, user_id)
            return Response(stream_with_context(gen),
                            mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        # ── 第3层：复杂度判断 ──
        complexity = _check_complexity(user_input, model_choice)
        print(f"[ROUTE L3] 复杂度判定: {complexity}", flush=True)

        # 根据复杂度选择性注入 system prompt 后缀
        planning_extra = ""
        if complexity == "复杂":
            planning_extra = "\n\n[系统指令] 这是一个复杂任务，请先规划步骤再逐步执行。"
            print(f"[ROUTE L3] 复杂任务 → 注入规划提示", flush=True)
        else:
            print(f"[ROUTE L3] 简单任务 → 直接执行", flush=True)

        return Response(
            stream_with_context(run_agent(
                user_message=user_input,
                model_choice=model_choice,
                session_id=session_id,
                trace_id=trace_id,
                user_id=user_id,
                streaming=True,
                session_name=session_name,
                tool_executor=_TOOL_EXECUTOR,
                default_model=default_model,
                confirmation_handler=wait_for_confirmation,
                planning_extra=planning_extra,
                on_success=lambda msg: _record_success(msg),
            )),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    except Exception as e:
        log_operation(trace_id, user_id, session_id if 'session_id' in locals() else "",
                      "error", str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": translate_error(str(e))}), 500


# ---------- 命令确认 API ----------
@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    """前端点击确认/取消后回调，唤醒 agent 循环中挂起的确认等待。"""
    try:
        data = request.json
        token = data.get("confirmation_token", "").strip()
        confirmed = data.get("confirmed", False)
        if not token:
            return jsonify({"success": False, "message": "缺少 confirmation_token"}), 400
        with _pending_lock:
            pending = _pending_confirmations.get(token)
        if not pending:
            return jsonify({"success": False, "message": "令牌无效或已过期"}), 404
        pending["result"]["confirmed"] = confirmed
        pending["event"].set()
        get_logger().info("命令确认", confirmation_token=token[:8], confirmed=confirmed)
        return jsonify({"success": True, "message": "已确认" if confirmed else "已取消"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ---------- 日志查询 API ----------
@app.route('/logs/view')
def log_viewer():
    return app.send_static_file('log_viewer.html')


@app.route('/api/logs/query')
def api_logs_query():
    lg = get_logger()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    level = request.args.get("level", "")
    keyword = request.args.get("keyword", "")
    session_name = request.args.get("session_name", "")
    limit = int(request.args.get("limit", 200))

    # 会话名称 → session_id 映射
    sid = None
    if session_name:
        for s in list_sessions():
            if session_name.lower() in s["title"].lower():
                sid = s["id"]
                break

    results = lg.query(
        date_from=date_from or None, date_to=date_to or None,
        level=level or None, keyword=keyword or None,
        session_id=sid, limit=min(limit, 500),
    )

    # 为每条日志附加 session_name
    if results:
        session_map = {s["id"]: s["title"] for s in list_sessions()}
        for entry in results:
            eid = (entry.get("extra") or {}).get("session_id", "")
            entry["session_name"] = session_map.get(eid, "")

    return jsonify({"success": True, "logs": results})


@app.route('/api/logs/dates')
def api_logs_available_dates():
    from logger import LOG_DIR
    dates = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("agent_*.jsonl"), reverse=True):
            dates.append(f.stem.replace("agent_", ""))
    return jsonify({"success": True, "dates": dates})


@app.route('/api/logs/llm_call')
def api_logs_llm_call():
    session_id = request.args.get("session_id", "")
    turn = int(request.args.get("turn", 0))
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400
    lg = get_logger()
    file_path = lg.get_llm_call_file(session_id, turn)
    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return jsonify({"success": True, "data": json.load(f)})
    return jsonify({"success": False, "message": "未找到该 LLM 调用记录"}), 404


@app.route('/api/sessions/<session_id>/rename', methods=['POST'])
def api_rename_session(session_id):
    data = request.json or {}
    new_title = data.get("title", "").strip()
    if not new_title:
        return jsonify({"success": False, "message": "名称不能为空"}), 400
    ok = rename_session(session_id, new_title)
    return jsonify({"success": ok, "message": "已修改" if ok else "会话不存在"})


# ---------- 模型配置 API ----------
MODELS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
_MYAGENT_DATA = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
USER_PROFILE_PATH = os.path.join(_MYAGENT_DATA, 'user_profile.json')

MODEL_PRESETS = {
    "deepseek":     {"name": "DeepSeek", "base_url": "https://api.deepseek.com",         "temperature": 0.7, "models": ["deepseek-chat", "deepseek-reasoner"]},
    "hunyuan":      {"name": "混元",      "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "temperature": 0.7, "models": ["hunyuan-turbos-latest", "hunyuan-lite"]},
    "qwen":         {"name": "通义千问",   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "temperature": 0.7, "models": ["qwen-turbo", "qwen-plus", "qwen-max"]},
    "zhipu":        {"name": "智谱GLM",   "base_url": "https://open.bigmodel.cn/api/paas/v4", "temperature": 0.7, "models": ["glm-4-flash", "glm-4-plus"]},
    "doubao":       {"name": "豆包",      "base_url": "https://ark.cn-beijing.volces.com/api/v3", "temperature": 0.7, "models": ["doubao-lite-128k", "doubao-pro-256k"]},
    "openai":       {"name": "OpenAI",    "base_url": "https://api.openai.com/v1",          "temperature": 0.7, "models": ["gpt-4o", "gpt-4o-mini"]},
    "ollama":       {"name": "Ollama 本地","base_url": "http://localhost:11434/v1",          "temperature": 0.7, "models": [], "warning": "本地模型功能可能受模型能力影响，效果远不如云端大模型"},
    "custom":       {"name": "自定义",     "base_url": "",                                   "temperature": 0.7, "models": []},
}


def _load_models_config():
    if os.path.exists(MODELS_CONFIG_PATH):
        with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"models": [], "active_model_id": ""}


def _save_models_config(config):
    with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_user_profile():
    if os.path.exists(USER_PROFILE_PATH):
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_user_profile(profile):
    with open(USER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    # 同步写入长期记忆 user.md + facts.md
    name = profile.get("name") or profile.get("nickname") or ""
    parts = []
    if name:
        parts.append(f"- 姓名: {name}")
    if profile.get("nickname") and profile.get("name") and profile["nickname"] != profile["name"]:
        parts.append(f"- 称呼偏好: {profile['nickname']}")
    if parts:
        _rewrite_md(USER_MD, "# 用户信息\n\n" + "\n".join(parts) + "\n\n# 偏好\n\n(无)\n")

    if profile.get("background"):
        existing = _read_lines(FACTS_MD)
        text = f"- [职业背景] {profile['background']}"
        if text not in existing:
            append_fact(text)
    if profile.get("hobbies"):
        existing = _read_lines(FACTS_MD)
        text = f"- [兴趣爱好] {profile['hobbies']}"
        if text not in existing:
            append_fact(text)


@app.route('/api/config/models', methods=['GET'])
def api_get_models_config():
    return jsonify({"success": True, **_load_models_config()})


@app.route('/api/config/models', methods=['POST'])
def api_save_models_config():
    try:
        data = request.json
        if not data or "models" not in data:
            return jsonify({"success": False, "message": "缺少 models 字段"}), 400
        _save_models_config(data)
        return jsonify({"success": True, "message": "配置已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/config/models/active', methods=['POST'])
def api_set_active_model():
    try:
        data = request.json
        model_id = data.get("model_id", "")
        config = _load_models_config()
        config["active_model_id"] = model_id
        _save_models_config(config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ---------- 用户信息与首次启动 API ----------
@app.route('/api/config/user_profile', methods=['GET'])
def api_get_user_profile():
    return jsonify({"success": True, "profile": _load_user_profile()})


@app.route('/api/config/user_profile', methods=['POST'])
def api_save_user_profile():
    try:
        data = request.json
        if not data or "profile" not in data:
            return jsonify({"success": False, "message": "缺少 profile 字段"}), 400
        _save_user_profile(data["profile"])
        return jsonify({"success": True, "message": "个人信息已保存"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/config/first_run', methods=['GET'])
def api_check_first_run():
    return jsonify({"first_run": not os.path.exists(USER_PROFILE_PATH)})


@app.route('/api/config/model_presets', methods=['GET'])
def api_get_model_presets():
    return jsonify({"success": True, "presets": MODEL_PRESETS})


# ---------- 启动 ----------
if __name__ == '__main__':
    ensure_session_titles()
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)