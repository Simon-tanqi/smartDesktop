"""Word文档处理工具：创建、编辑、格式化Word文档"""
import os
import re
import json
from pathlib import Path

from . import register_tool


try:
    from docx import Document
    from docx.shared import Inches, Mm, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    WORD_AVAILABLE = True
except ImportError:
    WORD_AVAILABLE = False

@register_tool(timeout=60)
def create_word_document(file_path: str, content: dict = None):
    """创建Word文档。直接调用即可，内部已集成所有依赖，禁止使用execute_command检查或安装python-docx。
file_path: 文件保存路径, content: 文档内容配置。
content 支持以下格式（任选一种）：

格式1（推荐）：{"sections": [{"heading": "标题", "content": [{"type": "paragraph", "text": "内容"}]}, ...]}
  - sections 是列表，每个元素有 heading(字符串标题)和 content(元素列表)
  - content 内的元素 type 支持: paragraph, heading, table

格式2（扁平 sections）：{"sections": [{"type": "heading", "text": "标题", "level": 2}, {"type": "paragraph", "text": "内容"}]}
  - sections 每个元素直接用 type 指定类型
  - type 支持: heading(level可选), paragraph, table(data:二维数组)

格式3（旧版）：{"title": "文档标题", "paragraphs": [{"text": "内容", "style": "normal"}]}
  - paragraphs 列表，style 支持 normal / bold / heading2"""
    # ========== 1. 依赖与基础参数校验 ==========
    if not WORD_AVAILABLE:
        return json.dumps({
            "success": False,
            "code": "DEPENDENCY_MISSING",
            "message": "缺少依赖库，请执行：pip install python-docx"
        }, ensure_ascii=False)

    if not isinstance(file_path, str) or not file_path.strip():
        return json.dumps({
            "success": False,
            "code": "PARAM_INVALID",
            "message": "参数错误：file_path 不能为空"
        }, ensure_ascii=False)

    file_path = file_path.strip()
    if not file_path.lower().endswith(".docx"):
        return json.dumps({
            "success": False,
            "code": "FORMAT_UNSUPPORTED",
            "message": "文件格式错误：仅支持 .docx 格式"
        }, ensure_ascii=False)

    try:
        file_path = os.path.abspath(file_path)
    except Exception:
        return json.dumps({
            "success": False,
            "code": "PATH_INVALID",
            "message": "文件路径非法"
        }, ensure_ascii=False)

    # ========== 2. 配置归一化 ==========
    # 兼容直接传列表格式（LLM 可能把 paragraphs 直接当顶层参数传）
    if isinstance(content, list):
        content = {"paragraphs": content}
    # 兼容 JSON 字符串
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            content = {}
    content = content if isinstance(content, dict) else {}
    config = {
        "meta": {"title": "新建文档", "author": "MyAgent", "subject": "", "keywords": "", "company": ""},
        "page": {"size": "A4", "orientation": "portrait",
                 "margin": {"top": 1, "bottom": 1, "left": 1.25, "right": 1.25}},
        "default_style": {"font_name": "宋体", "font_size": 12, "line_spacing": 1.5},
        "show_title": True,
        "content": []
    }

    # 兼容旧版参数结构
    if "paragraphs" in content and isinstance(content["paragraphs"], list):
        if content.get("title"):
            config["meta"]["title"] = content["title"]
        if content.get("author"):
            config["meta"]["author"] = content["author"]
        for para in content["paragraphs"]:
            if not isinstance(para, dict):
                continue
            style = para.get("style", "normal")
            if style == "heading2":
                config["content"].append({"type": "heading", "props": {"level": 2, "text": para.get("text", "")}})
            else:
                config["content"].append({
                    "type": "paragraph",
                    "props": {"text": para.get("text", ""), "style_type": style}
                })
    # 兼容 sections 格式（LLM 常用的通用结构）
    elif "sections" in content and isinstance(content["sections"], list):
        if content.get("title"):
            config["meta"]["title"] = content["title"]
        for section in content["sections"]:
            if not isinstance(section, dict):
                continue
            # 判断是扁平格式 {"type": "...", ...} 还是嵌套格式 {"heading": "...", "content": [...]}
            if "heading" in section and isinstance(section.get("heading"), str) and "content" in section:
                # 嵌套格式：heading 是字符串标题，content 是元素数组
                title = section["heading"]
                if title:
                    config["content"].append({"type": "heading", "props": {"level": 2, "text": title}})
                for item in section.get("content", []):
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "").lower()
                    props = {}
                    if item_type == "heading":
                        props["level"] = item.get("level", 2)
                        props["text"] = item.get("text", "")
                        config["content"].append({"type": "heading", "props": props})
                    elif item_type == "paragraph":
                        props["text"] = item.get("text", "")
                        config["content"].append({"type": "paragraph", "props": props})
                    elif item_type == "table":
                        data = item.get("data", [])
                        if data and isinstance(data, list) and len(data) > 0:
                            props["header"] = data[0]
                            props["rows"] = data[1:] if len(data) > 1 else []
                        config["content"].append({"type": "table", "props": props})
            else:
                # 扁平格式：直接解析 type/text/level
                sec_type = section.get("type", "").lower()
                props = {}
                if sec_type == "heading":
                    props["level"] = section.get("level", 2)
                    props["text"] = section.get("text", "")
                    align = section.get("alignment")
                    if align:
                        props["align"] = align
                    config["content"].append({"type": "heading", "props": props})
                elif sec_type == "paragraph":
                    props["text"] = section.get("text", "")
                    style_type = section.get("style", "normal")
                    if style_type != "normal":
                        props["style_type"] = style_type
                    config["content"].append({"type": "paragraph", "props": props})
                elif sec_type == "table":
                    data = section.get("data", [])
                    if data and isinstance(data, list) and len(data) > 0:
                        props["header"] = data[0]
                        props["rows"] = data[1:] if len(data) > 1 else []
                    config["content"].append({"type": "table", "props": props})
    else:
        if isinstance(content.get("meta"), dict):
            config["meta"].update(content["meta"])
        if isinstance(content.get("page"), dict):
            config["page"].update(content["page"])
        if isinstance(content.get("default_style"), dict):
            config["default_style"].update(content["default_style"])
        if "show_title" in content:
            config["show_title"] = bool(content["show_title"])
        if isinstance(content.get("content"), list):
            config["content"] = content["content"]

    # 通用兜底：所有格式检测都没匹配到内容时，智能探测 content 中的任何可用结构
    if not config["content"] and isinstance(content, dict):
        for key in content:
            val = content[key]
            if isinstance(val, list) and key not in ("meta", "page", "default_style", "sections"):
                # 尝试把未知列表按扁平 sections 解析
                for item in val:
                    if isinstance(item, dict) and "text" in item:
                        config["content"].append({
                            "type": "heading" if item.get("style") == "heading2" else "paragraph",
                            "props": {"text": item.get("text", "")}
                        })
                    elif isinstance(item, dict) and "type" in item:
                        props = {k: v for k, v in item.items() if k != "type"}
                        config["content"].append({"type": item["type"], "props": props})
                if config["content"]:
                    break
            elif isinstance(val, str) and len(val) > 10 and key not in ("title", "author"):
                # 字符串内容作为段落
                config["content"].append({"type": "paragraph", "props": {"text": val}})
                break

    try:
        # ========== 3. 文档初始化 ==========
        doc = Document()

        core_props = doc.core_properties
        core_props.title = config["meta"].get("title", "")
        core_props.author = config["meta"].get("author", "")
        core_props.subject = config["meta"].get("subject", "")
        core_props.keywords = config["meta"].get("keywords", "")
        core_props.comments = config["meta"].get("company", "")

        section = doc.sections[0]
        margin = config["page"].get("margin", {})
        section.top_margin = Inches(margin.get("top", 1))
        section.bottom_margin = Inches(margin.get("bottom", 1))
        section.left_margin = Inches(margin.get("left", 1.25))
        section.right_margin = Inches(margin.get("right", 1.25))

        # 设置横向/纵向与页面尺寸
        orientation = config["page"].get("orientation", "portrait")
        page_size = config["page"].get("size", "A4")
        if page_size == "A4":
            page_width_value = Mm(210)
            page_height_value = Mm(297)
        else:
            page_width_value = Inches(8.5)
            page_height_value = Inches(11)

        if orientation == "landscape":
            section.orientation = 1  # 横向
            section.page_width = page_height_value
            section.page_height = page_width_value
        else:
            section.orientation = 0  # 纵向
            section.page_width = page_width_value
            section.page_height = page_height_value

        # 全局默认字体
        default_font = config["default_style"]
        normal_style = doc.styles['Normal']
        normal_style.font.name = default_font.get("font_name", "宋体")
        normal_style.font.size = Pt(default_font.get("font_size", 12))
        line_spacing = default_font.get("line_spacing", 1.5)
        if line_spacing:
            normal_style.paragraph_format.line_spacing = line_spacing
            # 不设置 line_spacing_rule，python-docx 自动按倍数处理

        # 标题
        if config["show_title"] and config["meta"].get("title"):
            title_para = doc.add_heading(config["meta"]["title"], level=1)
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # ========== 4. 元素渲染 ==========
        valid_elements = 0
        for item in config["content"]:
            if not isinstance(item, dict) or "type" not in item:
                continue
            elem_type = item["type"].lower()
            props = item.get("props", {}) if isinstance(item.get("props"), dict) else {}

            if elem_type == "heading":
                level = max(1, min(9, int(props.get("level", 2))))
                text = str(props.get("text", ""))
                heading = doc.add_heading(text, level=level)
                align = props.get("align", "left")
                if align == "center":
                    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
                elif align == "right":
                    heading.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                valid_elements += 1

            elif elem_type == "paragraph":
                text = str(props.get("text", ""))
                para = doc.add_paragraph()
                run = para.add_run(text)

                style_type = props.get("style_type", "normal")
                style = props.get("style", {}) if isinstance(props.get("style"), dict) else {}
                if style_type == "bold":
                    run.bold = True
                elif style_type == "italic":
                    run.italic = True
                else:
                    run.bold = bool(style.get("bold", False))
                    run.italic = bool(style.get("italic", False))
                    run.underline = bool(style.get("underline", False))
                    if style.get("font_size"):
                        run.font.size = Pt(int(style["font_size"]))
                    if style.get("font_name"):
                        run.font.name = style["font_name"]
                    if style.get("color"):
                        try:
                            hex_color = style["color"].lstrip("#")
                            if len(hex_color) == 6:
                                r = int(hex_color[0:2], 16)
                                g = int(hex_color[2:4], 16)
                                b = int(hex_color[4:6], 16)
                                run.font.color.rgb = RGBColor(r, g, b)
                        except Exception:
                            pass

                align = props.get("align", "left")
                if align == "center":
                    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                elif align == "right":
                    para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                first_indent = props.get("first_line_indent")
                if first_indent is not None:
                    try:
                        indent_value = float(first_indent) * default_font.get("font_size", 12)
                        para.paragraph_format.first_line_indent = Pt(indent_value)
                    except (TypeError, ValueError):
                        pass
                valid_elements += 1

            elif elem_type == "list":
                ordered = bool(props.get("ordered", False))
                items = props.get("items", []) if isinstance(props.get("items"), list) else []
                for item_text in items:
                    style = "List Number" if ordered else "List Bullet"
                    doc.add_paragraph(str(item_text), style=style)
                valid_elements += 1

            elif elem_type == "table":
                header = props.get("header", []) if isinstance(props.get("header"), list) else []
                rows = props.get("rows", []) if isinstance(props.get("rows"), list) else []
                col_count = 0
                if header:
                    col_count = len(header)
                elif rows and isinstance(rows[0], list):
                    col_count = len(rows[0])
                if col_count == 0:
                    continue
                table = doc.add_table(rows=1, cols=col_count)
                table.alignment = WD_TABLE_ALIGNMENT.CENTER
                table.style = 'Table Grid'
                if header:
                    hdr_cells = table.rows[0].cells
                    for i, h in enumerate(header[:col_count]):
                        hdr_cells[i].text = str(h)
                        for run in hdr_cells[i].paragraphs[0].runs:
                            run.bold = True
                for row_data in rows:
                    if not isinstance(row_data, list):
                        continue
                    row_cells = table.add_row().cells
                    for i, cell_val in enumerate(row_data[:col_count]):
                        row_cells[i].text = str(cell_val)
                valid_elements += 1

            elif elem_type == "image":
                img_path = props.get("path", "")
                if not img_path or not os.path.exists(img_path):
                    continue
                try:
                    width = Inches(float(props.get("width", 6)))
                    doc.add_picture(img_path, width=width)
                    last_paragraph = doc.paragraphs[-1]
                    last_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    valid_elements += 1
                except Exception:
                    continue

            elif elem_type == "page_break":
                doc.add_page_break()
                valid_elements += 1

            elif elem_type == "blank_line":
                count = max(1, int(props.get("count", 1)))
                for _ in range(count):
                    doc.add_paragraph("")
                valid_elements += 1

        # ========== 5. 保存 ==========
        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        doc.save(file_path)

        try:
            file_size_kb = round(Path(file_path).stat().st_size / 1024, 1)
            file_size_str = f"{file_size_kb}KB"
        except Exception:
            file_size_str = "未知"

        return json.dumps({
            "success": True,
            "code": "SUCCESS",
            "message": f"Word文档创建成功，共渲染 {valid_elements} 个有效元素",
            "data": {
                "file_path": file_path,
                "file_size": file_size_str,
                "element_count": valid_elements
            }
        }, ensure_ascii=False)

    except PermissionError:
        return json.dumps({
            "success": False, "code": "PERMISSION_DENIED",
            "message": f"权限不足：无法写入文件 {file_path}"
        }, ensure_ascii=False)
    except OSError as e:
        return json.dumps({
            "success": False, "code": "IO_ERROR",
            "message": f"文件IO异常：{str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False, "code": "UNKNOWN_ERROR",
            "message": f"文档创建失败：{str(e)}"
        }, ensure_ascii=False)


