import sys
sys.path.insert(0, "D:\\PyCharm Project\\MyAgent")
from ppt_generator.renderer import render

slides = [
    {
        "template": "cover",
        "data": {
            "title": "AI时代的基础设施行业",
            "subtitle": "普通人如何抓住机遇",
            "meta": "MyAgent · 2026年7月",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "AI时代基础设施三大趋势",
            "items": [
                {"title": "算力基础设施爆发", "desc": "数据中心、GPU集群、边缘计算需求激增，AI芯片产业链全面受益"},
                {"title": "数据要素价值释放", "desc": "数据采集、清洗、标注、存储、传输全链条迎来黄金期"},
                {"title": "智能连接升级", "desc": "5G/6G、卫星互联网、工业互联网构建万物互联新生态"},
            ],
            "side": {
                "title": "关键数据",
                "list": [
                    "全球AI算力需求年增300%",
                    "数据中心投资年增50%",
                    "AI芯片市场规模破千亿美元",
                ],
            },
            "footer": "MyAgent | 1/8",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "普通人面临的挑战与机遇",
            "items": [
                {"title": "挑战：技术门槛提高", "desc": "AI时代对专业技能要求更高，传统岗位面临重构"},
                {"title": "机遇：新岗位大量涌现", "desc": "AI训练师、数据标注师、Prompt工程师等新职业诞生"},
                {"title": "机遇：技能溢价提升", "desc": "掌握AI工具的复合型人才价值倍增"},
            ],
            "side": {
                "title": "机遇窗口",
                "list": [
                    "AI基础设施建设潮",
                    "传统行业智能化改造",
                    "AI应用场景落地",
                ],
            },
            "footer": "MyAgent | 2/8",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "破局核心策略",
            "items": [
                {"title": "做AI做不到的事", "desc": "深度行业理解、创意创新、情感沟通是AI难以替代的核心竞争力"},
                {"title": "让AI为你工作", "desc": "熟练掌握AI工具，把重复性工作交给AI，专注高价值创造"},
                {"title": "跨界融合", "desc": "将传统行业经验与AI技术结合，形成独特竞争优势"},
            ],
            "side": {
                "title": "核心能力",
                "list": [
                    "学习能力与好奇心",
                    "问题解决与创造力",
                    "持续进化与适应力",
                ],
            },
            "footer": "MyAgent | 3/8",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "具体行动路径",
            "items": [
                {"title": "阶段一：掌握工具", "desc": "学习主流AI工具（ChatGPT、Midjourney、Python等），建立技术基础"},
                {"title": "阶段二：深耕领域", "desc": "选择一个细分领域深入研究，成为AI+行业的复合型专家"},
                {"title": "阶段三：打造品牌", "desc": "通过内容创作、项目实践建立个人IP，提升行业影响力"},
            ],
            "side": {
                "title": "推荐学习路径",
                "list": [
                    "1-3个月：掌握AI工具",
                    "3-6个月：完成项目实战",
                    "6-12个月：建立个人品牌",
                ],
            },
            "footer": "MyAgent | 4/8",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "值得关注的新赛道",
            "items": [
                {"title": "AI提示工程师", "desc": "设计高效的AI提示词，最大化AI模型输出质量，是当前最热门的新职业"},
                {"title": "数据服务", "desc": "数据标注、数据清洗、数据治理，AI时代的数字基建工人"},
                {"title": "AI内容创作", "desc": "利用AI工具进行文案、设计、视频创作，效率提升10倍以上"},
                {"title": "垂直领域AI应用", "desc": "医疗AI、教育AI、工业AI等细分领域的应用开发与落地"},
            ],
            "side": {
                "title": "赛道选择建议",
                "list": [
                    "结合自身兴趣",
                    "考虑市场需求",
                    "评估竞争程度",
                ],
            },
            "footer": "MyAgent | 5/8",
        },
    },
    {
        "template": "content",
        "data": {
            "title": "从现在开始的行动计划",
            "items": [
                {"title": "第1个月：AI工具入门", "desc": "注册并熟悉2-3个主流AI平台，完成基础功能学习，尝试用AI解决日常问题"},
                {"title": "第2-3个月：技能深化", "desc": "学习Python基础、数据分析、Prompt Engineering，完成3个以上AI辅助项目"},
                {"title": "第4-6个月：领域深耕", "desc": "选择一个垂直领域深入研究，发布相关内容，建立初步行业认知"},
                {"title": "第6-12个月：品牌建设", "desc": "持续输出高质量内容，参与行业交流，逐步建立个人影响力"},
            ],
            "side": {
                "title": "关键行动",
                "list": [
                    "每天使用AI工具",
                    "每周输出内容",
                    "每月复盘进步",
                ],
            },
            "footer": "MyAgent | 6/8",
        },
    },
    {
        "template": "cover",
        "data": {
            "title": "总结：AI时代的生存法则",
            "subtitle": "拥抱变化 · 驾驭AI · 持续进化",
            "meta": "MyAgent · 2026年7月",
        },
    },
]

output_path = r"C:\Users\username\AppData\Roaming\MyAgent\workspace\AI时代基础设施机遇.pptx"
out = render(slides, theme_name="tech_blue", output_path=output_path)
print(f"✅ PPT 已生成: {out}")
