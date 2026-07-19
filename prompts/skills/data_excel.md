# 数据处理与分析

本地 Excel/CSV 数据处理：清洗、透视、汇总、图表。

## 流程
1. **获取文件**：如果文件不在工作区内（如桌面、下载文件夹），先用 `copy_file_to_workspace` 将文件复制到工作区
2. **确认结构**：先用 `excel_read` 读取 Excel 文件查看数据结构，或直接在代码中用 pandas 读取前几行
3. **处理**：调用 `run_python` 传入 pandas/openpyxl 代码进行数据处理，或使用专用 Excel 工具（excel_pivot、excel_format、excel_chart_advanced 等）
4. **输出**：所有 Excel 工具操作都会在副本文件上进行，源文件不会被修改

## 支持库
- pandas: 数据读取、处理、透视表、写入 Excel
- openpyxl: Excel 高级操作（格式、图表、公式）
- xlsxwriter: 带图表的 Excel 写入
- numpy: 数值计算
- matplotlib/seaborn: 图表生成
- csv/json: 文件读写

## 专用 Excel 工具（推荐）
使用专用工具比直接写代码更安全，所有工具均在副本文件上操作，不会损坏源文件：

| 工具名 | 功能 | 返回结果包含 |
|--------|------|-------------|
| excel_read | 读取Excel数据（只读） | 数据内容 |
| excel_write | 写入数据到Excel（新建） | 写入行数 |
| excel_pivot | 创建透视表 | source_file, output_file |
| excel_format | 格式化Excel（样式） | source_file, output_file |
| excel_chart_advanced | 生成图表 | source_file, output_file |
| excel_merge | 合并多个Excel | source_files, output_file |
| excel_convert | 格式转换（xlsx/csv） | input_file, output_file |
| excel_formula | 插入公式 | source_file, output_file |
| excel_conditional_format | 设置条件格式 | source_file, output_file |
| excel_batch_process | 批量处理 | output_file |

**重要**：所有修改类工具都会自动创建带时间戳的副本文件（如 `data_copy_20250115_103022.xlsx`），操作在副本上进行，源文件保持不变。

## 常用操作示例

### 读取数据
```python
import pandas as pd
df = pd.read_excel('data.xlsx', sheet_name='Sheet1')
print(f'数据形状: {df.shape}')
print(f'列名: {list(df.columns)}')
print(f'前5行:\n{df.head().to_string()}')
```

### 数据清洗
```python
import pandas as pd
df = pd.read_excel('data.xlsx')
df = df.dropna()
df['日期'] = pd.to_datetime(df['日期'])
df['销售额'] = pd.to_numeric(df['销售额'], errors='coerce')
df.to_excel('cleaned_data.xlsx', index=False)
print(f'清洗完成，行数: {len(df)}')
```

### 透视表
```python
import pandas as pd
df = pd.read_excel('data.xlsx')
pivot = df.pivot_table(
    index=['部门', '月份'],
    values=['销售额', '利润'],
    aggfunc={'销售额': 'sum', '利润': 'mean'}
)
pivot.to_excel('pivot_result.xlsx', sheet_name='透视表')
print(f'透视表完成:\n{pivot.head().to_string()}')
```

### 数据汇总
```python
import pandas as pd
df = pd.read_excel('data.xlsx')
result = df.groupby('部门').agg({
    '销售额': 'sum',
    '订单数': 'count',
    '利润率': 'mean'
}).reset_index()
result.to_excel('summary.xlsx', sheet_name='部门汇总', index=False)
print(f'汇总完成:\n{result.to_string()}')
```

### 图表生成
```python
import pandas as pd
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

df = pd.read_excel('data.xlsx')
dept_sales = df.groupby('部门')['销售额'].sum()

plt.figure(figsize=(10, 6))
dept_sales.plot(kind='bar', color='skyblue')
plt.title('各部门销售额')
plt.xlabel('部门')
plt.ylabel('销售额')
plt.savefig('sales_chart.png')
print(f'图表已保存: sales_chart.png')
```

## 安全规则
- 文件读写仅限 workspace 目录
- 禁止网络访问（socket、requests、urllib）
- 禁止系统命令（subprocess、os.system）
- 禁止动态导入（__import__、importlib）
- 内存限制约 500MB，超时 30 秒
- 禁止危险操作（eval、exec、pickle、rmtree）

## 注意事项
- 使用相对路径（如 `data.xlsx`），文件会在 workspace 目录下查找
- 大文件建议先 `nrows=100` 确认结构再全量处理
- Excel 文件操作完成后会自动保存到 workspace，源文件不会被修改
- 中文文件名和内容均支持