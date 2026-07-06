import json
import hashlib
import difflib
import tempfile
import os
from tools import TOOL_REGISTRY
from llm_client import ai_concat
from config.settings import DATA_DIR

_CACHED_ACTIONS_PATH = os.path.join(DATA_DIR, "cached_actions.json")
os.makedirs(DATA_DIR, exist_ok=True)


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


def check_chat_layer(user_message: str, context: dict = None) -> str | None:
    """第1层：闲聊匹配。命中返回预设回复，未命中返回 None。
    
    增强版：支持上下文感知和个性化回复
    """
    import time
    
    msg_lower = user_message.strip().lower()
    
    # 精确匹配优先
    if msg_lower in _CHAT_MATCHES:
        reply = _CHAT_MATCHES[msg_lower]
        
        # 个性化处理
        if msg_lower in ['你好', 'hello', 'hi', 'hey'] and context:
            return _enhance_greeting_with_context(reply, context)
        
        return reply
    
    # 短消息模糊匹配
    if len(user_message.strip()) <= 8:
        for keyword, reply in _CHAT_MATCHES.items():
            if len(keyword) >= 2 and keyword.lower() in msg_lower:
                return reply
                
    # 时间感知的问候
    if any(word in msg_lower for word in ['早上', '早安']):
        return _get_time_enhanced_greeting()
    
    return None


def _enhance_greeting_with_context(base_reply: str, context: dict) -> str:
    """根据上下文增强问候语"""
    import time
    from memory import get_chat_messages
    
    try:
        # 获取用户名称信息
        session_id = context.get('session_id', '')
        if session_id:
            messages = get_chat_messages(session_id)
            user_name = _extract_user_name(messages)
            if user_name:
                hour = time.localtime().tm_hour
                if 5 <= hour < 12:
                    time_word = "早上好"
                elif 12 <= hour < 18:
                    time_word = "下午好"
                else:
                    time_word = "晚上好"
                return f"{time_word}，{user_name}！{base_reply}"
    except Exception:
        pass
    
    return base_reply


def _extract_user_name(messages: list) -> str | None:
    """从消息历史中提取用户名称"""
    for msg in reversed(messages[-10:]):  # 检查最近10条消息
        content = msg.get('content', '').lower()
        if '我叫' in content:
            import re
            match = re.search(r'我叫(.+?)(?：|，|。|！|\s|$)', content)
            if match:
                return match.group(1).strip()
    return None


def _get_time_enhanced_greeting() -> str:
    """获取时间增强的问候语"""
    import time
    hour = time.localtime().tm_hour
    
    if 5 <= hour < 9:
        return "早上好！新的一天开始了，有什么需要处理的吗？"
    elif 9 <= hour < 12:
        return "上午好！新的一天，有什么需要处理的吗？"
    elif 12 <= hour < 14:
        return "中午好！有什么需要处理的吗？"
    else:
        return "早上好！新的一天，有什么需要处理的吗？"


# ── 第2层：高频固化层 ──

def _msg_hash(user_message: str) -> str:
    normalized = user_message.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _load_cached_actions() -> dict:
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
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                      dir=DATA_DIR, delete=False, suffix=".tmp") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        tmpname = tf.name
    os.replace(tmpname, _CACHED_ACTIONS_PATH)


def check_frequent_cache(user_message: str) -> tuple[bool, str | None]:
    """第2层：高频固化匹配。
    返回 (命中, SSE格式响应文本或None)。
    命中时直接调用固化工具并套用模板，不调用LLM。
    暂时禁用：返回 False, None 让请求直接走 LLM 路线
    """
    return False, None


def _execute_cached_action(record: dict, user_message: str) -> tuple[bool, str | None]:
    """执行固化记录中的工具调用并套用模板，返回 (True, SSE响应文本)。"""
    try:
        tool_name = record["固化参数"]["tool"]
        params = record["固化参数"]["params"]
        template = record.get("固化回复模板", "{result_summary}")

        if tool_name not in TOOL_REGISTRY:
            return False, None

        tool_fn = TOOL_REGISTRY[tool_name]["function"]
        result = tool_fn(**params)

        result_str = ""
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict):
                    if parsed.get("success") is False:
                        return False, None
                    result_str = json.dumps(parsed, ensure_ascii=False, indent=2)
                else:
                    result_str = str(result)
            except json.JSONDecodeError:
                result_str = result
        else:
            result_str = str(result)

        if len(result_str) > 2000:
            result_str = result_str[:2000] + "\n\n... (内容过长已截断)"

        reply = template.replace("{result_summary}", result_str)
        return True, reply
    except Exception:
        return False, None


def record_success(user_message: str, tool_name: str = "", params: dict | None = None):
    """累计成功次数，达到6次则写入固化记录。

    每次 LLM 成功完成工具调用后触发，将实际的 tool_name 和 arguments
    写入「固化参数」字段，供 _execute_cached_action 直接复用。
    如果 tool_name 为空（纯文本回复 / general_chat），跳过不记录。
    """
    if not tool_name:
        return

    cache = _load_cached_actions()
    records = cache.setdefault("固化记录", {})
    msg_hash_val = _msg_hash(user_message)

    existing_key = None
    for label, record in records.items():
        if record.get("query_hash") == msg_hash_val:
            existing_key = label
            break

    if existing_key:
        rec = records[existing_key]
        rec["success_count"] = rec.get("success_count", 0) + 1
        rec["固化参数"] = {"tool": tool_name, "params": params or {}}
    else:
        cache_key = user_message.strip()[:50]
        records[cache_key] = {
            "query_hash": msg_hash_val,
            "success_count": 1,
            "固化参数": {"tool": tool_name, "params": params or {}},
            "固化回复模板": "{result_summary}",
        }

    _save_cached_actions(cache)


