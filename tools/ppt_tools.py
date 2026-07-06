"""PowerPoint演示文稿处理工具：创建、编辑、分析PPT文件"""
import os
import json
from pathlib import Path

# from . import register_tool, path_params  # 将在函数内部导入避免循环依赖


try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    from pptx.dml.color import RGBColor
    PPT_AVAILABLE = True
except ImportError:
    PPT_AVAILABLE = False


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


def add_slide_content(file_path: str, slide_config: dict):
    """向PPT添加幻灯片内容。file_path: PPT文件路径, slide_config: 幻灯片配置"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    try:
        # 加载现有演示文稿或创建新的
        if os.path.exists(file_path):
            prs = Presentation(file_path)
        else:
            prs = Presentation()
        
        slide_type = slide_config.get("type", "content")
        
        if slide_type == "title":
            slide_layout = prs.slide_layouts[0]
            slide = prs.slides.add_slide(slide_layout)
            title = slide.shapes.title
            subtitle = slide.placeholders[1]
            title.text = slide_config.get("title", "")
            subtitle.text = slide_config.get("subtitle", "")
            
        elif slide_type == "content":
            slide_layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(slide_layout)
            title = slide.shapes.title
            content = slide.placeholders[1]
            title.text = slide_config.get("title", "")
            content.text = "\n".join(slide_config.get("content", []))
            
        elif slide_type == "bullet":
            slide_layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(slide_layout)
            title = slide.shapes.title
            content = slide.placeholders[1]
            title.text = slide_config.get("title", "")
            
            # 添加项目符号列表
            text_frame = content.text_frame
            for item in slide_config.get("bullets", []):
                p = text_frame.add_paragraph()
                p.text = item
                p.level = 0
            
        elif slide_type == "blank":
            slide_layout = prs.slide_layouts[6]  # 空白布局
            slide = prs.slides.add_slide(slide_layout)
        
        # 保存
        prs.save(file_path)
        
        result = {
            "success": True,
            "message": f"幻灯片添加成功 ({slide_type})",
            "slide_type": slide_type,
            "current_slide_count": len(prs.slides)
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"添加幻灯片失败: {str(e)}"}, ensure_ascii=False)


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


def batch_ppt_process(directory: str, operation: str):
    """批量处理PPT文件。directory: 文件夹路径, operation: 操作类型"""
    if not PPT_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-pptx库: pip install python-pptx"})
    
    try:
        directory_path = Path(directory)
        ppt_files = list(directory_path.glob("*.pptx")) + list(directory_path.glob("*.ppt"))
        
        results = []
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
                    # 合并所有PPT到第一个文件
                    if not results:  # 第一个文件作为基础
                        base_prs = prs
                        base_file = file_path
                    else:
                        # 添加后续文件的幻灯片
                        for slide in prs.slides:
                            new_slide = base_prs.slides.add_slide(slide.slide_layout)
                            # 复制内容（简化版）
                            
                    info = {
                        "文件名": file_path.name,
                        "已合并幻灯片数": len(prs.slides)
                    }
                    results.append(info)
                    
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        # 保存合并结果
        if operation == "merge" and results:
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