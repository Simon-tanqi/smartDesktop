"""文件操作工具：扫描、移动、复制、解析文件夹"""
import os
import json
import shutil
import datetime

from . import register_tool, path_params


def _create_excel_copy(source_path: str) -> str:
    """复制Excel源文件到副本，返回副本文件路径。
    
    副本命名规则: {原文件名}_copy_{时间戳}.xlsx
    副本与源文件在同一目录下。
    如果源文件不存在，返回None。
    """
    if not os.path.exists(source_path):
        return None
    
    dir_name = os.path.dirname(source_path)
    base_name = os.path.basename(source_path)
    name_without_ext, ext = os.path.splitext(base_name)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    copy_name = f"{name_without_ext}_copy_{timestamp}{ext}"
    copy_path = os.path.join(dir_name, copy_name)
    
    shutil.copy2(source_path, copy_path)
    return copy_path


@register_tool(timeout=30)
@path_params("path")  # 确保路径参数被标记，便于安全审计
def scan_directory(path: str, max_results: int = 200):
    """
    扫描指定路径下的第一层文件和文件夹，返回 JSON 对象。
    
    Args:
        path: 要扫描的目录绝对路径（读操作不限制 workspace，任意位置可扫描）
        max_results: 最大返回条目数，默认 200，防止上下文溢出
    """
    # 1. 路径安全校验（读操作放开 workspace：允许任意位置读取）
    from .code_tools import _resolve_path_read
    target_path = _resolve_path_read(path)
    if not target_path:
        return json.dumps({
            "success": False,
            "message": f"非法路径或路径不在 workspace 范围内: {path}"
        }, ensure_ascii=False)

    try:
        entries = []
        total_count = 0
        truncated = False
        
        with os.scandir(target_path) as iterator:
            for entry in iterator:
                total_count += 1
                if len(entries) < max_results:
                    entries.append(entry.name)
                else:
                    truncated = True
                    break  # 达到上限后停止遍历，节省性能
        
        # 2. 统一返回格式（必须有 success 键）
        return json.dumps({
            "success": True,
            "data": entries,
            "total": total_count,
            "truncated": truncated,
            "message": f"扫描成功，共 {total_count} 项，返回 {len(entries)} 项" + ("（已截断）" if truncated else "")
        }, ensure_ascii=False)
        
    except PermissionError:
        return json.dumps({
            "success": False,
            "message": f"权限不足，无法访问目录: {target_path}"
        }, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({
            "success": False,
            "message": f"目录不存在: {target_path}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"扫描失败: {str(e)}"
        }, ensure_ascii=False)

@register_tool(timeout=30)
def resolve_folder(parent_dir: str, target_name: str):
    """在 parent_dir 下查找所有子文件夹，返回候选列表及匹配提示。"""
    if not os.path.isdir(parent_dir):
        return json.dumps({"success": False, "message": f"父目录不存在: {parent_dir}"})
    try:
        folders = [d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d))]
        return json.dumps({"parent": parent_dir, "target_name": target_name, "folders": folders}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


# ==================== 文件复制工具 ====================

@register_tool(timeout=30)
def copy_file_to_workspace(source_path: str, dest_filename: str = None):
    """将外部文件复制到工作区（workspace）。source_path: 源文件绝对路径, dest_filename: 目标文件名（可选，默认使用源文件名）"""
    from .code_tools import _ensure_workspace
    
    try:
        if not os.path.exists(source_path):
            return json.dumps({"success": False, "message": f"源文件不存在: {source_path}"}, ensure_ascii=False)
        
        workspace_root = _ensure_workspace()
        
        if not dest_filename:
            dest_filename = os.path.basename(source_path)
        
        dest_path = os.path.join(workspace_root, dest_filename)
        
        if os.path.exists(dest_path):
            source_stat = os.stat(source_path)
            dest_stat = os.stat(dest_path)
            
            if source_stat.st_size == dest_stat.st_size and source_stat.st_mtime <= dest_stat.st_mtime:
                return json.dumps({
                    "success": True,
                    "message": f"工作区已存在相同文件，跳过复制",
                    "source_path": source_path,
                    "dest_path": dest_path,
                    "copied": False
                }, ensure_ascii=False)
        
        shutil.copy2(source_path, dest_path)
        
        return json.dumps({
            "success": True,
            "message": f"文件已复制到工作区",
            "source_path": source_path,
            "dest_path": dest_path,
            "copied": True
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"复制失败: {str(e)}"}, ensure_ascii=False)


# ==================== Excel 高级处理工具 ====================

@register_tool(timeout=60)
def excel_pivot(file_path: str, index_cols: list, value_cols: list, agg_func: str = "sum", sheet_name: str = "透视表"):
    """创建Excel透视表。file_path: Excel文件路径, index_cols: 行索引列名列表, value_cols: 数值列名列表, agg_func: 聚合函数(sum/mean/count), sheet_name: 输出工作表名"""
    import pandas as pd

    try:
        copy_path = _create_excel_copy(file_path)
        if not copy_path:
            return json.dumps({"success": False, "message": f"源文件不存在: {file_path}"}, ensure_ascii=False)
        
        df = pd.read_excel(copy_path)
        print(f"数据形状: {df.shape}")
        print(f"列名: {list(df.columns)}")
        
        pivot_table = pd.pivot_table(
            df,
            index=index_cols,
            values=value_cols,
            aggfunc=agg_func
        )
        
        with pd.ExcelWriter(copy_path, mode='a', engine='openpyxl') as writer:
            pivot_table.to_excel(writer, sheet_name=sheet_name)
        
        result = {
            "success": True,
            "message": f"透视表创建成功，已保存到副本文件",
            "source_file": file_path,
            "output_file": copy_path,
            "shape": pivot_table.shape,
            "summary": pivot_table.head(10).to_dict()
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
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
        copy_path = _create_excel_copy(file_path)
        if not copy_path:
            return json.dumps({"success": False, "message": f"源文件不存在: {file_path}"}, ensure_ascii=False)
        
        wb = openpyxl.load_workbook(copy_path)
        
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = wb.active
        
        print(f"正在格式化工作表: {ws.title}")
        
        if default_ops["header_style"]:
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            for cell in ws[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
        
        if default_ops["auto_filter"]:
            ws.auto_filter.ref = ws.dimensions
        
        if default_ops["column_width"]:
            for column in ws.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except Exception:
                        pass
                adjusted_width = min(max_length + 2, 50)
                ws.column_dimensions[column_letter].width = adjusted_width
        
        if default_ops["freeze_header"]:
            ws.freeze_panes = "A2"
        
        wb.save(copy_path)
        
        result = {
            "success": True,
            "message": f"工作表 '{ws.title}' 格式化完成",
            "source_file": file_path,
            "output_file": copy_path,
            "operations_applied": [k for k, v in default_ops.items() if v]
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"格式化错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def excel_chart_advanced(file_path: str, chart_config: dict):
    """高级Excel图表生成。file_path: Excel文件路径, chart_config: 图表配置字典"""
    import openpyxl
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList

    try:
        copy_path = _create_excel_copy(file_path)
        if not copy_path:
            return json.dumps({"success": False, "message": f"源文件不存在: {file_path}"}, ensure_ascii=False)
        
        wb = openpyxl.load_workbook(copy_path)
        
        chart_type = chart_config.get('type', 'bar')
        chart_title = chart_config.get('title', '数据图表')
        data_sheet = chart_config.get('data_sheet', None)
        
        if data_sheet and data_sheet in wb.sheetnames:
            ws = wb[data_sheet]
        else:
            ws = wb.active
        
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
        
        min_col = chart_config.get('min_col', 2)
        min_row = chart_config.get('min_row', 1)
        max_col = chart_config.get('max_col', ws.max_column)
        max_row = chart_config.get('max_row', ws.max_row)
        
        data = Reference(ws, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
        cats = Reference(ws, min_col=min_col-1, min_row=min_row+1, max_row=max_row)
        
        chart.add_data(data, titles_from_data=True)
        if chart_type != "pie":
            chart.set_categories(cats)
        
        chart_sheet_name = chart_config.get('sheet_name', f"{chart_title}_{chart_type}")
        if chart_sheet_name in wb.sheetnames:
            del wb[chart_sheet_name]
        
        chart_ws = wb.create_sheet(chart_sheet_name)
        chart_ws.add_chart(chart, "A1")
        
        wb.save(copy_path)
        
        result = {
            "success": True,
            "message": f"图表已生成并保存到工作表: {chart_sheet_name}",
            "source_file": file_path,
            "output_file": copy_path,
            "chart_sheet": chart_sheet_name,
            "chart_type": chart_type,
            "title": chart_title
        }
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"图表生成错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=120)
def excel_batch_process(directory: str, operations: list = None):
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
                file_info = {
                    "文件名": file_path.name,
                    "文件大小": f"{file_path.stat().st_size / 1024:.1f}KB",
                    "修改时间": pd.to_datetime(file_path.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M')
                }
                
                xl = pd.ExcelFile(file_path)
                file_info["工作表数量"] = len(xl.sheet_names)
                file_info["工作表名称"] = xl.sheet_names
                
                for sheet_name in xl.sheet_names:
                    df = pd.read_excel(file_path, sheet_name=sheet_name, nrows=5)
                    sheet_info = {
                        "工作表": sheet_name,
                        "行数": len(pd.read_excel(file_path, sheet_name=sheet_name)),
                        "列数": len(df.columns),
                        "列名": list(df.columns)
                    }
                    file_info[f"Sheet_{sheet_name}"] = sheet_info
                
                if operations:
                    copy_path = _create_excel_copy(str(file_path))
                    if not copy_path:
                        file_info["处理状态"] = "源文件不存在"
                        results.append(file_info)
                        continue
                    
                    file_info["源文件"] = str(file_path)
                    file_info["输出文件"] = copy_path
                    
                    processed_df = pd.read_excel(copy_path)
                    applied_ops = []
                    
                    for op in operations:
                        op_type = op.get('type')
                        if op_type == 'filter':
                            column = op.get('column')
                            value = op.get('value')
                            processed_df = processed_df[processed_df[column] == value]
                            applied_ops.append(f"过滤 {column}={value}")
                        elif op_type == 'sort':
                            column = op.get('column')
                            ascending = op.get('ascending', True)
                            processed_df = processed_df.sort_values(column, ascending=ascending)
                            applied_ops.append(f"排序 {column} {'升序' if ascending else '降序'}")
                        elif op_type == 'drop_na':
                            processed_df = processed_df.dropna()
                            applied_ops.append("删除空值")
                        elif op_type == 'rename_columns':
                            mapping = op.get('mapping')
                            processed_df = processed_df.rename(columns=mapping)
                            applied_ops.append(f"重命名列 {list(mapping.keys())}")
                    
                    if applied_ops:
                        file_info["应用操作"] = applied_ops
                        processed_df.to_excel(copy_path, index=False)
                        file_info["处理后行数"] = len(processed_df)
                
                results.append(file_info)
                
            except Exception as e:
                print(f"处理文件 {file_path.name} 时出错: {str(e)}")
        
        summary_df = pd.DataFrame([{
            "文件名": r["文件名"],
            "文件大小": r["文件大小"],
            "工作表数": r["工作表数量"]
        } for r in results])
        
        print("\n批量处理完成!")
        print("\n文件汇总:")
        print(summary_df)
        
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


# ==================== Excel 基础操作工具 ====================

@register_tool(timeout=60)
def excel_read(file_path: str, sheet_name: str = None, max_rows: int = 100, start_row: int = 0):
    """读取Excel数据，返回JSON格式。file_path: Excel文件路径, sheet_name: 工作表名(默认第一个), max_rows: 最大读取行数, start_row: 起始行"""
    import pandas as pd

    try:
        df = pd.read_excel(file_path, sheet_name=sheet_name, skiprows=start_row, nrows=max_rows)
        
        if isinstance(df, dict):
            first_sheet = list(df.keys())[0]
            df = df[first_sheet]
        
        result = {
            "success": True,
            "message": f"读取成功",
            "shape": {"rows": len(df), "columns": len(df.columns)},
            "columns": list(df.columns),
            "data": df.head(min(max_rows, 50)).to_dict(orient='records'),
            "total_rows": len(df)
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"读取错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def excel_write(file_path: str, data: list, sheet_name: str = "Sheet1", mode: str = "write", header: bool = True):
    """写入数据到Excel。file_path: 文件路径, data: 数据列表(字典或列表), sheet_name: 工作表名, mode: write(覆盖)/append(追加)/new_sheet(新建Sheet), header: 是否写入表头"""
    import pandas as pd

    try:
        df = pd.DataFrame(data)
        
        if mode == "write":
            df.to_excel(file_path, sheet_name=sheet_name, index=False, header=header)
        elif mode == "append":
            with pd.ExcelWriter(file_path, mode='a', engine='openpyxl', if_sheet_exists='replace') as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False, header=header)
        elif mode == "new_sheet":
            with pd.ExcelWriter(file_path, mode='a', engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name=sheet_name, index=False, header=header)
        
        result = {
            "success": True,
            "message": f"写入成功，共 {len(df)} 行",
            "rows_written": len(df),
            "columns": list(df.columns)
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"写入错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=120)
def excel_merge(file_paths: list, output_path: str, strategy: str = "concat", join_key: str = None):
    """合并多个Excel文件。file_paths: 文件路径列表, output_path: 输出文件路径, strategy: concat(纵向合并)/merge(主键合并), join_key: 合并主键(strategy=merge时必填)"""
    import pandas as pd

    try:
        if strategy == "concat":
            dfs = []
            for fp in file_paths:
                df = pd.read_excel(fp)
                df['source_file'] = os.path.basename(fp)
                dfs.append(df)
            merged_df = pd.concat(dfs, ignore_index=True)
        
        elif strategy == "merge":
            if not join_key:
                return json.dumps({"success": False, "message": "merge策略需要指定join_key"}, ensure_ascii=False)
            
            merged_df = pd.read_excel(file_paths[0])
            for fp in file_paths[1:]:
                df = pd.read_excel(fp)
                merged_df = pd.merge(merged_df, df, on=join_key, how='outer')
        
        merged_df.to_excel(output_path, index=False)
        
        result = {
            "success": True,
            "message": f"合并成功，共 {len(merged_df)} 行",
            "source_files": file_paths,
            "output_file": output_path,
            "files_merged": len(file_paths),
            "rows": len(merged_df),
            "columns": list(merged_df.columns)
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"合并错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def excel_convert(input_path: str, output_format: str = "csv"):
    """Excel格式转换。input_path: 输入文件路径(.xlsx/.xls/.csv), output_format: csv/excel"""
    import pandas as pd

    try:
        file_ext = os.path.splitext(input_path)[1].lower()
        
        if file_ext in ['.xlsx', '.xls']:
            df = pd.read_excel(input_path)
            if output_format == "csv":
                output_path = os.path.splitext(input_path)[0] + ".csv"
                df.to_csv(output_path, index=False, encoding='utf-8-sig')
            else:
                output_path = input_path
        elif file_ext == '.csv':
            df = pd.read_csv(input_path)
            if output_format == "excel":
                output_path = os.path.splitext(input_path)[0] + ".xlsx"
                df.to_excel(output_path, index=False)
            else:
                output_path = input_path
        
        result = {
            "success": True,
            "message": f"转换完成",
            "input_file": input_path,
            "output_file": output_path,
            "rows": len(df),
            "columns": len(df.columns)
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"转换错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def excel_formula(file_path: str, sheet_name: str, cell: str, formula: str):
    """插入Excel公式。file_path: 文件路径, sheet_name: 工作表名, cell: 单元格位置(如A1), formula: 公式(如=SUM(B1:B10))"""
    import openpyxl

    try:
        copy_path = _create_excel_copy(file_path)
        if not copy_path:
            return json.dumps({"success": False, "message": f"源文件不存在: {file_path}"}, ensure_ascii=False)
        
        wb = openpyxl.load_workbook(copy_path)
        ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active
        
        ws[cell] = formula
        wb.save(copy_path)
        
        result = {
            "success": True,
            "message": f"公式已插入到 {sheet_name}!{cell}",
            "source_file": file_path,
            "output_file": copy_path,
            "formula": formula
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"公式插入错误: {str(e)}"}, ensure_ascii=False)


@register_tool(timeout=60)
def excel_conditional_format(file_path: str, sheet_name: str, cell_range: str, condition_type: str, rule: dict, style: dict = None):
    """设置条件格式。file_path: 文件路径, sheet_name: 工作表名, cell_range: 单元格范围(如A1:C10), condition_type: cellIs/greaterThan/lessThan/between/containsText, rule: 条件规则, style: 样式"""
    import openpyxl
    from openpyxl.formatting.rule import FormulaRule, CellIsRule
    from openpyxl.styles import PatternFill, Font, Border, Side

    try:
        copy_path = _create_excel_copy(file_path)
        if not copy_path:
            return json.dumps({"success": False, "message": f"源文件不存在: {file_path}"}, ensure_ascii=False)
        
        wb = openpyxl.load_workbook(copy_path)
        ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.active
        
        default_style = {
            "fill_color": "FFFF00",
            "font_color": "FF0000",
            "bold": False
        }
        if style:
            default_style.update(style)
        
        fill = PatternFill(start_color=default_style["fill_color"], end_color=default_style["fill_color"], fill_type="solid")
        font = Font(color=default_style["font_color"], bold=default_style["bold"])
        
        if condition_type == "cellIs":
            rule_type = rule.get('type', 'equal')
            value = rule.get('value', 0)
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(operator=rule_type, formula=[f'"{value}"' if isinstance(value, str) else str(value)],
                          stopIfTrue=True, fill=fill, font=font)
            )
        elif condition_type == "greaterThan":
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(operator='greaterThan', formula=[str(rule.get('value', 0))],
                          stopIfTrue=True, fill=fill, font=font)
            )
        elif condition_type == "lessThan":
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(operator='lessThan', formula=[str(rule.get('value', 0))],
                          stopIfTrue=True, fill=fill, font=font)
            )
        elif condition_type == "between":
            min_val = rule.get('min', 0)
            max_val = rule.get('max', 100)
            ws.conditional_formatting.add(
                cell_range,
                CellIsRule(operator='between', formula=[str(min_val), str(max_val)],
                          stopIfTrue=True, fill=fill, font=font)
            )
        elif condition_type == "containsText":
            text = rule.get('text', '')
            ws.conditional_formatting.add(
                cell_range,
                FormulaRule(formula=[f'=ISNUMBER(SEARCH("{text}",{cell_range.split(":")[0]}))'],
                          stopIfTrue=True, fill=fill, font=font)
            )
        
        wb.save(copy_path)
        
        result = {
            "success": True,
            "message": f"条件格式已应用到 {sheet_name}!{cell_range}",
            "source_file": file_path,
            "output_file": copy_path,
            "condition_type": condition_type,
            "rule": rule
        }
        return json.dumps(result, ensure_ascii=False, default=str)
        
    except Exception as e:
        return json.dumps({"success": False, "message": f"条件格式错误: {str(e)}"}, ensure_ascii=False)