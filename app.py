from flask import Flask, render_template, request, jsonify
import os, sys, json, uuid
from pathlib import Path


current_file = Path(__file__).resolve()
ROOT_DIR = current_file.parent.parent
sys.path.insert(0, str(ROOT_DIR))

# 现在下面的导入就不会报错了
from ai_reply import ai_concat, parse_ai_json, normalize_path_component, get_available_models
from intent_router import classify_intent, extract_params, INTENT_REGISTRY, SKILL_PROMPTS
from tools import TOOL_REGISTRY, build_dynamic_system_prompt
from memory import (
    load_long_term, get_recent_compressed, log_session_turn,
    create_session, get_session_messages, append_session_message,
    save_session_messages, update_last_turn,
    set_active_session, get_active_session_id, generate_session_id
)

app = Flask(__name__)
# ---------- 工作空间配置 ----------
workspace_path = os.path.dirname(os.path.abspath(__file__))

# ---------- 模型选择（可从前端传参） ----------
available_models = get_available_models()
default_model = available_models[0] if available_models else {
    "provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"
}

def get_model_choice(display_name=None):
    for m in available_models:
        if m["display"] == display_name:
            return m
    return default_model

# ---------- 核心处理（与 main.py 中的 run_agent 相同） ----------
def run_agent_with_steps(user_message, model_choice, session_id=None, continue_turns=0):
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    def llm(messages, model_name=None, provider=None):
        actual_model = model_name if model_name else model_choice["model"]
        actual_provider = provider if provider else model_choice["provider"]

        print(f"[DEBUG] 调用 LLM - provider: {actual_provider}, model: {actual_model}")

        # 强制将 provider 作为 model_name 的前缀（如果 ai_concat 依赖这种格式）
        # 或者直接原样传递
        # 尝试这种写法，确保参数名完全匹配：
        return ai_concat(
            messages=messages,
            model_name=actual_model,
            provider=actual_provider
        )

    # 第一阶段
    intent_type = classify_intent(user_message, llm, model_name=model_name, provider=provider)
    params = extract_params(user_message, intent_type, llm, model_name=model_name, provider=provider) or {}

    # 记忆
    long_term = load_long_term()
    memory_str = ""
    if long_term.get("name"):
        memory_str += f"用户姓名：{long_term['name']}。"
    if long_term.get("hobbies"):
        memory_str += f"爱好：{', '.join(long_term['hobbies'])}。"
    if long_term.get("long_term_goal"):
        memory_str += f"长期目标：{long_term['long_term_goal']}。"
    if memory_str:
        memory_str = "【长期记忆】\n" + memory_str + "\n"

    compressed_str = get_recent_compressed(3)
    if compressed_str:
        compressed_str = compressed_str + "\n"

    planning_context = (
        f"{memory_str}"
        f"{compressed_str}"
        f"原始需求：{user_message}\n\n"
        f"意图类型：{intent_type} ({INTENT_REGISTRY.get(intent_type, '')})\n"
        f"提取的参数：{json.dumps(params, ensure_ascii=False) if params else '无'}\n\n"
        f"【重要】请完整理解用户的最终目标，不要只执行第一步。"
    )

    base_system = build_dynamic_system_prompt()
    skill_prompt = SKILL_PROMPTS.get(intent_type, "")
    full_system = base_system + (skill_prompt or "")

    history_messages = get_session_messages(session_id)
    cleaned_history = [m for m in history_messages if m["role"] != "system"]

    messages = [
        {"role": "system", "content": full_system},
        *cleaned_history,
        {"role": "user", "content": planning_context}
    ]

    append_session_message(session_id, "user", user_message)
    log_session_turn("user", user_message)

    collected_steps = []
    MAX_TURNS = 15
    start_turn = continue_turns

    for turn in range(start_turn, MAX_TURNS):
        if turn >= MAX_TURNS - 2:
            messages.append({"role": "system", "content": "你即将达到思考轮次上限，请尽快收尾。"})

        res = ai_concat(messages, model_name=model_name, provider=provider)
        if not res:
            return collected_steps, "AI 请求失败"

        ai_content = parse_ai_json(res["message"]["content"])

        # progress
        if ai_content and isinstance(ai_content, dict) and "progress" in ai_content:
            progress_msg = ai_content["progress"]
            collected_steps.append({"step": progress_msg, "status": "进行中"})
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            messages.append({"role": "assistant", "content": json.dumps(ai_content, ensure_ascii=False)})
            continue

        # final_message
        if ai_content and isinstance(ai_content, dict) and "final_message" in ai_content:
            final_msg = ai_content["final_message"]
            collected_steps.append({"step": "完成", "status": "完成", "detail": final_msg})
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            return collected_steps, final_msg

        # 无 tool_name 或非字典，当作最终回复
        if not ai_content or not isinstance(ai_content, dict) or "tool_name" not in ai_content:
            fallback = res["message"]["content"]
            collected_steps.append({"step": "AI 回复", "status": "完成", "detail": fallback})
            append_session_message(session_id, "assistant", fallback)
            return collected_steps, fallback

        tool_name = ai_content["tool_name"]
        arguments = ai_content.get("arguments", {})

        if tool_name == "finished":
            append_session_message(session_id, "assistant", json.dumps(ai_content, ensure_ascii=False))
            collected_steps.append({"step": "任务结束", "status": "完成"})
            return collected_steps, "任务已完成"

        # 执行工具
        if tool_name in TOOL_REGISTRY:
            path_params = TOOL_REGISTRY[tool_name].get("path_params", [])
            for p in path_params:
                if p in arguments:
                    arguments[p] = normalize_path_component(arguments[p])
            try:
                result = TOOL_REGISTRY[tool_name]["function"](**arguments)
                result_str = str(result)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "... (已截断)"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "完成", "detail": result_str[:200]})
            except Exception as e:
                result_str = f"工具执行错误: {str(e)}"
                collected_steps.append({"step": f"执行 {tool_name}", "status": "错误", "detail": str(e)})
        else:
            result_str = f"工具 '{tool_name}' 不存在"
            collected_steps.append({"step": f"未知工具 {tool_name}", "status": "错误"})

        tool_call_msg = json.dumps(ai_content, ensure_ascii=False)
        tool_result_msg = f"工具 {tool_name} 返回：{result_str}"
        messages.append({"role": "assistant", "content": tool_call_msg})
        messages.append({"role": "user", "content": tool_result_msg})

        append_session_message(session_id, "assistant", tool_call_msg)
        append_session_message(session_id, "user", tool_result_msg)
        save_session_messages(session_id, messages)
        update_last_turn(session_id, turn)

    # 超时保存
    update_last_turn(session_id, MAX_TURNS)
    save_session_messages(session_id, messages)
    return collected_steps, "任务未完成，已保存进度。可以继续。"

