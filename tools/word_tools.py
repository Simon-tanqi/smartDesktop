"""Word文档处理工具：创建、编辑、格式化Word文档"""
import os
import json
from pathlib import Path

from . import register_tool, path_params


try:
    import docx
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.section import WD_ORIENTATION
    WORD_AVAILABLE = True
except ImportError:
    WORD_AVAILABLE = False


@register_tool(timeout=60)
def create_word_document(file_path: str, content: dict = None):
    """创建Word文档。file_path: 文件保存路径, content: 文档内容配置"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    default_content = {
        "title": "新建文档",
        "author": "MyAgent",
        "paragraphs": [
            {"text": "这是一个由MyAgent生成的Word文档。", "style": "normal"},
            {"text": "您可以使用各种工具来编辑和格式化此文档。", "style": "normal"}
        ]
    }
    
    if content:
        default_content.update(content)
    
    try:
        # 创建文档
        doc = Document()
        
        # 设置文档属性
        doc.core_properties.title = default_content["title"]
        doc.core_properties.author = default_content["author"]
        
        # 添加标题
        title = doc.add_heading(default_content["title"], level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # 添加段落
        for para_config in default_content["paragraphs"]:
            paragraph = doc.add_paragraph(para_config["text"])
            
            # 设置段落样式
            if para_config.get("style") == "bold":
                for run in paragraph.runs:
                    run.bold = True
            elif para_config.get("style") == "italic":
                for run in paragraph.runs:
                    run.italic = True
            elif para_config.get("style") == "heading2":
                # 转换为标题
                paragraph.style = "Heading 2"
        
        # 保存文档
        doc.save(file_path)
        
        result = {
            "success": True,
            "message": f"Word文档已创建: {file_path}",
            "file_size": f"{Path(file_path).stat().st_size / 1024:.1f}KB"
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"创建文档失败: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def add_word_content(file_path: str, content_type: str, content_config: dict):
    """向Word文档添加内容。file_path: 文档路径, content_type: 内容类型(paragraph/table/heading/image), content_config: 内容配置"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    try:
        # 加载现有文档或创建新文档
        if os.path.exists(file_path):
            doc = Document(file_path)
        else:
            doc = Document()
        
        if content_type == "paragraph":
            paragraph = doc.add_paragraph(content_config.get("text", ""))
            
            # 设置段落样式
            if content_config.get("bold"):
                for run in paragraph.runs:
                    run.bold = True
            if content_config.get("italic"):
                for run in paragraph.runs:
                    run.italic = True
            
            # 设置对齐方式
            alignment = content_config.get("alignment", "left")
            if alignment == "center":
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif alignment == "right":
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                
        elif content_type == "heading":
            level = content_config.get("level", 1)
            heading = doc.add_heading(content_config.get("text", ""), level=level)
            
        elif content_type == "table":
            rows = content_config.get("rows", 2)
            cols = content_config.get("cols", 2)
            table = doc.add_table(rows=rows, cols=cols)
            table.style = "Table Grid"
            
            # 填充数据
            data = content_config.get("data", [])
            for i, row_data in enumerate(data):
                if i < rows:
                    for j, cell_data in enumerate(row_data):
                        if j < cols:
                            table.cell(i, j).text = str(cell_data)
                            
        elif content_type == "image":
            image_path = content_config.get("image_path")
            if image_path and os.path.exists(image_path):
                width = content_config.get("width", 6)  # 默认6英寸
                doc.add_picture(image_path, width=Inches(width))
        
        # 保存文档
        doc.save(file_path)
        
        result = {
            "success": True,
            "message": f"{content_type}内容添加成功",
            "content_type": content_type,
            "file_path": file_path
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"添加内容失败: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def extract_word_text(file_path: str, page_range: tuple = None):
    """提取Word文档文本内容。file_path: 文档路径, page_range: 页码范围(起始,结束)"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    try:
        doc = Document(file_path)
        
        # 提取所有段落文本
        paragraphs = []
        for para in doc.paragraphs:
            if para.text.strip():  # 跳过空行
                paragraphs.append({
                    "text": para.text,
                    "style_name": para.style.name if para.style else "Normal",
                    "is_heading": "Heading" in (para.style.name if para.style else "")
                })
        
        # 提取表格数据
        tables = []
        for table in doc.tables:
            table_data = []
            for row in table.rows:
                row_data = []
                for cell in row.cells:
                    row_data.append(cell.text)
                table_data.append(row_data)
            tables.append(table_data)
        
        result = {
            "success": True,
            "metadata": {
                "title": doc.core_properties.title,
                "author": doc.core_properties.author,
                "created": str(doc.core_properties.created) if doc.core_properties.created else None,
                "paragraph_count": len(paragraphs),
                "table_count": len(tables)
            },
            "paragraphs": paragraphs[:50],  # 限制返回数量避免超长
            "tables_count": len(tables),
            "summary": {
                "total_chars": sum(len(p["text"]) for p in paragraphs),
                "heading_count": sum(1 for p in paragraphs if p["is_heading"])
            }
        }
        
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"提取文本失败: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def search_replace_word(file_path: str, search_text: str, replace_text: str, case_sensitive: bool = False):
    """在Word文档中查找替换文本。file_path: 文档路径, search_text: 查找文本, replace_text: 替换文本, case_sensitive: 是否区分大小写"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    try:
        doc = Document(file_path)
        replacements_count = 0
        
        # 替换段落中的文本
        for paragraph in doc.paragraphs:
            if search_text in paragraph.text if case_sensitive else search_text.lower() in paragraph.text.lower():
                old_text = paragraph.text
                if case_sensitive:
                    new_text = old_text.replace(search_text, replace_text)
                else:
                    # 不区分大小写的替换（保持原大小写）
                    import re
                    pattern = re.compile(re.escape(search_text), re.IGNORECASE)
                    new_text = pattern.sub(replace_text, old_text)
                
                paragraph.text = new_text
                if old_text != new_text:
                    replacements_count += 1
        
        # 替换表格中的文本
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if search_text in cell.text if case_sensitive else search_text.lower() in cell.text.lower():
                        old_text = cell.text
                        if case_sensitive:
                            new_text = old_text.replace(search_text, replace_text)
                        else:
                            import re
                            pattern = re.compile(re.escape(search_text), re.IGNORECASE)
                            new_text = pattern.sub(replace_text, old_text)
                        
                        cell.text = new_text
                        if old_text != new_text:
                            replacements_count += 1
        
        # 保存文档
        doc.save(file_path)
        
        result = {
            "success": True,
            "message": f"完成文本替换: '{search_text}' -> '{replace_text}'",
            "replacements_made": replacements_count,
            "file_path": file_path
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"查找替换失败: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def batch_word_process(directory: str, operation: str, params: dict = None):
    """批量处理Word文档。directory: 文件夹路径, operation: 操作类型, params: 操作参数"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    try:
        directory_path = Path(directory)
        word_files = list(directory_path.glob("*.docx")) + list(directory_path.glob("*.doc"))
        
        results = []
        for file_path in word_files:
            try:
                doc = Document(file_path)
                
                if operation == "extract_info":
                    info = {
                        "文件名": file_path.name,
                        "段落数": len(doc.paragraphs),
                        "表格数": len(doc.tables),
                        "文件大小": f"{file_path.stat().st_size / 1024:.1f}KB",
                        "作者": doc.core_properties.author,
                        "标题": doc.core_properties.title
                    }
                    results.append(info)
                    
                elif operation == "word_count":
                    total_words = 0
                    for para in doc.paragraphs:
                        total_words += len(para.text.split())
                    
                    info = {
                        "文件名": file_path.name,
                        "字数": total_words,
                        "段落数": len(doc.paragraphs)
                    }
                    results.append(info)
                    
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        # 生成汇总报告
        result = {
            "success": True,
            "operation": operation,
            "files_processed": len(results),
            "results": results
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"批量处理失败: {str(e)}"}, ensure_ascii=False)