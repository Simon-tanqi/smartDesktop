# setup_config.py — 独立运行，不依赖 llm_client（避免模块级 load_config 报错）
import os, json
from cryptography.fernet import Fernet

KEY_FILE = ".myagent_key"
CONFIG_FILE = "config.enc"

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


def ensure_key():
    """读取或生成密钥文件。生成时直接写入 .myagent_key。"""
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'r', encoding='utf-8') as f:
            key = f.read().strip()
        if key:
            try:
                return Fernet(key.encode())
            except Exception:
                print("密钥文件格式无效，将重新生成。")
    key = Fernet.generate_key().decode()
    with open(KEY_FILE, 'w', encoding='utf-8') as f:
        f.write(key)
    print("=" * 60)
    print("已生成加密密钥并保存至 .myagent_key（已加入 .gitignore）")
    print("=" * 60)
    return Fernet(key.encode())


if __name__ == "__main__":
    f = ensure_key()

    print("选择后端类型:")
    print("  1. 本地 Ollama")
    print("  2. DeepSeek API")
    print("  3. 豆包 (火山引擎)")
    print("  4. 通义千问 (DashScope)")
    print("  5. 其他 OpenAI 兼容 API")
    choice = input("输入编号 (1-5): ").strip()

    if choice == "1":
        model = input("模型名称 (默认 deepseek-r1:7b): ").strip() or "deepseek-r1:7b"
        config = {"backend": "ollama", "model": model}
    elif choice in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[choice]
        api_key = input(f"请输入 {preset['name']} API Key: ").strip()
        model = input(f"模型名称 (默认 {preset['default_model']}): ").strip() or preset["default_model"]
        config = {
            "backend": preset["name"],
            "api_key": api_key,
            "model": model,
            "base_url": preset["base_url"],
        }
    elif choice == "5":
        backend = input("后端标识名 (如 openai / groq): ").strip() or "custom"
        base_url = input("API Base URL: ").strip()
        api_key = input("API Key: ").strip()
        model = input("模型名称: ").strip()
        config = {"backend": backend, "api_key": api_key, "model": model, "base_url": base_url}
    else:
        print("无效选择")
        exit(1)

    plain = json.dumps(config, ensure_ascii=False)
    encrypted = f.encrypt(plain.encode()).decode()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f_out:
        f_out.write(encrypted)
    print(f"✅ 配置已加密保存至 {CONFIG_FILE}")
