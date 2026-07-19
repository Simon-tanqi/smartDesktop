import json
p = r'C:\Users\username\AppData\Roaming\MyAgent\cached_actions.json'
data = json.loads(open(p, encoding='utf-8').read())
records = data['固化记录']
print(f'剩余 {len(records)} 条记录:')
for k, v in records.items():
    tools = [s.get('tool') for s in v.get('workflow', [])]
    sc = v.get('success_count', 0)
    kind = v.get('kind', '?')
    print(f'  - {k}: success={sc}, kind={kind}, tools={tools}')
