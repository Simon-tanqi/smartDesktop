# 数据处理与分析

本地 Excel/CSV 数据处理：清洗、透视、汇总、图表。

## 流程
1. `read_file` 确认结构 → `run_python` 处理 → 结果写回源文件新 Sheet 或输出文件
2. 大文件先 `head()` 确认结构再全量处理，用 `print()` 输出关键值

## 保存策略
- **默认**：pd.ExcelWriter(mode='a') 在源文件新建 Sheet，不另存
- **图表**：openpyxl.chart 插入新 Sheet
- **另存**：仅用户明确说「另存为」时才建新文件
