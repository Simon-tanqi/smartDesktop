# 快速验证 PPT 渲染管道
import sys
sys.path.insert(0, "D:\\PyCharm Project\\MyAgent")
from ppt_generator.renderer import render

slides = [
    {
        "template": "cover",
        "data": {
            "title": "2026 年 AI 发展报告",
            "subtitle": "普通人如何抓住 AI 时代的机遇",
            "meta": "MyAgent · 2026年7月",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "AI 三大趋势",
            "items": [
                {"title": "🤖 多模态大模型", "desc": "文本、图像、视频、语音全面融合，交互方式革命"},
                {"title": "🧠 Agent 自主智能", "desc": "AI 从对话走向自主决策、工具调用和任务执行"},
                {"title": "📊 行业落地加速", "desc": "医疗、金融、教育、制造四大领域深度渗透"},
            ],
            "side": {
                "title": "📌 关键数据",
                "list": [
                    "全球 AI 市场规模预计突破 $3000 亿",
                    "中国 AI 相关岗位增长 47%",
                    "Agent 类产品融资增长 3 倍",
                ],
            },
            "footer": "MyAgent | 1/3",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "普通人四大机遇",
            "items": [
                {"title": "💼 AI + 副业", "desc": "利用 AI 工具做内容创作、编程辅助、设计出图"},
                {"title": "🎓 技能升级", "desc": "Prompt Engineering、AI Agent 开发、数据分析"},
                {"title": "🛠️ 垂直应用", "desc": "细分行业的小模型微调、RAG 应用开发"},
                {"title": "🌐 全球化", "desc": "借助 AI 突破语言壁垒，拓展海外市场"},
            ],
            "side": {
                "title": "⚡ 建议行动",
                "list": [
                    "本月：熟悉 2-3 个主流 AI 工具",
                    "本季：完成一个小型 AI 应用",
                    "今年：建立 AI 领域的个人 IP",
                ],
            },
            "footer": "MyAgent | 2/3",
        },
    },
]

out = render(slides, theme_name="tech_blue")
print(f"✅ PPT 已生成: {out}")
