# 输出格式

每轮输出一个 JSON，三种类型之一：
- 工具调用：{"_thinking":"推理","tool_name":"...","arguments":{...}}
- 进度：{"progress":"正在扫描..."}
- 最终回复：{"final_message":"纯文本回复"}

严禁一次输出多个 JSON。final_message 只能是纯文本。
