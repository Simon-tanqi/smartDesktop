"""PowerPoint演示文稿处理工具：创建、编辑、分析PPT文件"""
import os
import json
import sys
from pathlib import Path

from . import register_tool, path_params


try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    from pptx.dml.color import RGBColor
    PPT_AVAILABLE = True
except ImportError:
    PPT_AVAILABLE = False
    
    
# 在 generate_ppt 的 tool schema 中，强制要求每个 slide 携带 source 字段

PPT_SLIDE_SCHEMA = {
    "type": "object",
    "required": ["slide_type", "title", "content", "data_source"],
    "properties": {
        "slide_type": {"type": "string", "enum": ["cover", "content", "section", "chart", "table", "comparison"]},
        "title": {"type": "string"},
        "content": {"type": "string"},
        "data_source": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["search_verified", "model_knowledge", "user_provided", "estimated"]
                },
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "数据来源URL（type=search_verified时必填）"
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "数据可信度"
                },
                "note": {
                    "type": "string",
                    "description": "如 type=estimated，说明估算依据"
                }
            }
        }
    }
}

@register_tool(timeout=120)
def generate_ppt(slides: list = None, theme: str = 'tech_blue', output_path: str = None, 
                 img_timeout: int = 30, download_timeout: int = 60, **kwargs):
    """
    一站式生成视觉精美的PowerPoint演示文稿（HTML截图背景 + 可编辑文字层）。
    内部流程：HTML+CSS模板渲染 → Playwright 截图做背景 → python-pptx 覆盖透明文本层。
    效果：打开PPT看到精美设计，点击文字可直接编辑。
    
    返回：{success, message, file_path, file_size, slide_count}
    
    Args:
        slides: 幻灯片列表，支持多模板混排，每页格式：
            {  "template": "模板名", "data": { 模板数据 }  }

            可用模板：

            cover — 封面页
              title: 标题, subtitle: 副标题, meta: 底部信息

            section — 章节分隔页（深色背景）
              title: 章节标题, subtitle: 副标题, number: 编号

            content — 内容页（列表项+可选侧边栏）
              title: 标题, items: [{"title":"标题","desc":"描述","icon":"emoji"}],
              side: {"title":"侧边栏标题","list":["A","B"],"stat":"数值","stat_label":"说明"},
              footer: 页脚

            icon_grid — 图标网格页（展示特点/功能/数据指标）
              title: 标题, desc: 描述, cols: 列数(默认4),
              items: [{"icon":"📊","title":"名称","desc":"说明","stat":"数值"}]

            chart — 数据图表页（柱状图+饼图）
              title: 标题,
              bars: [{"label":"X轴","value":数值,"pct":百分比高度}],
              pie: {"total":"总计","segments":["色值1 百分比","色值2 百分比"],"legend":[{"color":"#色值","label":"名称","value":"数值"}]}

            table — 数据表格页
              title: 标题, desc: 说明,
              columns: ["列名1","列名2",...],
              rows: [["单元格1","单元格2"],...]

            image_text — 图文混排页（左图右文）
              title: 标题, paragraphs: ["段落1","段落2"], icon: "🖼️",
              tagline: 标签行, img_url: 可选图片URL

            comparison — 对比页（双栏对比）
              title: 标题, desc: 说明,
              col_a: {"badge":"标签","title":"标题","items":["特点1","特点2"],"price":"￥价格","price_sub":"说明"},
              col_b: {"badge":"标签","title":"标题","items":["特点1","特点2"],"price":"￥价格","price_sub":"说明"}

        theme: 主题配色，可选: tech_blue(科技蓝), business_gray(商务灰),
               gradient_purple(渐变紫), education_orange(教育橙), minimal_dark(极简黑)
        output_path: 可选输出路径，默认自动生成到workspace目录
        img_timeout: 外部图片加载超时时间（秒），默认30秒
        download_timeout: 图片下载超时时间（秒），默认60秒
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from ppt_generator.renderer import render
        from ppt_generator.themes import THEMES
        from config.settings import DATA_DIR
        
        if theme not in THEMES:
            theme = 'tech_blue'
        
        if 'raw' in kwargs and slides is None:
            raw_args = kwargs.get('raw')
            try:
                if isinstance(raw_args, str):
                    raw_data = json.loads(raw_args)
                else:
                    raw_data = raw_args
                if isinstance(raw_data, dict):
                    slides = raw_data.get('slides')
                    theme = raw_data.get('theme', theme)
                    output_path = raw_data.get('output_path', output_path)
                elif isinstance(raw_data, list):
                    slides = raw_data
            except Exception:
                pass
        
        if slides is None:
            return json.dumps({"success": False, "message": "缺少必需参数: slides"}, ensure_ascii=False)
        
        if output_path is None:
            workspace_dir = os.path.join(DATA_DIR, "workspace")
            os.makedirs(workspace_dir, exist_ok=True)
            from datetime import datetime
            import uuid
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            rand_suffix = str(uuid.uuid4())[:8]
            output_path = os.path.join(workspace_dir, f"presentation_{timestamp}_{rand_suffix}.pptx")
        else:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
        
        out = render(slides, theme_name=theme, output_path=output_path, 
                      img_timeout=img_timeout, download_timeout=download_timeout)
        
        fp = Path(out)
        from pptx import Presentation
        prs = Presentation(out)
        result = {
            "success": True,
            "message": f"PPT 已生成: {out}",
            "file_path": str(fp.resolve()),
            "file_size": f"{fp.stat().st_size / 1024:.1f}KB",
            "slide_count": len(prs.slides),
        }
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        import traceback
        return json.dumps({"success": False, "message": f"生成PPT失败: {str(e)}", "traceback": traceback.format_exc()}, ensure_ascii=False)


@register_tool
def create_presentation(file_path: str, config: dict = None):
    """创建PowerPoint演示文稿。file_path: 保存路径, config: 演示文稿配置"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    default_config = {
        "title": "新建演示文稿",
        "slides": [
            {
                "type": "title",
                "title": "欢迎使用MyAgent",
                "subtitle": "PowerPoint演示文稿自动生成"
            },
            {
                "type": "content",
                "title": "功能特点",
                "content": ["• 自动化PPT生成", "• 批量处理", "• 智能排版"]
            }
        ]
    }
    
    if config:
        default_config.update(config)
    
    try:
        # 创建演示文稿
        prs = Presentation()
        
        for slide_config in default_config["slides"]:
            if slide_config["type"] == "title":
                # 标题幻灯片
                slide_layout = prs.slide_layouts[0]
                slide = prs.slides.add_slide(slide_layout)
                title = slide.shapes.title
                subtitle = slide.placeholders[1]
                
                title.text = slide_config.get("title", "")
                subtitle.text = slide_config.get("subtitle", "")
                
            elif slide_config["type"] == "content":
                # 内容幻灯片
                slide_layout = prs.slide_layouts[1]
                slide = prs.slides.add_slide(slide_layout)
                title = slide.shapes.title
                content = slide.placeholders[1]
                
                title.text = slide_config.get("title", "")
                content.text = "\n".join(slide_config.get("content", []))
                
            elif slide_config["type"] == "image":
                # 图片幻灯片
                slide_layout = prs.slide_layouts[5]  # 空白布局
                slide = prs.slides.add_slide(slide_layout)
                title = slide.shapes.title
                title.text = slide_config.get("title", "")
                
                # 添加图片
                image_path = slide_config.get("image_path")
                if image_path and os.path.exists(image_path):
                    left = Inches(1)
                    top = Inches(2)
                    slide.shapes.add_picture(image_path, left, top, width=Inches(8))
        
        # 保存演示文稿
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        prs.save(file_path)
        
        result = {
            "success": True,
            "message": f"演示文稿已创建: {file_path}",
            "slide_count": len(prs.slides),
            "file_size": f"{Path(file_path).stat().st_size / 1024:.1f}KB"
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"创建演示文稿失败: {str(e)}"}, ensure_ascii=False)

