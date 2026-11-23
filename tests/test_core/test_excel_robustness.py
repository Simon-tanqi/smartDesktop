# -*- coding: utf-8 -*-
"""Excel 读取健壮性回归：表头探测、前导零、文本数字、公式列、副本命名。"""
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools as T  # noqa: E402

T._ensure_tool_registration()


def _read(**kwargs):
    import json
    return json.loads(T.TOOL_REGISTRY["excel_read"]["function"](**kwargs))


def _pivot(**kwargs):
    import json
    return json.loads(T.TOOL_REGISTRY["excel_pivot"]["function"](**kwargs))


def _make_title_row_xlsx(path: Path):
    """真实报表结构：合并标题行 + 制表说明 + 第 3 行表头。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "订单明细"
    ws.merge_cells("A1:C1")
    ws["A1"] = "2026 年 Q3 销售明细表"
    ws["A2"] = "制表：数据运营部"
    ws.append(["订单号", "部门", "销售额"])
    ws.append(["SO001", "销售一部", 1000])
    ws.append(["SO002", "销售二部", 2000])
    wb.save(path)


def test_header_auto_detected(tmp_path):
    f = tmp_path / "报表.xlsx"
    _make_title_row_xlsx(f)
    r = _read(file_path=str(f), max_rows=10)
    assert r["success"] is True
    assert r["header_row"] == 2
    assert r["header_auto_detected"] is True
    assert r["columns"] == ["订单号", "部门", "销售额"]
    assert r["data"][0]["部门"] == "销售一部"
    assert any("表头" in w for w in r["warnings"])


def test_explicit_header_row_wins(tmp_path):
    f = tmp_path / "报表.xlsx"
    _make_title_row_xlsx(f)
    r = _read(file_path=str(f), header_row=2)
    assert r["header_row"] == 2
    assert r["header_auto_detected"] is False


def test_total_rows_counts_real_file(tmp_path):
    f = tmp_path / "报表.xlsx"
    _make_title_row_xlsx(f)
    r = _read(file_path=str(f))
    # 2 行标题 + 1 行表头 + 2 行数据
    assert r["total_rows"] == 5


def test_leading_zero_preserved_in_csv(tmp_path):
    f = tmp_path / "客户.csv"
    f.write_text("客户,手机号\n张三,013800000000\n李四,013900000001\n", encoding="utf-8-sig")
    r = _read(file_path=str(f), max_rows=5)
    assert r["data"][0]["手机号"] == "013800000000"


def test_thousand_separator_coerced(tmp_path):
    f = tmp_path / "文本数字.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["部门", "销售额"])
    ws.append(["销售一部", "1,280,000"])
    ws.append(["销售二部", "960,000"])
    wb.save(f)
    r = _read(file_path=str(f), max_rows=5)
    assert r["data"][0]["销售额"] == 1280000


def test_pivot_sums_text_numbers(tmp_path):
    f = tmp_path / "文本数字.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["部门", "销售额"])
    ws.append(["销售一部", "1,280,000"])
    ws.append(["销售二部", "960,000"])
    wb.save(f)
    r = _pivot(file_path=str(f), index_cols=["部门"], value_cols=["销售额"], sheet_name="汇总")
    assert r["success"] is True, r.get("message")
    assert r["summary"]["销售额"]["销售一部"] == 1280000


def test_formula_column_warns(tmp_path):
    f = tmp_path / "公式.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["金额", "合计"])
    ws.append([100, None])
    ws["B2"] = "=A2*1.1"
    wb.save(f)
    r = _read(file_path=str(f))
    assert any("公式" in w for w in r["warnings"])


def test_copy_names_are_unique(tmp_path):
    from tools.file_tools import _create_excel_copy

    f = tmp_path / "源.xlsx"
    _make_title_row_xlsx(f)
    a = _create_excel_copy(str(f))
    b = _create_excel_copy(str(f))
    assert a and b and a != b
    assert Path(a).exists() and Path(b).exists()


def test_missing_column_error_is_actionable(tmp_path):
    f = tmp_path / "源.xlsx"
    _make_title_row_xlsx(f)
    r = _pivot(file_path=str(f), index_cols=["不存在"], value_cols=["销售额"], sheet_name="汇总")
    assert r["success"] is False
    assert "列不存在" in r["message"]
