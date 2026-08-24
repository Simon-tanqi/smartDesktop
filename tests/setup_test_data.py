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
TEST_DATA = os.path.join(BASE, "tests", "test_data")


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


SAMPLE_CONFIG_CONTENT = """\
# sample_config.yaml - Agent 测试样例配置
# T04 用例目标：定位 server.timeout 并将其从 30 改为 60

app:
  name: smartDesktop
  version: 1.0.0
  debug: false

server:
  host: 127.0.0.1
  port: 8080
  timeout: 30

database:
  host: localhost
  port: 5432
  pool_size: 10
"""


def _write_sample_config():
    """写入样例配置文件（T04 配置修改用例的 fixture）"""
    p = os.path.join(TEST_DATA, "sample_config.yaml")
    if os.path.exists(p):
        print("sample_config.yaml exists, skip:", p)
        return p
    with open(p, "w", encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG_CONTENT)
    print("sample_config.yaml written:", p)
    return p


def _ensure_workspace_copy():
    """按 docs/agent_test_suite.md 约定，将 sample_config.yaml 复制到 Agent 的 workspace 目录。

    workspace 路径取 config.settings.DATA_DIR/workspace；若导入失败（未在项目根运行）
    则跳过复制，仅保留 tests/test_data 下的 fixture 源。
    幂等：workspace 副本已存在且内容一致时不重复复制。
    """
    src = os.path.join(TEST_DATA, "sample_config.yaml")
    if not os.path.exists(src):
        return
    try:
        import sys
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        from config.settings import DATA_DIR
    except Exception:
        print("skip workspace copy: cannot import config.settings")
        return
    ws_dir = os.path.join(DATA_DIR, "workspace")
    os.makedirs(ws_dir, exist_ok=True)
    dst = os.path.join(ws_dir, "sample_config.yaml")
    need_copy = True
    if os.path.exists(dst):
        try:
            with open(src, "r", encoding="utf-8") as f1, open(dst, "r", encoding="utf-8") as f2:
                need_copy = f1.read() != f2.read()
        except OSError:
            need_copy = True
    if need_copy:
        import shutil
        shutil.copy2(src, dst)
        print("sample_config.yaml copied to workspace:", dst)
    else:
        print("sample_config.yaml workspace copy up-to-date:", dst)


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
    _write_sample_config()
    _ensure_workspace_copy()


if __name__ == "__main__":
    main()