@register_tool(timeout=30)
@path_params("file_path", "image_path")  # 需要导入 path_params
def add_slide_content(file_path: str, slide_config: dict) -> str:
    """
    向PPT添加幻灯片（支持文字、项目符号、图片、标题页）
    
    Args:
        file_path: PPT文件路径
        slide_config: 幻灯片配置，包含:
            - type: "title" | "content" | "bullet" | "image" | "blank"
            - title: 标题文本
            - content: 内容列表 (用于 content 类型)
            - bullets: 项目符号列表 (用于 bullet 类型)
            - image_path: 图片路径 (用于 image 类型)
            - layout_name: 可选，布局名称 (如 "标题幻灯片", "内容与标题")
    """
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库"})
    
    try:
        # 1. 确保目录存在
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        
        # 2. 加载或新建
        if os.path.exists(file_path):
            prs = Presentation(file_path)
        else:
            prs = Presentation()
        
        slide_type = slide_config.get("type", "content")
        
        # 3. 根据布局名称或类型选择布局 (更稳健)
        layout_map = {
            "标题幻灯片": 0,
            "标题和内容": 1,
            "节标题": 2,
            "两栏内容": 3,
            "比较": 4,
            "仅标题": 5,
            "空白": 6,
        }
        # 如果用户指定了 layout_name，优先使用
        layout_name = slide_config.get("layout_name", "")
        if layout_name in layout_map:
            layout_idx = layout_map[layout_name]
        else:
            # 根据类型映射默认布局
            default_layouts = {
                "title": 0,
                "content": 1,
                "bullet": 1,
                "image": 5,  # 仅标题（适合放图）
                "blank": 6
            }
            layout_idx = default_layouts.get(slide_type, 1)
        
        # 如果索引超出范围，兜底使用空白布局
        if layout_idx >= len(prs.slide_layouts):
            layout_idx = 6 if len(prs.slide_layouts) > 6 else len(prs.slide_layouts) - 1
        
        slide_layout = prs.slide_layouts[layout_idx]
        slide = prs.slides.add_slide(slide_layout)
        
        # 4. 处理标题 (如果有标题占位符)
        if slide.shapes.title:
            title_shape = slide.shapes.title
            title_shape.text = slide_config.get("title", "")
        
        # 5. 根据类型处理内容
        if slide_type == "title":
            # 标题幻灯片：处理副标题 (通常占位符索引1)
            if len(slide.placeholders) > 1:
                subtitle = slide.placeholders[1]
                # 清空默认文本
                subtitle.text = slide_config.get("subtitle", "")
                
        elif slide_type == "content":
            # 内容幻灯片（带正文占位符）
            content_placeholder = None
            # 寻找正文占位符 (通常不是标题)
            for placeholder in slide.placeholders:
                if placeholder != slide.shapes.title:
                    content_placeholder = placeholder
                    break
            
            if content_placeholder:
                # 关键修复：清空默认文本（解决“单击添加文本”叠加问题）
                text_frame = content_placeholder.text_frame
                text_frame.clear()  # 清除所有段落
                
                content_items = slide_config.get("content", [])
                if isinstance(content_items, str):
                    content_items = [content_items]
                
                for i, item in enumerate(content_items):
                    if i == 0:
                        text_frame.text = item
                    else:
                        p = text_frame.add_paragraph()
                        p.text = item
                        p.level = 0

        elif slide_type == "bullet":
            # 项目符号列表（带缩进层级）
            content_placeholder = None
            for placeholder in slide.placeholders:
                if placeholder != slide.shapes.title:
                    content_placeholder = placeholder
                    break
            
            if content_placeholder:
                text_frame = content_placeholder.text_frame
                text_frame.clear()  # 清空默认文本
                
                bullets = slide_config.get("bullets", [])
                for i, item in enumerate(bullets):
                    p = text_frame.add_paragraph() if i > 0 else text_frame.paragraphs[0]
                    p.text = item
                    p.level = slide_config.get("level", 0)  # 默认一级
                    # 设置项目符号样式（可选）
                    p.bullet = True

        elif slide_type == "image":
            # 图片幻灯片（新增支持）
            image_path = slide_config.get("image_path")
            if not image_path or not os.path.exists(image_path):
                return json.dumps({"success": False, "message": f"图片不存在: {image_path}"})
            
            # 计算居中位置 (默认宽度占页面的80%)
            left = Inches(1)
            top = Inches(1.5)
            width = Inches(8)
            height = Inches(5)  # 保持比例，实际会自动适配
            slide.shapes.add_picture(image_path, left, top, width=width)
            
            # 如果幻灯片本身有标题，已经在上面设置了

        elif slide_type == "blank":
            pass  # 什么都不做

        # 6. 保存
        prs.save(file_path)
        
        return json.dumps({
            "success": True,
            "message": f"幻灯片添加成功 ({slide_type})",
            "slide_type": slide_type,
            "current_slide_count": len(prs.slides),
            "file_path": file_path
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"添加幻灯片失败: {str(e)}"}, ensure_ascii=False)

