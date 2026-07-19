# PowerPoint 精美演示文稿生成

使用 `generate_ppt` 一次性生成视觉精美的 PPTX。

## 核心工具

**`generate_ppt(slides, theme, output_path)`**

## 参数说明

- `slides`：幻灯片列表，包含所有页面的内容和布局配置
- `theme`：主题配色，默认 `tech_blue`
- `output_path`：输出路径（可选）

## 模板类型

- `cover` — 封面页（渐变色背景+标题+副标题+底部信息）
- `content` — 内容页（白色背景+顶部装饰条+主内容区+可选侧边栏）

## 主题配色

- `tech_blue` — 深蓝渐变，科技/专业报告
- `business_gray` — 蓝灰，商务汇报
- `gradient_purple` — 紫色渐变，创意风格
- `education_orange` — 橙色暖色，培训教育
- `minimal_dark` — 暗色极简

## slides 数据格式

```json
[
  {
    "template": "cover",
    "data": {
      "title": "主标题",
      "subtitle": "副标题",
      "meta": "日期/作者"
    }
  },
  {
    "template": "content",
    "data": {
      "title": "页面标题",
      "items": [
        {"title": "要点标题", "desc": "详细描述"}
      ],
      "side": {"title": "侧边栏", "list": ["A", "B"]},
      "footer": "页脚"
    }
  }
]
```

## 规则

- **必须一次性生成全部页面**，不要分多次调用
- 耗时约 5-10 秒/页，请告知用户正在生成
- 返回文件路径并通知用户查看
