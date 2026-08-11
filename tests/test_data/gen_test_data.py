import openpyxl, csv, os, random

# Generate sales_data.xlsx
wb = openpyxl.Workbook()
ws = wb.active
ws.title = 'Sheet1'
headers = ['部门', '姓名', '销售额', '月份']
ws.append(headers)
data = [
    ['销售部', '张三', random.randint(3000, 20000), 'Q1'],
    ['销售部', '李四', random.randint(3000, 20000), 'Q1'],
    ['技术部', '王五', random.randint(5000, 15000), 'Q1'],
    ['技术部', '赵六', random.randint(5000, 15000), 'Q1'],
    ['市场部', '孙七', random.randint(2000, 12000), 'Q1'],
    ['市场部', '周八', random.randint(2000, 12000), 'Q1'],
    ['销售部', '赵九', random.randint(3000, 20000), 'Q2'],
    ['技术部', '钱十', random.randint(5000, 15000), 'Q2'],
]
for row in data:
    ws.append(row)
wb.save(r'D:\PyCharm Project\smartDesktop\tests\test_data\sales_data.xlsx')
print('sales_data.xlsx created')

# Generate sales_data.csv
with open(r'D:\PyCharm Project\smartDesktop\tests\test_data\sales_data.csv', 'w', newline='', encoding='utf-8-sig') as f:
    writer = csv.writer(f)
    writer.writerow(headers)
    for row in data:
        writer.writerow(row)
print('sales_data.csv created')

# Generate sample.txt
with open(r'D:\PyCharm Project\smartDesktop\tests\test_data\sample.txt', 'w', encoding='utf-8') as f:
    f.write('The quick brown fox jumps over the lazy dog. ')
    f.write('Artificial intelligence is transforming the world. ')
    f.write('Python is a powerful programming language. ')
    f.write('Machine learning models require large datasets. ')
    f.write('The future of technology is collaborative and intelligent.')
print('sample.txt created')

# Generate file1.xlsx and file2.xlsx for merge test
for i, name in enumerate(['file1.xlsx', 'file2.xlsx'], 1):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sheet1'
    ws.append(['姓名', '年龄', '城市'])
    ws.append([f'用户{i}1', 20+i, f'城市{i}1'])
    ws.append([f'用户{i}2', 22+i, f'城市{i}2'])
    wb.save(rf'D:\PyCharm Project\smartDesktop\tests\test_data\{name}')
    print(f'{name} created')

print('ALL TEST DATA GENERATED')