@register_tool
def extract_presentation_info(file_path: str):
    """提取PPT演示文稿信息。file_path: PPT文件路径"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    try:
        prs = Presentation(file_path)
        
        slides_info = []
        total_text_length = 0
        
        for i, slide in enumerate(prs.slides):
            slide_text = []
            
            # 提取所有形状中的文本
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text = shape.text.strip()
                    if text:
                        slide_text.append(text)
                        total_text_length += len(text)
            
            slides_info.append({
                "slide_number": i + 1,
                "text_count": len(slide_text),
                "text_preview": slide_text[:3],  # 前3个文本内容
                "has_table": any(hasattr(shape, "table") for shape in slide.shapes),
                "has_chart": any(hasattr(shape, "chart") for shape in slide.shapes)
            })
        
        result = {
            "success": True,
            "presentation_info": {
                "slide_count": len(prs.slides),
                "total_text_length": total_text_length,
                "file_size": f"{Path(file_path).stat().st_size / 1024:.1f}KB"
            },
            "slides_info": slides_info,
            "summary": {
                "slides_with_text": sum(1 for slide in slides_info if slide["text_count"] > 0),
                "slides_with_tables": sum(1 for slide in slides_info if slide["has_table"]),
                "slides_with_charts": sum(1 for slide in slides_info if slide["has_chart"])
            }
        }
        
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"提取信息失败: {str(e)}"}, ensure_ascii=False)

@register_tool
def export_presentation_text(file_path: str, output_file: str = None):
    """导出PPT文本内容。file_path: PPT文件路径, output_file: 输出文本文件路径"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    try:
        prs = Presentation(file_path)
        
        all_text = []
        
        for i, slide in enumerate(prs.slides):
            slide_text = [f"=== 幻灯片 {i+1} ==="]
            
            # 提取所有文本
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text = shape.text.strip()
                    if text:
                        slide_text.append(text)
            
            # 提取表格文本
            for shape in slide.shapes:
                if hasattr(shape, "table"):
                    slide_text.append("[表格内容]")
                    for row in shape.table.rows:
                        row_text = []
                        for cell in row.cells:
                            cell_text = cell.text.strip()
                            if cell_text:
                                row_text.append(cell_text)
                        if row_text:
                            slide_text.append(" | ".join(row_text))
            
            all_text.extend(slide_text)
            all_text.append("")  # 幻灯片间空行
        
        # 输出到文件或直接返回
        full_text = "\n".join(all_text)
        
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(full_text)
            
            result = {
                "success": True,
                "message": f"文本内容已导出到: {output_file}",
                "slide_count": len(prs.slides),
                "total_lines": len(all_text)
            }
        else:
            result = {
                "success": True,
                "message": "文本内容提取成功",
                "slide_count": len(prs.slides),
                "text_preview": full_text[:1000] + "..." if len(full_text) > 1000 else full_text
            }
        
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"导出文本失败: {str(e)}"}, ensure_ascii=False)

