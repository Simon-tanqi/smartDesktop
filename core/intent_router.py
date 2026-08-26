"""L0-L3 意图分类与路由（规则优先，不调用 LLM）。

四层路由设计：
- L0 直接回答：问候 / 感谢 / 告别 / 身份等，硬编码正则匹配并返回预设回复，完全不走 LLM。
- L1 简单任务：时间 / 日期 / 星期由本地格式化直接回复；天气类规则命中但当前
  无可用工具，标记 degraded=True 由上层降级到 L2 走 LLM。
- L2 简单 LLM 任务：默认档位（写总结 / 解释概念 / 推荐等），走 LLM 但无规划、无复杂工具链。
- L3 复杂任务：命中「动作 + 产物」多步强信号，由上层实例化 Planner 生成计划并进入 ReAct 循环。

本模块只做分类与回复生成，不依赖任何引擎/LLM，保证 L0/L1 命中时零 LLM 调用。
"""
import re
import datetime
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class IntentResult:
    """意图分类结果。

    route_level: "L0" / "L1" / "L2" / "L3"
    reply: L0/L1 直接回复文本（L2/L3 为空）
    degraded: L1 命中但工具缺失，需由上层降级到 L2 走 LLM
    exec_action: L1 工具执行型意图的工具名（当前恒为空：原预设路由工具未实现已移除，见 docs/ROADMAP.md）
    exec_params: 上述工具的执行参数（dict）
    detail: 命中规则说明（日志/排查用）
    """
    route_level: str = "L2"
    reply: str = ""
    degraded: bool = False
    exec_action: str = ""
    exec_params: dict = None
    detail: str = "默认 L2（简单 LLM 任务）"


# ═══════════════════════════════════════════════
# L0 规则库：问候 / 感谢 / 告别 / 身份 / 能力
# ═══════════════════════════════════════════════

_L0_PATTERNS: List[tuple] = [
    (re.compile(r"^(你好|您好|哈喽|嗨|嗨喽|早上好|上午好|中午好|下午好|晚上好|早安|晚安|hello|hi|hey)[!！。.\s]*$", re.IGNORECASE),
     "你好！我是您的桌面智能助手，有什么可以帮您的吗？"),
    (re.compile(r"^(谢谢|感谢|多谢|thanks|thank you|thx)[!！。.\s]*$", re.IGNORECASE),
     "不客气，随时为您效劳。"),
    (re.compile(r"^(再见|拜拜|bye|goodbye|回见|下次见|see you)[!！。.\s]*$", re.IGNORECASE),
     "再见，期待下次为您服务！"),
    (re.compile(r"^(你是谁|你叫什么|介绍一下你自己|what are you|who are you)[?？]?$", re.IGNORECASE),
     "我是您的桌面智能助手，可以帮您查询信息、操作文件、执行命令等，请告诉我您的需求。"),
    (re.compile(r"^(你能做什么|你会什么|你有什么功能|你能帮我做什么|help)[?？]?$", re.IGNORECASE),
     "我可以帮您：查时间天气、搜索网页、读写文件、执行命令、生成文档等。直接说出您的需求即可。"),
]


def _try_l0(text: str) -> Optional[str]:
    """L0 规则匹配，命中返回预设回复，否则返回 None。"""
    for pattern, reply in _L0_PATTERNS:
        if pattern.match(text.strip()):
            return reply
    return None


# ═══════════════════════════════════════════════
# L1 规则库：时间 / 日期 / 星期 / 天气
# ═══════════════════════════════════════════════

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_L1_TIME = re.compile(r"现在几点|几点钟|当前时间|几点了|现在什么时间|现在什么时候|现在时间|几时|what time|time now", re.IGNORECASE)
_L1_DATE = re.compile(r"今天几号|今天日期|现在几号|今天是多少号|今天什么日期|什么日期|today.{0,6}date|date today", re.IGNORECASE)
_L1_WEEKDAY = re.compile(r"今天星期几|今天周几|星期几|周几|what day", re.IGNORECASE)
# 天气类：命中表示用户期望天气信息，但当前无本地天气工具，置 degraded 由上层降级 L2
_L1_WEATHER = re.compile(r"今天天气|天气怎么样|天气如何|天气怎样|天气好|下雨|气温|多少度|weather", re.IGNORECASE)

# 任务信号词：时间/日期/星期/工具直连命中时，若消息同时含这些信号，
# 视为复合任务（如"几点了，顺便帮我写份报告"），不做 L1 直答以免截断真实任务。
_L1_TASK_SIGNALS = re.compile(
    r"帮我|帮忙|麻烦|顺便|然后|接着|并且|同时|还要|还需要|另外|再帮我|"
    r"写|生成|创建|制作|搜索|搜一下|下载|删除|修改|执行|运行|分析|统计|总结|翻译|打开|安装|卸载|整理",
    re.IGNORECASE,
)


