"""一次性脚本：列出所有工具名"""
import os, re
tool_dir = r'D:\PyCharm Project\MyAgent\tools'
names = []
for f in os.listdir(tool_dir):
    if f.endswith('.py') and not f.startswith('_'):
        path = os.path.join(tool_dir, f)
        with open(path, encoding='utf-8') as fp:
            content = fp.read()
        # @register_tool 后面紧跟着 def name(...) 或 @register_tool(name='x') \n def name(...)
        for m in re.finditer(r'@register_tool(?:\([^)]*\))?\s*\ndef\s+(\w+)\s*\(([^)]*)\)', content):
            func_name = m.group(1)
            args = m.group(2)
            n = re.search(r'''name\s*=\s*["']([^"']+)["']''', args)
            if n:
                names.append(n.group(1))
            else:
                names.append(func_name)
for n in sorted(set(names)):
    print(n)
