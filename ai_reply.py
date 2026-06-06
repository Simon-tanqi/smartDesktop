import os
import json
import base64
import requests
import re

# ================== 配置加解密（简单对称加密，仅供演示） ==================
# 生产环境建议使用 `cryptography` 库的 Fernet
CONFIG_FILE = "config.enc"
ENCODING = "utf-8"

def _generate_key():
    """生成一个简单的密钥（实际应使用更安全的随机密钥）"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# 硬编码的本地密钥（仅用于演示，实际应从环境变量或外部安全存储读取）
SECRET_KEY = "Z2VuZXJhdGUtYS1yYW5kb20tc2VjcmV0LWtleS0xMjM0NTY3OA=="


def _simple_encrypt(plain_text: str, key: str) -> str:
    """简单的异或+base64加密（不安全，仅用于隐藏明文字符串）"""
    key_bytes = key.encode(ENCODING)
    plain_bytes = plain_text.encode(ENCODING)
    result = bytearray()
    for i, b in enumerate(plain_bytes):
        result.append(b ^ key_bytes[i % len(key_bytes)])
    return base64.urlsafe_b64encode(result).decode()


def _simple_decrypt(encrypted_text: str, key: str) -> str:
    """对应解密"""
    key_bytes = key.encode(ENCODING)
    data = base64.urlsafe_b64decode(encrypted_text.encode(ENCODING))
    result = bytearray()
    for i, b in enumerate(data):
        result.append(b ^ key_bytes[i % len(key_bytes)])
    return result.decode(ENCODING)


def load_config():
    """从加密配置文件读取配置，若不存在则返回默认（Ollama本地）"""
    if not os.path.exists(CONFIG_FILE):
        print("⚠️ 未找到加密配置文件，默认使用本地 Ollama。")
        return {"backend": "ollama", "model": "deepseek-r1:7b"}

    try:
        with open(CONFIG_FILE, 'r', encoding=ENCODING) as f:
            encrypted = f.read()
        decrypted = _simple_decrypt(encrypted, SECRET_KEY)
        config = json.loads(decrypted)
        return config
    except Exception as e:
        print(f"❌ 配置文件解密失败: {e}，回退到本地 Ollama。")
        return {"backend": "ollama", "model": "deepseek-r1:7b"}


def save_config(backend, api_key, model="deepseek-chat", base_url="https://api.deepseek.com"):
    """生成加密配置文件"""
    config = {
        "backend": backend,
        "model": model,
        "base_url": base_url
    }
    if backend == "deepseek":
        config["api_key"] = api_key
    plain = json.dumps(config, ensure_ascii=False)
    encrypted = _simple_encrypt(plain, SECRET_KEY)
    with open(CONFIG_FILE, 'w', encoding=ENCODING) as f:
        f.write(encrypted)
    print(f"✅ 配置已保存至 {CONFIG_FILE}")


# ================== 通用 AI 调用接口 ==================
# 运行时加载配置
AI_CONFIG = load_config()


# 修改原有的 ai_concat，增加 provider 参数以覆盖后端
def ai_concat(messages, model_name=None, provider=None):
    """
    统一的 AI 调用入口。
    provider: 可选 "ollama" 或 "deepseek"，若不传则使用配置文件的后端。
    model_name: 可选模型名，若不传则使用配置或默认值。
    """
    backend = provider or AI_CONFIG.get("backend", "ollama")

    if backend == "ollama":
        return _ollama_call(messages, model_name or AI_CONFIG.get("model", "deepseek-r1:7b"))
    elif backend == "deepseek":
        return _deepseek_call(messages,
                              model_name or AI_CONFIG.get("model", "deepseek-chat"),
                              AI_CONFIG.get("api_key", ""),
                              AI_CONFIG.get("base_url", "https://api.deepseek.com"))
    else:
        print(f"❌ 未知后端: {backend}")
        return None

def _ollama_call(messages, model):
    """原有的 Ollama 调用逻辑"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0}
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Ollama 请求失败: {e}")
        return None


def _deepseek_call(messages, model, api_key, base_url):
    """DeepSeek API 调用（兼容 OpenAI 格式）"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False
    }
    try:
        response = requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        # 转换为 Ollama 风格的返回格式，保持兼容
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return {"message": {"content": content}}
    except Exception as e:
        print(f"❌ DeepSeek API 请求失败: {e}")
        return None


# ================== 保留原有的工具函数 ==================
def parse_ai_json(content):
    from json_repair import loads as repair_loads
    try:
        clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', clean_content)
        if json_match:
            clean_content = json_match.group(1).strip()
        return repair_loads(clean_content)
    except Exception as e:
        print(f"❌ JSON 解析异常: {e}")
        return None


def normalize_path_component(path_str):
    path_str = re.sub(r'([A-Za-z])\s*盘', r'\1:', path_str)
    path_str = path_str.replace('/', '\\')
    path_str = re.sub(r'^([A-Za-z]:)(?!\\)', r'\1\\', path_str)
    return os.path.normpath(path_str)

# 新增：获取可用模型列表（供 GUI 使用）
def get_available_models():
    """
    返回可用模型列表，格式为：
    [
        {"display": "本地 Ollama (deepseek-r1:7b)", "provider": "ollama", "model": "deepseek-r1:7b"},
        {"display": "DeepSeek (deepseek-chat)", "provider": "deepseek", "model": "deepseek-chat"}
    ]
    你也可以从配置文件动态读取，这里为了简单先写死。
    """
    # 可以读取配置中的信息，但为了稳定，先提供默认两项
    models = [
        {"display": "本地 Ollama (deepseek-r1:7b)", "provider": "ollama", "model": "deepseek-r1:7b"},
        {"display": "DeepSeek (deepseek-chat)", "provider": "deepseek", "model": "deepseek-chat"},
    ]
    # 如果配置文件中是 deepseek，则可调整默认显示（也可以动态生成）
    return models