@register_tool(timeout=60)
def add_word_content(file_path: str, content_type: str, content_config: dict):
    """向Word文档添加内容。file_path: 文档路径, content_type: 内容类型(paragraph/table/heading/image), content_config: 内容配置"""
    if not WORD_AVAILABLE:
        return json.dumps({
            "success": False, "code": "DEPENDENCY_MISSING",
            "message": "需要安装python-docx库: pip install python-docx"
        }, ensure_ascii=False)

    # file_path 空值校验
    if not isinstance(file_path, str) or not file_path.strip():
        return json.dumps({
            "success": False, "code": "PARAM_INVALID",
            "message": "参数错误：file_path 不能为空"
        }, ensure_ascii=False)

    file_path = file_path.strip()
    # .docx 后缀自动补充
    if not file_path.lower().endswith(".docx"):
        file_path += ".docx"

    # 目录自动创建
    parent_dir = os.path.dirname(os.path.abspath(file_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    # content_type 合法性校验
    ALLOWED_TYPES = {"title", "heading", "paragraph", "bullet_list", "numbered_list"}
    if not isinstance(content_type, str) or content_type not in ALLOWED_TYPES:
        return json.dumps({
            "success": False, "code": "PARAM_INVALID",
            "message": f"content_type 不合法，仅允许: {', '.join(sorted(ALLOWED_TYPES))}"
        }, ensure_ascii=False)

    # 兼容：LLM 可能传 JSON 字符串而非对象
    if isinstance(content_config, str):
        try:
            content_config = json.loads(content_config)
        except (json.JSONDecodeError, TypeError):
            content_config = {"text": content_config}
    if not isinstance(content_config, dict):
        content_config = {"text": str(content_config)}
    
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
            "code": "SUCCESS",
            "message": f"{content_type}内容添加成功",
            "content_type": content_type,
            "file_path": file_path
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({
            "success": False, "code": "UNKNOWN_ERROR",
            "message": f"添加内容失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool(timeout=60)
def extract_word_text(file_path: str):
    """提取Word文档文本内容。file_path: 文档路径"""
    if not WORD_AVAILABLE:
        return json.dumps({
            "success": False, "code": "DEPENDENCY_MISSING",
            "message": "需要安装python-docx库: pip install python-docx"
        }, ensure_ascii=False)
    
    try:
        if not os.path.exists(file_path):
            return json.dumps({
                "success": False, "code": "FILE_NOT_FOUND",
                "message": f"文件不存在: {file_path}"
            }, ensure_ascii=False)

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
            "code": "SUCCESS",
            "metadata": {
                "title": doc.core_properties.title,
                "author": doc.core_properties.author,
                "created": str(doc.core_properties.created) if doc.core_properties.created else None,
                "paragraph_count": len(paragraphs),
                "table_count": len(tables)
            },
            "paragraphs": paragraphs[:50],  # 限制返回数量避免超长
            "tables": tables[:10],
            "tables_count": len(tables),
            "summary": {
                "total_chars": sum(len(p["text"]) for p in paragraphs),
                "heading_count": sum(1 for p in paragraphs if p["is_heading"])
            }
        }
        
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({
            "success": False, "code": "UNKNOWN_ERROR",
            "message": f"提取文本失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool(timeout=60)
def search_replace_word(file_path: str, search_text: str, replace_text: str, case_sensitive: bool = False):
    """在Word文档中查找替换文本。file_path: 文档路径, search_text: 查找文本, replace_text: 替换文本, case_sensitive: 是否区分大小写"""
    if not WORD_AVAILABLE:
        return json.dumps({"success": False, "message": "需要安装python-docx库: pip install python-docx"})
    
    try:
        doc = Document(file_path)
        replacements_count = 0

        def _replace_in_paragraph(paragraph):
            nonlocal replacements_count
            full_text = paragraph.text if case_sensitive else paragraph.text.lower()
            search_target = search_text if case_sensitive else search_text.lower()
            if search_target not in full_text:
                return

            # 跨 run 替换方案：先尝试逐 run 精确定位
            found_in_single_run = False
            for run in paragraph.runs:
                run_text = run.text if case_sensitive else run.text.lower()
                if search_target in run_text:
                    if case_sensitive:
                        new_text = run.text.replace(search_text, replace_text)
                    else:
                        new_text = re.sub(re.escape(search_text), replace_text, run.text, flags=re.IGNORECASE)
                    if new_text != run.text:
                        run.text = new_text
                        replacements_count += 1
                    found_in_single_run = True

            # 跨 run 兜底：合并所有 runs 后整体替换
            if not found_in_single_run:
                combined = paragraph.text
                if case_sensitive:
                    new_combined = combined.replace(search_text, replace_text)
                else:
                    new_combined = re.sub(re.escape(search_text), replace_text, combined, flags=re.IGNORECASE)
                diff = len(re.findall(re.escape(search_text), combined, flags=0 if case_sensitive else re.IGNORECASE))
                if new_combined != combined:
                    # 清空原有 runs，写入合并后的文本
                    for run in paragraph.runs:
                        run.text = ""
                    if paragraph.runs:
                        paragraph.runs[0].text = new_combined
                    replacements_count += diff

        # 替换段落中的文本
        for paragraph in doc.paragraphs:
            _replace_in_paragraph(paragraph)

        # 替换表格中的文本
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if search_text in (cell.text if case_sensitive else cell.text.lower()):
                        for paragraph in cell.paragraphs:
                            _replace_in_paragraph(paragraph)
        
        # 保存文档
        doc.save(file_path)
        
        result = {
            "success": True,
            "code": "SUCCESS",
            "message": f"完成文本替换: '{search_text}' -> '{replace_text}'",
            "replacements_made": replacements_count,
            "file_path": file_path
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "code": "UNKNOWN_ERROR",
            "message": f"查找替换失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool(timeout=60)
def batch_word_process(directory: str, operation: str):
    """批量处理Word文档。directory: 文件夹路径, operation: 操作类型"""
    if not WORD_AVAILABLE:
        return json.dumps({
            "success": False, "code": "DEPENDENCY_MISSING",
            "message": "需要安装python-docx库: pip install python-docx"
        }, ensure_ascii=False)
    
    try:
        directory_path = Path(directory)
        if not directory_path.exists() or not directory_path.is_dir():
            return json.dumps({
                "success": False, "code": "FILE_NOT_FOUND",
                "message": f"目录不存在或无法访问: {directory}"
            }, ensure_ascii=False)
        word_files = list(directory_path.glob("*.docx"))
        
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
                    total_chinese = 0
                    total_english = 0
                    for para in doc.paragraphs:
                        text = para.text
                        total_chinese += len(re.findall(r'[\u4e00-\u9fff]', text))
                        total_english += len(re.findall(r'[a-zA-Z]+', text))

                    info = {
                        "文件名": file_path.name,
                        "中文字数": total_chinese,
                        "英文单词数": total_english,
                        "段落数": len(doc.paragraphs)
                    }
                    results.append(info)
                    
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        # 生成汇总报告
        result = {
            "success": True,
            "code": "SUCCESS",
            "operation": operation,
            "files_processed": len(results),
            "results": results
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({
            "success": False, "code": "UNKNOWN_ERROR",
            "message": f"批量处理失败: {str(e)}"
        }, ensure_ascii=False)