import json
import PySimpleGUI as sg
from ai_reply import ai_concat, parse_ai_json, normalize_path_component, get_available_models
from intent_router import classify_intent, extract_params, INTENT_REGISTRY
from tools import TOOL_REGISTRY, build_dynamic_system_prompt
import sys
import inspect
from tools_log import log_execution  # 直接导入装饰器

def auto_log_all_functions(module):
    """自动给模块内所有函数添加日志"""
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            # 正确写法：log_execution()(obj)
            setattr(module, name, log_execution()(obj))

# 自动给当前模块（main.py）所有函数加日志
auto_log_all_functions(sys.modules[__name__])

def run_agent(user_input: str, model_choice: dict):
    """
    model_choice 示例: {"provider": "ollama", "model": "deepseek-r1:7b"}
    """
    provider = model_choice["provider"]
    model_name = model_choice["model"]

    # 封装一个局部调用，方便传递额外参数
    def llm(messages, model_name=None, provider=None):
        # 优先使用传入的，否则用外层变量
        actual_model = model_name if model_name else model_choice["model"]
        actual_provider = provider if provider else model_choice["provider"]
        return ai_concat(messages, model_name=actual_model, provider=actual_provider)


    # ========== 第一阶段 ==========
    intent_type = classify_intent(user_input, llm, model_name=model_name, provider=provider)
    print(f"[系统] 意图分类: {intent_type}")

    if intent_type == "general_chat":
        return {"success": True, "message": "通用对话功能暂未实现，目前仅支持文件操作等。"}

    params = extract_params(user_input, intent_type, llm, model_name=model_name, provider=provider)
    print(f"[系统] 参数提取: {params}")

    # ========== 第二阶段 ==========
    system_prompt = build_dynamic_system_prompt()

    planning_context = (
        f"原始需求：{user_input}\n\n"
        f"意图类型：{intent_type} ({INTENT_REGISTRY.get(intent_type, '')})\n"
        f"提取的参数：{json.dumps(params, ensure_ascii=False) if params else '无'}\n\n"
        f"请根据以上信息，规划工具调用并输出 JSON。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": planning_context}
    ]

    MAX_TURNS = 5
    for turn in range(MAX_TURNS):
        print(f"\n[第 {turn + 1} 轮思考]")
        # 调用时继续使用相同的 provider 和 model_name
        res = ai_concat(messages, model_name=model_name, provider=provider)
        if not res:
            return {"success": False, "message": "AI 请求失败"}

        ai_content = parse_ai_json(res["message"]["content"])
        print(ai_content)

        if not ai_content or not isinstance(ai_content, dict):
            return {"success": True, "message": str(ai_content) if ai_content else "任务完成"}
        if "tool_name" not in ai_content:
            return {"success": True, "message": json.dumps(ai_content, ensure_ascii=False)}
        if ai_content["tool_name"] == "finished":
            return {"success": True, "message": "任务已完成"}

        tool_name = ai_content["tool_name"]
        arguments = ai_content["arguments"]

        if tool_name in TOOL_REGISTRY:
            print(f"[系统] 调用工具: {tool_name}, 参数: {arguments}")

            path_params = TOOL_REGISTRY[tool_name].get("path_params", [])
            for param in path_params:
                if param in arguments:
                    arguments[param] = normalize_path_component(arguments[param])

            try:
                result = TOOL_REGISTRY[tool_name]["function"](**arguments)
                print(f"结果: {result}")
            except Exception as e:
                result = f"工具执行错误: {str(e)}"
        else:
            result = f"工具 '{tool_name}' 不存在"

        messages.append({"role": "assistant", "content": json.dumps(ai_content, ensure_ascii=False)})
        messages.append({"role": "user",
                         "content": f"工具 {tool_name} 返回：{json.dumps(result, ensure_ascii=False)}\n请决定下一步。"})

    return {"success": False, "message": "Agent 思考轮数达到上限"}


# ==================== GUI ====================
sg.theme('DarkAmber')

# 获取模型列表，构造下拉框选项
available_models = get_available_models()
display_list = [m["display"] for m in available_models]
default_display = display_list[0] if display_list else ""

layout = [
    [sg.Text('选择模型:'), sg.Combo(display_list, default_value=default_display, key='-MODEL-', readonly=True)],
    [sg.Text('请输入指令：')],
    [sg.Input(key='-INPUT-')],
    [sg.Button('发送'), sg.Button('退出')]
]

window = sg.Window('通用 Agent', layout)

while True:
    event, values = window.read()
    if event in (sg.WIN_CLOSED, '退出'):
        break
    if event == '发送':
        user_input = values['-INPUT-'].strip()
        if not user_input:
            continue

        # 根据显示名称找到对应的 provider 和 model
        selected_display = values['-MODEL-']
        selected_model = next((m for m in available_models if m["display"] == selected_display), None)
        if not selected_model:
            sg.popup_error("模型选择无效")
            continue

        print(f"[系统] 获取到用户输入: {user_input}，使用模型: {selected_model['provider']}/{selected_model['model']}")
        result = run_agent(user_input, selected_model)

        if result and isinstance(result, dict):
            if result.get("success"):
                sg.popup("操作成功", result["message"])
            else:
                sg.popup_error("操作失败", result.get("message", "未知错误"))
        else:
            sg.popup_error("解析失败", "AI 未能返回有效指令")

        window['-INPUT-'].update('')
window.close()