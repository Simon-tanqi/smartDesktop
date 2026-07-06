# setup_config.py — 独立运行，不依赖 llm_client（避免模块级 load_config 报错）
import os, yaml
import keyring

KEYRING_SERVICE = "MyAgent"
KEYRING_USERNAME = "api_key"
DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
CONFIG_YAML = os.path.join(DATA_DIR, 'config.yaml')

# OpenAI 兼容 API 厂商预设（base_url + 默认模型）
PROVIDER_PRESETS = {
    "2": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
    "3": {
        "name": "豆包 (火山引擎)",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-1-5-pro-256k",
    },
    "4": {
        "name": "通义千问 (DashScope)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
}


def _try_keyring_set(api_key):
    """安全地向 keyring 写入 API Key。成功返回 True，失败返回 False。"""
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)

    print("选择后端类型:")
    print("  1. 本地 Ollama")
    print("  2. DeepSeek API")
    print("  3. 豆包 (火山引擎)")
    print("  4. 通义千问 (DashScope)")
    print("  5. 其他 OpenAI 兼容 API")
    choice = input("输入编号 (1-5): ").strip()

    yaml_config = {}
    api_key = None

    if choice == "1":
        model = input("模型名称 (默认 deepseek-r1:7b): ").strip() or "deepseek-r1:7b"
        yaml_config = {"backend": "ollama", "model": model}
    elif choice in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[choice]
        api_key = input(f"请输入 {preset['name']} API Key: ").strip()
        model = input(f"模型名称 (默认 {preset['default_model']}): ").strip() or preset["default_model"]
        yaml_config = {
            "backend": preset["name"],
            "model": model,
            "base_url": preset["base_url"],
        }
    elif choice == "5":
        backend = input("后端标识名 (如 openai / groq): ").strip() or "custom"
        base_url = input("API Base URL: ").strip()
        api_key = input("API Key: ").strip()
        model = input("模型名称: ").strip()
        yaml_config = {"backend": backend, "model": model, "base_url": base_url}
    else:
        print("无效选择")
        exit(1)

    # ── 保存 API Key 到系统凭据管理器 ──
    if api_key:
        if _try_keyring_set(api_key):
            print("✅ API Key 已安全存储至系统凭据管理器（Windows 凭据管理器）")
        else:
            print("⚠️ 系统凭据管理器不可用，API Key 将明文写入配置文件。")
            yaml_config["api_key"] = api_key

    # ── 保存非敏感配置到 YAML ──
    with open(CONFIG_YAML, 'w', encoding='utf-8') as f:
        yaml.dump(yaml_config, f, allow_unicode=True, default_flow_style=False)
    print(f"✅ 配置已保存至 {CONFIG_YAML}")