@register_tool
def batch_ppt_process(directory: str, operation: str):
    """批量处理PPT文件。directory: 文件夹路径, operation: 操作类型"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    try:
        directory_path = Path(directory)
        ppt_files = list(directory_path.glob("*.pptx")) + list(directory_path.glob("*.ppt"))
        
        results = []
        if operation == "merge":
            base_prs = Presentation()
        for file_path in ppt_files:
            try:
                prs = Presentation(file_path)
                
                if operation == "info":
                    info = {
                        "文件名": file_path.name,
                        "幻灯片数量": len(prs.slides),
                        "文件大小": f"{file_path.stat().st_size / 1024:.1f}KB",
                        "总字符数": sum(
                            len(shape.text) 
                            for slide in prs.slides 
                            for shape in slide.shapes 
                            if hasattr(shape, "text")
                        )
                    }
                    results.append(info)
                
                elif operation == "merge":
                    for slide in prs.slides:
                        slide_layout = base_prs.slide_layouts[6]  # blank layout
                        new_slide = base_prs.slides.add_slide(slide_layout)
                        # Copy shapes from source slide
                        for shape in slide.shapes:
                            if hasattr(shape, "element"):
                                new_slide.shapes._spTree.append(shape.element)
                    
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        # 保存合并结果
        if operation == "merge":
            merge_output = directory_path / "合并演示文稿.pptx"
            base_prs.save(merge_output)
            
        result = {
            "success": True,
            "operation": operation,
            "files_processed": len(results),
            "results": results
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"批量处理失败: {str(e)}"}, ensure_ascii=False)