# ── 第3层：复杂度判断 ──

def check_complexity(user_message: str, model_choice: dict, context: dict = None) -> str:
    """第3层：调用LLM判断任务复杂度，返回 '复杂' 或 '简单'。
    增强版：多维度规则判断 + 智能缓存 + 域感知
    """
    import hashlib
    import time
    
    # 缓存键生成
    cache_key = hashlib.md5(f"{user_message}_{model_choice.get('model', '')}".encode()).hexdigest()
    
    # 检查缓存
    if hasattr(check_complexity, '_cache') and cache_key in check_complexity._cache:
        cached_result = check_complexity._cache[cache_key]
        if time.time() - cached_result['timestamp'] < 300:  # 5分钟缓存
            return cached_result['result']
    
    msg = user_message.strip()
    
    # 增强的规则判断 - 第一层：长度和关键词
    complexity_score = 0.0
    
    # 任务指示词检测
    COMPLEX_INDICATORS = {
        "分析": 0.8, "统计": 0.7, "对比": 0.6, "总结": 0.6, "报告": 0.8,
        "生成": 0.7, "批量": 0.6, "替换": 0.5, "重构": 0.8, "安装": 0.7,
        "卸载": 0.7, "配置": 0.6, "修复": 0.7, "诊断": 0.8, "排查": 0.7,
        "优化": 0.6, "迁移": 0.8, "转换": 0.5, "提取": 0.6, "同步": 0.6,
        "架构": 0.9, "设计": 0.8, "规划": 0.7, "策略": 0.7, "方案": 0.6
    }
    
    for indicator, score in COMPLEX_INDICATORS.items():
        if indicator in msg:
            complexity_score = max(complexity_score, score)
    
    # 长度和结构分析
    if len(msg) > 50:  # 较长消息更可能复杂
        complexity_score += 0.2
    elif len(msg) < 10:  # 很短的消息除非有特殊关键词
        if complexity_score == 0:
            complexity_score = 0.1
    
    # 疑问类型分析
    if msg.endswith(("?", "？")):
        ACTION_VERBS = ["帮我", "请", "搜索", "下载", "创建", "删除", "修改", "执行", "运行"]
        if any(verb in msg for verb in ACTION_VERBS):
            complexity_score += 0.1
        else:
            # 更多是咨询类问题，相对简单
            complexity_score = max(0, complexity_score - 0.1)
    
    # 技术术语检测
    TECH_TERMS = ["API", "数据库", "服务器", "框架", "算法", "架构", "部署", "容器", "微服务"]
    if any(term in msg for term in TECH_TERMS):
        complexity_score += 0.3
    
    # 多任务指示
    MULTI_TASK_INDICATORS = ["然后", "接着", "同时", "另外", "还有", "并且", "最后"]
    if any(indicator in msg for indicator in MULTI_TASK_INDICATORS):
        complexity_score += 0.4
    
    # 时间敏感性
    TIME_SENSITIVE = ["紧急", "马上", "立刻", "尽快", "今天", "今天"]
    if any(word in msg for word in TIME_SENSITIVE):
        complexity_score += 0.2
    
    # 基于域信息的复杂度调整
    if context and 'detected_domains' in context:
        high_complexity_domains = ['system_high_risk', 'excel_analyze', 'code_dev', 'architect']
        detected_domains = context['detected_domains']
        if any(domain in high_complexity_domains for domain in detected_domains):
            complexity_score += 0.3
    
    # 快速判断：如果规则判断已经很明确，直接返回
    if complexity_score >= 0.7:
        result = "复杂"
    elif complexity_score <= 0.3:
        result = "简单"
    else:
        # 模糊情况：使用LLM进行精细判断
        result = _llm_complexity_judgment(user_message, model_choice, complexity_score)
    
    # 缓存结果
    if not hasattr(check_complexity, '_cache'):
        check_complexity._cache = {}
    check_complexity._cache[cache_key] = {
        'result': result,
        'timestamp': time.time(),
        'score': complexity_score
    }
    
    return result


def _llm_complexity_judgment(user_message: str, model_choice: dict, base_score: float) -> str:
    """使用LLM进行复杂度判断"""
    judge_prompt = (
        f'请判断以下用户请求的复杂度。当前基础评分为：{base_score:.1f}（0-1，越高越复杂）\n\n'
        f'用户请求：{user_message}\n\n'
        f'请在考虑基础评分的情况下，综合考虑以下因素：\n'
        f'1. 是否需要多步骤协作完成\n'
        f'2. 是否需要搜索外部信息\n'
        f'3. 是否涉及系统配置修改\n'
        f'4. 是否有技术复杂性\n'
        f'5. 是否需要多个工具配合\n\n'
        f'回复格式：仅回复"复杂"或"简单"'
    )

    messages = [{"role": "user", "content": judge_prompt}]
    try:
        result = ai_concat(
            messages=messages,
            model_name=model_choice.get("model"),
            provider=model_choice.get("provider"),
            trace_id=model_choice.get("model", "complexity"),
        )
        
        if result and isinstance(result, dict):
            content = result.get("message", {}).get("content", "")
            content = content.strip().lower()
            if "复杂" in content:
                return "复杂"
        
        return "简单"
    except Exception:
        # LLM调用失败时，基于基础评分决定
        return "复杂" if base_score > 0.5 else "简单"


# 初始化缓存清理定时器
def _cleanup_complexity_cache():
    """定期清理复杂度缓存"""
    if hasattr(check_complexity, '_cache'):
        current_time = time.time()
        expired_keys = []
        for key, data in check_complexity._cache.items():
            if current_time - data['timestamp'] > 1800:  # 30分钟后过期
                expired_keys.append(key)
        
        for key in expired_keys:
            del check_complexity._cache[key]
