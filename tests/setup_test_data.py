# -*- coding: utf-8 -*-
"""setup_test_data.py - 生成/补全 Tank Smart 测试所需的 fixture 数据。
幂等：已存在则跳过，缺失则补齐。
- test_data/sample.txt        英文样例文本（T-53 单词统计）
- test_data/sales_data.xlsx   部门/姓名/销售额/月份（T-43~T-50）
- test_data/file1.xlsx        合并源1（T-48）
- test_data/file2.xlsx        合并源2（T-48）
"""
import os
import csv
import openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DATA = os.path.join(BASE, "test_data")


def _write_sample_txt():
    """写入英文样例文本"""
    p = os.path.join(TEST_DATA, "sample.txt")
    text = (
        "Tank Smart is an industrial-grade agent testing framework.\n"
        "It executes real agent runs and scores them strictly by actual feedback.\n"
        "Every test case replays a single user request against the live agent.\n"
        "Assertions are built only from the agent's real tool results and artifacts.\n"
        "We never mock the agent. Real execution is the only source of truth.\n"
    )
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    print("sample.txt written:", p)


def _write_sales_csv():
    """写入销售数据 CSV"""
    p = os.path.join(TEST_DATA, "sales_data.csv")
    rows = [
        ["部门", "姓名", "销售额", "月份"],
        ["销售部", "张三", "15000", "Q1"],
        ["技术部", "李四", "8000", "Q1"],
        ["市场部", "王五", "12000", "Q1"],
        ["销售部", "赵六", "20000", "Q1"],
        ["技术部", "孙七", "6000", "Q1"],
        ["市场部", "周八", "9000", "Q1"],
    ]
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)
    print("sales_data.csv written:", p)


def _write_sales_xlsx():
    """写入销售数据 Excel"""
    p = os.path.join(TEST_DATA, "sales_data.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售数据"
    ws.append(["部门", "姓名", "销售额", "月份"])
    data = [
        ["销售部", "张三", 15000, "Q1"],
        ["技术部", "李四", 8000, "Q1"],
        ["市场部", "王五", 12000, "Q1"],
        ["销售部", "赵六", 20000, "Q1"],
        ["技术部", "孙七", 6000, "Q1"],
        ["市场部", "周八", 9000, "Q1"],
    ]
    for r in data:
        ws.append(r)
    wb.save(p)
    print("sales_data.xlsx written:", p)


def _write_file(path, rows):
    """将行数据写入指定 Excel 文件"""
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    print("written:", path)


def main():
    """幂等生成全部测试 fixture 数据"""
    os.makedirs(TEST_DATA, exist_ok=True)
    _write_sample_txt()
    _write_sales_csv()
    _write_sales_xlsx()
    _write_file(
        os.path.join(TEST_DATA, "file1.xlsx"),
        [["姓名", "年龄", "城市"], ["测试A", 25, "北京"], ["测试B", 30, "上海"]],
    )
    _write_file(
        os.path.join(TEST_DATA, "file2.xlsx"),
        [["姓名", "年龄", "城市"], ["测试C", 35, "广州"], ["测试D", 40, "深圳"]],
    )


if __name__ == "__main__":
    main()
