"""
PPT 渲染引擎：三阶降级渲染

Tier 1 - HTML → Playwright 截图 → PPTX（截图背景，文字不可编辑，无重叠）
Tier 2 - python-pptx 带样式（彩色背景/装饰条，文字可编辑）
Tier 3 - python-pptx 白底黑字（兜底）

自动降级：Tier 1 → 失败 → Tier 2 → 失败 → Tier 3
"""

import json, os, tempfile, math
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

from .themes import THEMES, DEFAULT_THEME
from . import OUTPUT_DIR

SLIDE_W = 1920
SLIDE_H = 1080
PPTX_W_INCHES = 13.333   # 1920px @ 144dpi
PPTX_H_INCHES = 7.5       # 1080px @ 144dpi
PPTX_W = Inches(PPTX_W_INCHES)
PPTX_H = Inches(PPTX_H_INCHES)

# ── 工具函数 ──────────────────────────────────

def _px_to_emu(x: float, y: float) -> tuple:
    """将 1920×1080 坐标系中的像素坐标转为 EMU"""
    emu_x = int(x / SLIDE_W * PPTX_W_INCHES * 914400)
    emu_y = int(y / SLIDE_H * PPTX_H_INCHES * 914400)
    return emu_x, emu_y

def _px_to_wh(w: float, h: float) -> tuple:
    emu_w = int(w / SLIDE_W * PPTX_W_INCHES * 914400)
    emu_h = int(h / SLIDE_H * PPTX_H_INCHES * 914400)
    return emu_w, emu_h

def _pct_to_emu(pct: float, is_width: bool) -> int:
    """将百分比转为 EMU"""
    inches = PPTX_W_INCHES if is_width else PPTX_H_INCHES
    return int(pct / 100 * inches * 914400)

# ── 每页文本块的定位提取 ────────────────────

def _extract_text_regions(page, slide_id: int) -> list:
    """
    用 Playwright JS 提取所有 data-tb 标记文本块的 bounding box
    返回 [{id, x, y, w, h, text, font_size, font_weight, color, text_align}, ...]
    """
    js_code = r"""
    () => {
        const els = document.querySelectorAll('[data-tb]');
        const results = [];
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            
            const textContent = el.textContent || el.innerText || '';
            const cleanText = textContent.trim().replace(/[ \t\n\r]+/g, ' ');
            
            if (!cleanText) continue;
            
            let fontSize = parseFloat(style.fontSize);
            if (isNaN(fontSize)) fontSize = 16;
            
            let color = style.color;
            if (color.startsWith('rgb')) {
                const rgbParts = color.substring(4, color.length - 1).split(',');
                if (rgbParts.length >= 3) {
                    color = '#' + 
                        parseInt(rgbParts[0].trim()).toString(16).padStart(2, '0') + 
                        parseInt(rgbParts[1].trim()).toString(16).padStart(2, '0') + 
                        parseInt(rgbParts[2].trim()).toString(16).padStart(2, '0');
                }
            } else if (color.startsWith('rgba')) {
                const rgbaParts = color.substring(5, color.length - 1).split(',');
                if (rgbaParts.length >= 3) {
                    color = '#' + 
                        parseInt(rgbaParts[0].trim()).toString(16).padStart(2, '0') + 
                        parseInt(rgbaParts[1].trim()).toString(16).padStart(2, '0') + 
                        parseInt(rgbaParts[2].trim()).toString(16).padStart(2, '0');
                }
            }
            
            results.push({
                id: el.getAttribute('data-tb'),
                x: rect.x,
                y: rect.y,
                w: rect.width,
                h: rect.height,
                text: cleanText,
                font_size: fontSize,
                font_weight: style.fontWeight,
                color: color,
                text_align: style.textAlign || 'left',
            });
        }
        return results;
    }
    """
    
    regions = page.evaluate(js_code)
    
    print(f"[PPT Renderer] 第{slide_id+1}页提取到 {len(regions)} 个文本区域")
    return regions


