"""追踪 2026071208_1 任务的工具调用"""
import json
with open(r'C:\Users\username\AppData\Roaming\MyAgent\logs\llm_calls\2026071208_1_新会话3.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
turn = data['turns'][0]
print(f'user_task: {turn["user_task"]}')
print(f'child_turns: {len(turn["child_turns"])}')
print()
for i, ct in enumerate(turn['child_turns']):
    tc = ct.get('tool_calls')
    ai_out = ct.get('ai_output', '') or ''
    if tc or '调用' in ai_out or '执行' in ai_out or '失败' in ai_out or '成功' in ai_out:
        print(f'=== child_turn #{i} ===')
        print(f'  ai_output: {ai_out[:400]}')
        if tc:
            print(f'  tool_calls: {json.dumps(tc, ensure_ascii=False, indent=2)[:800]}')
        tr = ct.get('tool_result', '')
        if tr:
            print(f'  tool_result: {str(tr)[:500]}')
        print()