# ---------- Flask 路由 ----------
@app.route('/')
def index():
    return render_template('index.html')


@app.route("/api/chat", methods=["POST"])
def api_chat():
    print("原始请求体:", request.json)
    try:
        data = request.json
        user_input = data.get("message", "").strip()
        session_id = data.get("session_id")

        # 获取前端发送的完整模型配置对象（包含 provider, model, display）
        model_choice = data.get("model_choice")

        # 如果前端没有发送 model_choice（兼容旧版本），则尝试从 model 字段获取 display 字符串再匹配
        if not model_choice or not isinstance(model_choice, dict):
            model_display = data.get("model", None)
            model_choice = get_model_choice(model_display)

        # 可选：打印日志确认实际使用的模型
        print(f"[api_chat] 实际使用的模型配置: {model_choice}")

        if not user_input:
            return jsonify({"success": False, "message": "消息不能为空"})

        steps, final_msg = run_agent_with_steps(
            user_message=user_input,
            model_choice=model_choice,  # 直接传递配置对象
            session_id=session_id
        )

        return jsonify({
            "success": True,
            "steps": steps,
            "message": final_msg
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/models', methods=['GET'])
def get_models():
    """
    返回可用的模型列表供前端选择
    """
    # 假设 available_models 是你已经在 app.py 顶部定义好的全局变量
    # 如果它是空的，使用你提到的默认值
    if not available_models:
        return jsonify([{
            "provider": "ollama",
            "model": "deepseek-r1:7b",
            "display": "本地 Ollama"
        }])

    # 将 available_models 转换为 JSON 格式返回
    return jsonify(available_models)


# ---------- 启动 ----------
if __name__ == '__main__':
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)