def _hex_color(rgba_str: str) -> str:
    """将 'rgb(r,g,b)' 或 'rgba(r,g,b,a)' 转为 '#rrggbb'"""
    import re
    m = re.search(r'rgba?\((\d+),\s*(\d+),\s*(\d+)', rgba_str)
    if m:
        return '#{:02x}{:02x}{:02x}'.format(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return '#333333'


def _render_source_footnote(slide_data: dict) -> str:
    """在 slide 底部渲染数据来源标注"""
    source = slide_data.get("data_source", {})
    source_type = source.get("type", "model_knowledge")

    footnote_map = {
        "search_verified": f" 数据来源: {', '.join(source.get('urls', [])[:2])}",
        "user_provided": " 数据来源: 用户提供",
        "model_knowledge": "⚠️ 基于模型知识，未经实时验证",
        "estimated": f" 估算值: {source.get('note', '无说明')}",
    }

    footnote_text = footnote_map.get(source_type, "")
    if not footnote_text:
        return ""

    return f'''    <div class="slide-footnote" style="
        position: absolute;
        bottom: 12px;
        left: 40px;
        right: 40px;
        font-size: 10px;
        color: #999;
        border-top: 1px solid #eee;
        padding-top: 6px;
    ">
        {footnote_text}
    </div>
    '''


# ── PPTX 生成 ──────────────────────────────────

def _add_text_box(slide, region: dict):
    """在幻灯片上添加一个透明背景的文本框"""
    left = Emu(_px_to_emu(region['x'], 0)[0])
    top  = Emu(_px_to_emu(0, region['y'])[1])
    w    = Emu(_px_to_wh(region['w'], 0)[0])
    h    = Emu(_px_to_wh(0, region['h'])[1])

    txBox = slide.shapes.add_textbox(left, top, w, h)
    tf = txBox.text_frame
    tf.word_wrap = True

    p = tf.paragraphs[0]
    p.text = region['text']
    
    # 字体大小：浏览器 px → PowerPoint pt (1pt = 1/72 inch, 1px @ 96dpi = 1/96 inch)
    # 所以 pt = px * 72 / 96 = px * 0.75，但实际渲染时可能需要微调
    p.font.size = Pt(region['font_size'] * 0.72)
    
    # 设置中文字体（关键修复：确保中文正常显示）
    try:
        p.font.name = 'Microsoft YaHei'
        if hasattr(p.font, 'east_asian_font'):
            p.font.east_asian_font.name = 'Microsoft YaHei'
    except Exception:
        p.font.name = 'Microsoft YaHei'
    
    # 颜色
    hex_color = _hex_color(region['color'])
    r, g, b = _hex_to_rgb(hex_color)
    p.font.color.rgb = RGBColor(r, g, b)
    
    p.font.bold = region['font_weight'] in ('bold', '700', '800', '900')

    if region['text_align'] == 'center':
        p.alignment = PP_ALIGN.CENTER
    elif region['text_align'] == 'right':
        p.alignment = PP_ALIGN.RIGHT

    # 完全透明：无线条、无填充（关键修复：消除重影）
    txBox.fill.background()
    txBox.line.fill.background()
    # 额外确保线条宽度为0
    txBox.line.width = Emu(0)
    
    return txBox


def _hex_to_rgb(hex_color: str):
    """#667eea → (102, 126, 234)"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _add_bg(slide, color1: str, color2: str = None):
    """设置幻灯片背景色"""
    bg = slide.background
    fill = bg.fill
    fill.solid()
    r, g, b = _hex_to_rgb(color1)
    fill.fore_color.rgb = RGBColor(r, g, b)
    if color2:
        # 叠加矩形模拟渐变底色
        r2, g2, b2 = _hex_to_rgb(color2)
        shape = slide.shapes.add_shape(
            1, Emu(0), Emu(0), PPTX_W, PPTX_H
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(r2, g2, b2)
        # 设置透明度
        from pptx.oxml.ns import qn
        spPr = shape._element.spPr
        sf = spPr.find(qn('a:solidFill'))
        if sf is not None:
            srgbClr = sf.find(qn('a:srgbClr'))
            if srgbClr is not None:
                from lxml import etree
                alpha = etree.SubElement(srgbClr, qn('a:alpha'))
                alpha.set('val', '40000')
        shape.line.fill.background()


def _add_shape_rect(slide, left_pct, top_pct, w_pct, h_pct, color: str, opacity: float = None):
    """添加装饰矩形"""
    r, g, b = _hex_to_rgb(color)
    shape = slide.shapes.add_shape(
        1,  # rectangle
        Emu(_pct_to_emu(left_pct, True)),
        Emu(_pct_to_emu(top_pct, False)),
        Emu(_pct_to_emu(w_pct, True)),
        Emu(_pct_to_emu(h_pct, False)),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(r, g, b)
    if opacity is not None:
        from pptx.oxml.ns import qn
        spPr = shape._element.spPr
        sf = spPr.find(qn('a:solidFill'))
        if sf is not None:
            srgbClr = sf.find(qn('a:srgbClr'))
            if srgbClr is not None:
                from lxml import etree
                alpha_val = str(int(opacity * 100000))
                alpha = etree.SubElement(srgbClr, qn('a:alpha'))
                alpha.set('val', alpha_val)
    shape.line.fill.background()
    return shape


def _add_styled_text(slide, text: str, left_pct, top_pct, w_pct, h_pct,
                     font_size=18, bold=False, color="#ffffff", align="left"):
    """添加带样式文本框"""
    r, g, b = _hex_to_rgb(color)
    txBox = slide.shapes.add_textbox(
        Emu(_pct_to_emu(left_pct, True)),
        Emu(_pct_to_emu(top_pct, False)),
        Emu(_pct_to_emu(w_pct, True)),
        Emu(_pct_to_emu(h_pct, False)),
    )
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(font_size)
    p.font.bold = bold
    p.font.color.rgb = RGBColor(r, g, b)
    if align == "center":
        p.alignment = PP_ALIGN.CENTER
    elif align == "right":
        p.alignment = PP_ALIGN.RIGHT
    return txBox


def _render_styled(slides_data: list, theme: dict, output_path: str) -> str:
    """Tier 2: python-pptx 带样式渲染（无外部依赖）"""
    prs = Presentation()
    prs.slide_width = PPTX_W
    prs.slide_height = PPTX_H
    blank_layout = prs.slide_layouts[6]

    c1 = theme.get("color1", "#1a1a2e")
    c2 = theme.get("color2", "#16213e")
    accent = theme.get("accent", "#667eea")

    for slide_info in slides_data:
        slide = prs.slides.add_slide(blank_layout)
        tmpl = slide_info.get("template", "content")
        data = slide_info.get("data", {})

        if tmpl == "cover":
            # 封面：深色背景 + 白色文字
            _add_bg(slide, c1, c2)
            # 顶部装饰条
            _add_shape_rect(slide, 0, 0, 100, 0.8, accent)
            _add_shape_rect(slide, 0, 99.2, 100, 0.8, accent)
            # 标题
            _add_styled_text(slide, data.get("title", ""), 8, 30, 84, 20,
                             font_size=40, bold=True, color="#ffffff", align="center")
            # 副标题
            _add_styled_text(slide, data.get("subtitle", ""), 12, 50, 76, 10,
                             font_size=22, color="#cccccc", align="center")
            # 底部信息
            if data.get("meta"):
                _add_styled_text(slide, data.get("meta", ""), 12, 92, 76, 6,
                                 font_size=12, color="#999999", align="center")
            # 页码
            slide_num = len(prs.slides)
            _add_styled_text(slide, str(slide_num), 92, 94, 6, 4,
                             font_size=10, color="#888888", align="right")
        elif tmpl == "section":
            # 章节分隔页
            _add_bg(slide, c1, c2)
            _add_shape_rect(slide, 0, 0, 100, 0.8, accent)
            _add_styled_text(slide, data.get("number", ""), 8, 20, 84, 15,
                             font_size=24, color=accent, align="center")
            _add_styled_text(slide, data.get("title", ""), 8, 35, 84, 15,
                             font_size=36, bold=True, color="#ffffff", align="center")
            _add_styled_text(slide, data.get("subtitle", ""), 12, 52, 76, 10,
                             font_size=20, color="#cccccc", align="center")
        elif tmpl == "content":
            # 内容页（列表项）
            _add_bg(slide, "#ffffff")
            _add_shape_rect(slide, 0, 0, 100, 0.6, accent)
            _add_styled_text(slide, data.get("title", ""), 5, 4, 90, 10,
                             font_size=28, bold=True, color="#1a1a2e")
            
            items = data.get("items", [])
            if items:
                y_pos = 15
                for item in items[:6]:
                    if y_pos > 90:
                        break
                    item_title = item.get("title", "")
                    item_desc = item.get("desc", "")
                    icon = item.get("icon", "")
                    if icon:
                        item_title = f"{icon} {item_title}"
                    _add_styled_text(slide, item_title, 8, y_pos, 84, 6,
                                     font_size=18, bold=True, color="#333333")
                    if item_desc:
                        _add_styled_text(slide, item_desc, 10, y_pos + 5, 80, 5,
                                         font_size=14, color="#666666")
                    y_pos += 12
            
            side = data.get("side")
            if side:
                _add_shape_rect(slide, 75, 12, 23, 76, accent, opacity=0.1)
                _add_styled_text(slide, side.get("title", ""), 77, 14, 18, 6,
                                 font_size=14, bold=True, color=accent)
                side_list = side.get("list", [])
                y_pos = 22
                for item in side_list[:5]:
                    if y_pos > 85:
                        break
                    _add_styled_text(slide, f"• {item}", 78, y_pos, 16, 4,
                                     font_size=12, color="#555555")
                    y_pos += 5
                if side.get("stat"):
                    _add_styled_text(slide, side.get("stat", ""), 78, 80, 16, 6,
                                     font_size=28, bold=True, color=accent)
                    _add_styled_text(slide, side.get("stat_label", ""), 78, 86, 16, 3,
                                     font_size=10, color="#666666")
        elif tmpl == "icon_grid":
            # 图标网格页
            _add_bg(slide, "#ffffff")
            _add_shape_rect(slide, 0, 0, 100, 0.6, accent)
            _add_styled_text(slide, data.get("title", ""), 5, 4, 90, 10,
                             font_size=28, bold=True, color="#1a1a2e")
            _add_styled_text(slide, data.get("desc", ""), 8, 12, 84, 6,
                             font_size=14, color="#666666", align="center")
            
            cols = data.get("cols", 4)
            items = data.get("items", [])
            grid_x = [(100 - cols * 22) / 2 + i * 22 for i in range(cols)]
            y_pos = 22
            
            for idx, item in enumerate(items):
                col = idx % cols
                row = idx // cols
                x_pos = grid_x[col] if col < len(grid_x) else 10 + col * 22
                item_y = y_pos + row * 20
                if item_y > 90:
                    break
                icon = item.get("icon", "📊")
                _add_styled_text(slide, icon, x_pos, item_y, 18, 6,
                                 font_size=32, align="center")
                _add_styled_text(slide, item.get("title", ""), x_pos, item_y + 6, 18, 4,
                                 font_size=14, bold=True, color="#333333", align="center")
                _add_styled_text(slide, item.get("desc", ""), x_pos, item_y + 10, 18, 3,
                                 font_size=12, color="#666666", align="center")
                if item.get("stat"):
                    _add_styled_text(slide, item.get("stat", ""), x_pos, item_y + 14, 18, 4,
                                     font_size=18, bold=True, color=accent, align="center")
        elif tmpl == "chart":
            # 数据图表页
            _add_bg(slide, "#ffffff")
            _add_shape_rect(slide, 0, 0, 100, 0.6, accent)
            _add_styled_text(slide, data.get("title", ""), 5, 4, 90, 10,
                             font_size=28, bold=True, color="#1a1a2e")
            
            bars = data.get("bars", [])
            if bars:
                _add_styled_text(slide, data.get("chart_title", "数据对比"), 8, 14, 40, 5,
                                 font_size=16, bold=True, color="#333333")
                bar_width = 70 / max(len(bars), 1)
                start_x = 15
                max_val = max(bar.get("value", 0) for bar in bars) if bars else 1
                for idx, bar in enumerate(bars):
                    x_pos = start_x + idx * bar_width
                    height = (bar.get("value", 0) / max_val) * 50 if max_val > 0 else 0
                    _add_shape_rect(slide, x_pos, 75 - height, bar_width - 2, height, accent)
                    _add_styled_text(slide, bar.get("label", ""), x_pos, 77, bar_width - 2, 3,
                                     font_size=10, color="#666666", align="center")
                    pct_val = bar.get('pct', 0)
                    if isinstance(pct_val, str) and '%' in pct_val:
                        pct_display = pct_val
                    else:
                        pct_display = f"{pct_val}%"
                    _add_styled_text(slide, pct_display, x_pos, 75 - height - 4, bar_width - 2, 3,
                                     font_size=10, bold=True, color="#333333", align="center")
            
            pie = data.get("pie")
            if pie:
                _add_styled_text(slide, pie.get("total", ""), 65, 70, 20, 6,
                                 font_size=20, bold=True, color="#333333", align="center")
                legend = pie.get("legend", [])
                y_pos = 25
                for item in legend[:5]:
                    if y_pos > 65:
                        break
                    color = item.get("color", "#cccccc")
                    _add_shape_rect(slide, 60, y_pos, 3, 3, color)
                    _add_styled_text(slide, f"{item.get('label', '')}: {item.get('value', '')}", 65, y_pos, 30, 3,
                                     font_size=12, color="#555555")
                    y_pos += 5
        else:
            # 其他模板：基础内容
            _add_bg(slide, "#ffffff")
            _add_shape_rect(slide, 0, 0, 100, 0.6, accent)
            _add_styled_text(slide, data.get("title", f"[{tmpl}]"), 5, 4, 90, 10,
                             font_size=28, bold=True, color="#1a1a2e")

    prs.save(output_path)
    return output_path


def _render_basic(slides_data: list, output_path: str) -> str:
    """Tier 3: 基础 python-pptx 白底黑字兜底"""
    prs = Presentation()
    prs.slide_width = PPTX_W
    prs.slide_height = PPTX_H

    for slide_info in slides_data:
        tmpl = slide_info.get("template", "content")
        data = slide_info.get("data", {})
        layout = prs.slide_layouts[0] if tmpl == "cover" else prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        if hasattr(slide, 'shapes') and slide.shapes.title:
            slide.shapes.title.text = data.get("title", "")
        if hasattr(slide, 'shapes') and hasattr(slide.shapes, 'placeholders'):
            for ph in slide.shapes.placeholders:
                if ph.placeholder_format.idx == 1:
                    text = ""
                    for item in data.get("items", []):
                        text += f"• {item.get('title', '')}"
                        if item.get("desc"):
                            text += f"\n  {item['desc']}"
                        text += "\n"
                    ph.text = text or data.get("subtitle", "")

    prs.save(output_path)
    return output_path


def _render_playwright(slides_data: list, theme: dict, output_path: str,
                       img_timeout: int = 30, download_timeout: int = 60) -> str:
    """Tier 1: HTML → Playwright 截图 → 双轨 PPTX（截图背景 + 可编辑文字层）"""
    from playwright.sync_api import sync_playwright

    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(),
    )

    prs = Presentation()
    prs.slide_width = PPTX_W
    prs.slide_height = PPTX_H
    blank_layout = prs.slide_layouts[6]

    slide_images = []
    text_regions = []
    temp_dirs = []

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-gpu',
                        '--disable-dev-shm-usage',
                        '--font-render-hinting=none',
                        '--force-color-profile=srgb',
                    ]
                )
            except Exception as e:
                raise RuntimeError(f"浏览器启动失败，请确保已安装 Playwright 浏览器：{e}")

            page = browser.new_page(
                viewport={"width": SLIDE_W, "height": SLIDE_H},
                device_scale_factor=1.0,
            )
            page.set_default_timeout(img_timeout * 1000)
            
            page.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})

            for idx, slide_info in enumerate(slides_data):
                tmpl_name = slide_info.get("template", "content")
                if not tmpl_name.endswith(".html"):
                    tmpl_name += ".html"

                tmpl = env.get_template(tmpl_name)
                html = tmpl.render(theme=theme, **slide_info.get("data", {}))

                # 集成数据来源脚注
                footnote_html = _render_source_footnote(slide_info)
                if footnote_html:
                    if '</body>' in html:
                        html = html.replace('</body>', footnote_html + '\n</body>')
                    else:
                        html += '\n' + footnote_html

                tmp_dir = Path(tempfile.mkdtemp())
                temp_dirs.append(tmp_dir)
                tmp_html = tmp_dir / f"slide_{idx}.html"
                tmp_html.write_text(html, encoding="utf-8")
                page.goto(tmp_html.as_uri(), wait_until="networkidle")
                
                page.evaluate("""() => {
                    return new Promise(resolve => {
                        const checkReady = () => {
                            if (document.fonts && document.fonts.ready) {
                                document.fonts.ready.then(resolve);
                            } else {
                                setTimeout(resolve, 500);
                            }
                        };
                        const images = document.querySelectorAll('img');
                        if (images.length === 0) {
                            checkReady();
                        } else {
                            let loaded = 0;
                            images.forEach(img => {
                                if (img.complete) {
                                    loaded++;
                                } else {
                                    img.onload = () => { loaded++; checkAll(); };
                                    img.onerror = () => { loaded++; checkAll(); };
                                }
                            });
                            const checkAll = () => {
                                if (loaded >= images.length) checkReady();
                            };
                            checkAll();
                        }
                    });
                }""")

                regions = _extract_text_regions(page, idx)
                text_regions.append((idx, regions))

                # 隐藏所有 data-tb 元素（用 display:none 确保完全隐藏，避免重影）
                page.evaluate("""() => {
                    const els = document.querySelectorAll('[data-tb]');
                    els.forEach(el => {
                        el.style.transition = 'none';
                        el.style.display = 'none';
                    });
                }""")
                # 等待浏览器完成重绘（确保 display:none 完全生效）
                page.wait_for_timeout(300)

                img_path = Path(output_path).parent / f"slide_{idx}.png"
                page.screenshot(
                    path=str(img_path), 
                    clip={"x": 0, "y": 0, "width": 1920, "height": 1080},
                    type='png',
                    scale='css',
                )
                slide_images.append((idx, img_path))
                print(f"[PPT Renderer] 第{idx+1}页渲染完成（{len(regions)}个文本区域）")

            browser.close()

        for idx, img_path in slide_images:
            slide = prs.slides.add_slide(blank_layout)
            slide.shapes.add_picture(str(img_path), Emu(0), Emu(0), PPTX_W, PPTX_H)
            for region in text_regions[idx][1]:
                _add_text_box(slide, region)
            os.remove(img_path)

        prs.save(output_path)
        return output_path

    finally:
        for tmp_dir in temp_dirs:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


def render(slides_data: list, theme_name: str = DEFAULT_THEME, output_path: str = None,
           img_timeout: int = 30, download_timeout: int = 60) -> str:
    """
    渲染 PPT（三阶渲染引擎）

    Tier 1（主方案）: HTML + Playwright 截图 → PPTX（视觉完美截图背景，文字不可编辑）
                     如需可编辑文字，使用 Tier 2
    Tier 2（次方案）: python-pptx 带样式渲染（彩色背景、装饰条、排版，文字可编辑）
    Tier 3（兜底）  : python-pptx 白底黑字基础渲染

    参数：
      slides_data: [
        {
          "template": "cover|content",
          "data": { ... },
        },
      ]
      theme_name: 主题名称
      output_path: 可选输出路径
      img_timeout: 外部图片加载超时时间（秒），默认30秒
      download_timeout: 图片下载超时时间（秒），默认60秒

    返回：输出文件的绝对路径
    """
    import traceback
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])

    if output_path is None:
        output_path = str(OUTPUT_DIR / "presentation.pptx")

    print(f"[PPT Renderer] 开始渲染，主题: {theme_name}, 页数: {len(slides_data)}")

    # ── Tier 1: Playwright 方案 ──
    try:
        from playwright.sync_api import sync_playwright
        _playwright_available = True
    except ImportError:
        _playwright_available = False
        print("[PPT Renderer] Playwright 未安装，跳过 Tier 1")

    if _playwright_available:
        try:
            result = _render_playwright(slides_data, theme, output_path, 
                                        img_timeout=img_timeout, download_timeout=download_timeout)
            print(f"[PPT Renderer] Tier 1 (Playwright) 渲染成功: {result}")
            return result
        except Exception as e:
            print(f"[PPT Renderer] Tier 1 (Playwright) 渲染失败: {str(e)}")
            print(f"[PPT Renderer] 降级到 Tier 2")
            traceback.print_exc()

    # ── Tier 2: 带样式 python-pptx ──
    try:
        result = _render_styled(slides_data, theme, output_path)
        print(f"[PPT Renderer] Tier 2 (带样式) 渲染成功: {result}")
        return result
    except Exception as e:
        print(f"[PPT Renderer] Tier 2 (带样式) 渲染失败: {str(e)}")
        print(f"[PPT Renderer] 降级到 Tier 3")
        traceback.print_exc()

    # ── Tier 3: 白底黑字兜底 ──
    result = _render_basic(slides_data, output_path)
    print(f"[PPT Renderer] Tier 3 (基础) 渲染完成: {result}")
    return result
