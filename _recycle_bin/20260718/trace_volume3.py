"""列出每个 child_turn 的 tool_calls 和 tool_result 概要"""
import json
with open(r'C:\Users\username\AppData\Roaming\MyAgent\logs\llm_calls\2026071208_1_新会话3.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
turn = data['turns'][0]
print(f'user_task: {turn["user_task"]}')
print(f'child_turns: {len(turn["child_turns"])}')
print('='*80)
for i, ct in enumerate(turn['child_turns']):
    tc = ct.get('tool_calls') or []
    ai_out = ct.get('ai_output', '') or ''
    print(f'--- turn #{i} ---')
    if tc:
        for t in tc:
            tool = t.get('tool') or t.get('name') or 'unknown'
            args = t.get('arguments', {})
            # truncate args
            if isinstance(args, dict):
                args_short = {k: (str(v)[:80] if not isinstance(v, (list, dict)) else str(v)[:80]) for k, v in args.items()}
            else:
                args_short = str(args)[:200]
            print(f'  tool_call: {tool} args={args_short}')
    tr = ct.get('tool_result')
    if tr:
        if isinstance(tr, str):
            print(f'  result: {tr[:300]}')
        elif isinstance(tr, dict):
            print(f'  result: success={tr.get("success")} message={tr.get("message", "")[:200]}')
            if 'routed_from_execute_command' in tr:
                print(f'  ROUTED FROM execute_command!')
