"""追踪 turn #0 完整输出"""
import json
with open(r'C:\Users\username\AppData\Roaming\MyAgent\logs\llm_calls\2026071208_1_新会话3.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
turn = data['turns'][0]
ct = turn['child_turns'][0]
print('=== child_turn #0 完整内容 ===')
print('user_input:', ct.get('user_input', '')[:500])
print()
print('--- ai_thinking ---')
print((ct.get('ai_thinking') or '')[:500])
print()
print('--- ai_output ---')
print((ct.get('ai_output') or '')[:1500])
print()
print('--- tool_calls ---')
print(json.dumps(ct.get('tool_calls'), ensure_ascii=False, indent=2))
print()
print('--- tool_result ---')
print(json.dumps(ct.get('tool_result'), ensure_ascii=False, indent=2))