def _parse_volume_level(text: str):
    """从音量表达中解析目标音量级别，返回 0-100 整数；无法解析返回 None。

    注：原 L1 音量直连路由已移除（set_system_volume 工具未实现，见 docs/ROADMAP.md），
    本函数暂无调用方，保留以待工具实现后重挂路由时复用。
    """
    m = re.search(r"百分之\s*(\d{1,3})", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    m = re.search(r"调到\s*(\d{1,3})|设为\s*(\d{1,3})|设成\s*(\d{1,3})|改成\s*(\d{1,3})|改为\s*(\d{1,3})|到\s*(\d{1,3})|至\s*(\d{1,3})", text)
    if m:
        for g in m.groups():
            if g is not None:
                return max(0, min(100, int(g)))
    if re.search(r"调大|增大|提高|大一点|大点", text):
        return 80
    if re.search(r"调小|减小|降低|小一点|小点", text):
        return 20
    if re.search(r"最大", text):
        return 100
    if re.search(r"最小", text):
        return 0
    if re.search(r"一半", text):
        return 50
    return None


def _try_l1(text: str) -> tuple:
    """L1 规则匹配，返回 (reply, degraded, exec_action, exec_params)。

    reply 非空时由上层直接返回（时间/日期/星期，本地生成）；
    reply 为空且 degraded=True 时上层降级到 L2（通用天气类，无工具可用）。
    exec_action 恒为空：原 L1 工具执行型预设路由（昨日天气/音量/桌面图标）依赖的
    三个工具未实现，对应路由已移除，相关问句安全降级走 LLM 流程（见 docs/ROADMAP.md）。
    """
    now = datetime.datetime.now()
    # 复合任务护栏：命中预设规则但消息里还有任务信号时，回落原流程走 LLM
    has_task_signal = bool(_L1_TASK_SIGNALS.search(text))
    if _L1_TIME.search(text) and not has_task_signal:
        return (f"现在是 {now:%H:%M}。", False, "", None)
    if _L1_DATE.search(text) and not has_task_signal:
        return (f"今天是 {now:%Y} 年 {now:%m} 月 {now:%d} 日。", False, "", None)
    if _L1_WEEKDAY.search(text) and not has_task_signal:
        return (f"今天是{_WEEKDAYS[now.weekday()]}。", False, "", None)
    if _L1_WEATHER.search(text):
        return ("", True, "", None)
    return ("", False, "", None)


# ═══════════════════════════════════════════════
# L3 规则库：多步产物型任务强信号
# ═══════════════════════════════════════════════

_L3_ACTION = (
    "生成|创建|制作|开发|实现|搭建|整理|归档|分析|调研|研究|设计|编写|撰写|构建|部署|安装|批量|优化|重构|翻译"
)
_L3_PRODUCT = (
    "报告|文档|PPT|PPTX|演示文稿|Excel|表格|代码|脚本|程序|项目|网页|网站|视频|图片|文件|目录|文件夹|"
    "数据库|系统|应用|软件|方案|桌面|简历|论文|合同|报表|图表"
)
_L3_COMBO = re.compile(rf"({_L3_ACTION}).{{0,12}}({_L3_PRODUCT})")
_L3_DIRECT = re.compile(rf"(做一个|做一份|写一个|写一份|写个|搞一个|帮我做个|帮我写个|帮我做一份|生成一个|创建一个|制作一份).{{0,16}}({_L3_PRODUCT})")


def _try_l3(text: str) -> bool:
    """L3 强信号匹配：动作+产物组合或直接宾语结构。"""
    if _L3_COMBO.search(text):
        return True
    if _L3_DIRECT.search(text):
        return True
    return False


def classify_intent(user_message: str) -> IntentResult:
    """对用户消息做 L0-L3 意图分类（纯规则，不调用 LLM）。"""
    text = (user_message or "").strip()
    if not text:
        return IntentResult(route_level="L2", detail="空输入，走 L2 兜底")

    # L0：问候/感谢/告别/身份 → 直接回答
    l0_reply = _try_l0(text)
    if l0_reply is not None:
        return IntentResult(route_level="L0", reply=l0_reply, detail="L0 问候/身份规则命中")

    # L1：时间/日期/星期/天气/工具执行型预设路由 → 本地格式化或直连工具
    l1_reply, l1_degraded, l1_exec_action, l1_exec_params = _try_l1(text)
    if l1_degraded:
        return IntentResult(route_level="L1", reply="", degraded=True,
                            detail="L1 天气规则命中但无本地工具，降级 L2")
    if l1_exec_action:
        return IntentResult(route_level="L1", exec_action=l1_exec_action,
                            exec_params=l1_exec_params or {},
                            detail=f"L1 工具执行型预设路由命中: {l1_exec_action}")
    if l1_reply:
        return IntentResult(route_level="L1", reply=l1_reply, detail="L1 时间/日期/星期规则命中")

    # L3：多步产物型任务 → 上层实例化 Planner 规划后进入 ReAct
    if _try_l3(text):
        return IntentResult(route_level="L3", detail="L3 多步产物型任务强信号命中")

    return IntentResult(route_level="L2", detail="默认 L2（简单 LLM 任务）")


def build_plan_prompt(plan, prefix: str = "【当前任务规划】") -> str:
    """将 TaskPlan 序列化为注入 system 消息的规划提示词文本。"""
    if plan is None or not getattr(plan, "steps", None):
        return ""
    lines = [f"{prefix}请严格按以下步骤执行，逐步完成，不要跳过："]
    for i, step in enumerate(plan.steps, start=1):
        desc = getattr(step, "description", None) or str(step)
        tool = getattr(step, "tool_name", "") or ""
        lines.append(f"{i}. {desc}" + (f"（推荐工具：{tool}）" if tool else ""))
    if getattr(plan, "final_answer", ""):
        lines.append(f"\n最终产出要求：{plan.final_answer}")
    return "\n".join(lines)
