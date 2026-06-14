from llm_client import save_config

if __name__ == "__main__":
    print("选择后端类型:")
    print("1. 本地 Ollama")
    print("2. DeepSeek API")
    choice = input("输入编号 (1/2): ").strip()

    if choice == "1":
        save_config(backend="ollama", api_key=None, model="deepseek-r1:7b")
    elif choice == "2":
        api_key = input("请输入 DeepSeek API Key: ").strip()
        model = input("模型名称 (默认 deepseek-chat): ").strip() or "deepseek-chat"
        save_config(backend="deepseek", api_key=api_key, model=model)
    else:
        print("无效选择")