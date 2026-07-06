"""文件操作工具：扫描、移动、复制、解析文件夹"""
import os
import json
import shutil

# from . import register_tool, path_params  # 将在函数内部导入避免循环依赖


def scan_directory(path: str):
    """扫描指定路径下的第一层文件和文件夹，返回 JSON 数组。path: 要扫描的目录绝对路径"""
    try:
        entries = [entry.name for entry in os.scandir(path)]
        return json.dumps(entries, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": f"扫描失败: {str(e)}"}, ensure_ascii=False)


def move_file(source: str, destination: str, overwrite: bool = True):
    """移动文件。source: 源文件路径, destination: 目标目录或新路径, overwrite: 是否覆盖已存在文件(默认true)"""
    source = os.path.normpath(source)
    destination = os.path.normpath(destination)
    if not os.path.isfile(source):
        return json.dumps({"success": False, "message": f"源文件不存在: {source}"})
    if os.path.isdir(destination):
        dest_file = os.path.join(destination, os.path.basename(source))
    elif os.path.isdir(os.path.dirname(destination)):
        dest_file = destination
    else:
        return json.dumps({"success": False, "message": f"目标路径无效（父目录不存在）: {destination}"})
    if os.path.exists(dest_file) and not overwrite:
        return json.dumps({"success": False, "message": f"目标已存在且 overwrite=False: {dest_file}"})
    try:
        shutil.move(source, dest_file)
        return json.dumps({"success": True, "message": f"成功移动到 {dest_file}"})
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


def copy_file(source: str, destination: str, overwrite: bool = True):
    """复制文件。source: 源文件路径, destination: 目标目录或新路径, overwrite: 是否覆盖已存在文件(默认true)"""
    source = os.path.normpath(source)
    destination = os.path.normpath(destination)
    if not os.path.isfile(source):
        return json.dumps({"success": False, "message": f"源文件不存在: {source}"})
    if os.path.isdir(destination):
        dest_file = os.path.join(destination, os.path.basename(source))
    elif os.path.isdir(os.path.dirname(destination)):
        dest_file = destination
    else:
        return json.dumps({"success": False, "message": f"目标路径无效（父目录不存在）: {destination}"})
    if os.path.exists(dest_file) and not overwrite:
        return json.dumps({"success": False, "message": f"目标已存在且 overwrite=False: {dest_file}"})
    try:
        shutil.copy2(source, dest_file)
        return json.dumps({"success": True, "message": f"成功复制到 {dest_file}"})
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


def resolve_folder(parent_dir: str, target_name: str):
    """在 parent_dir 下查找所有子文件夹，返回候选列表及匹配提示。"""
    if not os.path.isdir(parent_dir):
        return json.dumps({"success": False, "message": f"父目录不存在: {parent_dir}"})
    try:
        folders = [d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d))]
        return json.dumps({"parent": parent_dir, "target_name": target_name, "folders": folders}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


# ==================== Excel 高级处理工具 ====================

def excel_pivot(file_path: str, index_cols: list, value_cols: list, agg_func: str = "sum"):
    """创建Excel透视表。file_path: Excel文件路径, index_cols: 行索引列名列表, value_cols: 数值列名列表, agg_func: 聚合函数(sum/mean/count)"""
    import pandas as pd

    try:
        # 读取Excel文件
        df = pd.read_excel(file_path)
        print(f"数据形状: {df.shape}")
        print(f"列名: {list(df.columns)}")
        
        # 创建透视表
        pivot_table = pd.pivot_table(
            df,
            index=index_cols,
            values=value_cols,
            aggfunc=agg_func
        )
        
        # 写入新Sheet
        with pd.ExcelWriter(file_path, mode='a', engine='openpyxl') as writer:
            pivot_table.to_excel(writer, sheet_name='透视表')
        
        result = {
            "success": True,
            "message": f"透视表创建成功，已保存到 '透视表' Sheet",
            "shape": pivot_table.shape,
            "summary": pivot_table.head(10).to_dict()
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"错误: {str(e)}"}, ensure_ascii=False)


