# api.py —— 使用本地 ai_reply 模块，不再单独调用外部 API
import sys, os, json
from flask import Flask, request, jsonify, send_from_directory

# ---------- 1. 路径设置（确保能导入 agent 包） ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(BASE_DIR, '..', 'agent')      # 根据实际结构调整
AGENT_DIR = os.path.abspath(AGENT_DIR)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

from ai_reply import ai_concat, get_available_models, load_config

app = Flask(__name__)

# 截图静态文件目录
SCREENSHOTS_DIR = os.path.join(os.path.dirname(BASE_DIR), 'workspace', 'screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ---------- 2. 工具函数：将内部响应转为 OpenAI 格式 ----------
def to_openai_format(internal_response):
    """
    ai_concat 返回格式示例: {"message": {"content": "这是回复"}}
    转换为 OpenAI 格式: {"choices": [{"message": {"content": "这是回复"}}], ...}
    """
    if not internal_response:
        return {"choices": [{"message": {"content": "AI 请求失败"}}]}
    content = internal_response.get("message", {}).get("content", "")
    return {
        "id": "chatcmpl-" + os.urandom(4).hex(),
        "object": "chat.completion",
        "created": int(__import__('time').time()),
        "model": "local-agent",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }

# ---------- 3. 聊天补全接口 ----------
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    data = request.json
    if not data or "messages" not in data:
        return jsonify({"error": {"message": "messages is required", "type": "invalid_request_error"}}), 400

    messages = data["messages"]
    model = data.get("model", None)   # 前端可传模型名，如 "deepseek-chat"
    stream = data.get("stream", False)

    if stream:
        return jsonify({"error": {"message": "Stream not supported", "type": "not_supported"}}), 501

    # 调用本地 ai_reply 模块
    try:
        raw_response = ai_concat(messages, model_name=model)
        openai_response = to_openai_format(raw_response)
        return jsonify(openai_response)
    except Exception as e:
        return jsonify({"error": {"message": str(e), "type": "api_error"}}), 500

# ---------- 4. 模型列表接口（返回本地支持的模型） ----------
@app.route("/v1/models", methods=["GET"])
def list_models():
    available = get_available_models()   # 返回 [{"display": ..., "provider": ..., "model": ...}, ...]
    models = []
    for m in available:
        models.append({
            "id": m["model"],
            "object": "model",
            "owned_by": m["provider"]
        })
    return jsonify({"object": "list", "data": models})

# ---------- 5. 健康检查 ----------
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "local-agent-proxy"})

# ---------- 6. 截图静态文件服务 ----------
@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOTS_DIR, filename)

if __name__ == "__main__":
    port = 5000
    print(f"本地 Agent API 已启动，端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=True)