def excel_format(file_path: str, sheet_name: str = None, operations: dict = None):
    """Excel格式化处理。file_path: 文件路径, sheet_name: 工作表名, operations: 格式化操作字典"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    
    default_ops = {
        "header_style": True,
        "auto_filter": True,
        "column_width": True,
        "freeze_header": True
    }
    if operations:
        default_ops.update(operations)
    
    try:
        # 加载工作簿
        wb = openpyxl.load_workbook(file_path)
        
        # 选择工作表
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = wb.active
        
        print(f"正在格式化工作表: {ws.title}")
        
        # 应用格式化
        if default_ops["header_style"]:
            # 标题行样式
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
        
        if default_ops["auto_filter"]:
            # 自动筛选
            ws.auto_filter.ref = ws.dimensions
        
        if default_ops["column_width"]:
            # 自动调整列宽
            for column in ws.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                ws.column_dimensions[column_letter].width = adjusted_width
        
        if default_ops["freeze_header"]:
            # 冻结首行
            ws.freeze_panes = "A2"
        
        # 保存
        wb.save(file_path)
        
        result = {
            "success": True,
            "message": f"工作表 '{ws.title}' 格式化完成",
            "operations_applied": [k for k, v in default_ops.items() if v]
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"格式化错误: {str(e)}"}, ensure_ascii=False)


def excel_chart_advanced(file_path: str, chart_config: dict):
    """高级Excel图表生成。file_path: Excel文件路径, chart_config: 图表配置字典"""
    import openpyxl
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList

    try:
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        
        # 解析配置
        chart_type = chart_config.get('type', 'bar')
        chart_title = chart_config.get('title', '数据图表')
        
        # 创建图表
        if chart_type == "bar":
            chart = BarChart()
        elif chart_type == "line":
            chart = LineChart()
        elif chart_type == "pie":
            chart = PieChart()
        else:
            chart = BarChart()
        
        chart.title = chart_title
        chart.style = 13
        
        if chart_type != "pie":
            chart.legend.position = "r"
            chart.show_blanks_as = "gap"
        
        # 添加数据
        data = Reference(ws, min_col=2, min_row=1, max_col=ws.max_column, max_row=ws.max_row)
        cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
        
        chart.add_data(data, titles_from_data=True)
        if chart_type != "pie":
            chart.set_categories(cats)
        
        # 插入图表
        chart_sheet_name = f"{chart_title}_{chart_type}"
        if chart_sheet_name in wb.sheetnames:
            del wb[chart_sheet_name]
        
        chart_ws = wb.create_sheet(chart_sheet_name)
        chart_ws.add_chart(chart, "A1")
        
        # 保存
        wb.save(file_path)
        
        result = {
            "success": True,
            "message": f"图表已生成并保存到工作表: {chart_sheet_name}",
            "chart_sheet": chart_sheet_name,
            "chart_type": chart_type,
            "title": chart_title
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"图表生成错误: {str(e)}"}, ensure_ascii=False)


def excel_batch_process(directory: str, operations: list):
    """批量处理Excel文件。directory: 文件夹路径, operations: 操作列表"""
    import os
    import pandas as pd
    from pathlib import Path

    try:
        directory_path = Path(directory)
        excel_files = list(directory_path.glob("*.xlsx")) + list(directory_path.glob("*.xls"))
        
        print(f"找到 {len(excel_files)} 个Excel文件")
        
        results = []
        for file_path in excel_files:
            try:
                # 读取文件信息
                file_info = {
                    "文件名": file_path.name,
                    "文件大小": f"{file_path.stat().st_size / 1024:.1f}KB",
                    "修改时间": pd.to_datetime(file_path.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M')
                }
                
                # 读取Excel基本信息
                xl = pd.ExcelFile(file_path)
                file_info["工作表数量"] = len(xl.sheet_names)
                file_info["工作表名称"] = xl.sheet_names
                
                # 处理每个工作表
                for sheet_name in xl.sheet_names:
                    df = pd.read_excel(file_path, sheet_name=sheet_name, nrows=5)
                    sheet_info = {
                        "工作表": sheet_name,
                        "行数": len(pd.read_excel(file_path, sheet_name=sheet_name)),
                        "列数": len(df.columns),
                        "列名": list(df.columns)
                    }
                    file_info[f"Sheet_{sheet_name}"] = sheet_info
                
                results.append(file_info)
                
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        # 生成汇总报告
        summary_df = pd.DataFrame([{
            "文件名": r["文件名"],
            "文件大小": r["文件大小"],
            "工作表数": r["工作表数量"]
        } for r in results])
        
        print("\n批量处理完成!")
        print("\n文件汇总:")
        print(summary_df)
        
        # 保存结果到新的Excel文件
        output_file = directory_path / "批量处理报告.xlsx"
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            summary_df.to_excel(writer, sheet_name='汇总', index=False)
            
            for i, result in enumerate(results):
                detail_df = pd.DataFrame([{
                    "文件名": result["文件名"],
                    "文件大小": result["文件大小"],
                    "工作表数量": result["工作表数量"],
                    "工作表名称": ", ".join(result["工作表名称"])
                }])
                detail_df.to_excel(writer, sheet_name=f"文件_{i+1}", index=False)
        
        result = {
            "success": True,
            "message": f"批量处理完成，共处理 {len(results)} 个文件",
            "files_processed": len(results),
            "output_file": str(output_file),
            "summary": summary_df.to_dict(orient='records')
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"批量处理错误: {str(e)}"}, ensure_ascii=False)