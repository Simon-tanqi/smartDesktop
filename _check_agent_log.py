import json
path = r'C:\Users\username\AppData\Roaming\MyAgent\logs\agent_2026-08-01.jsonl'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
print(f'Total lines: {len(lines)}')

# Search for log_llm_call's self.info("LLM调用", ...) entries
# These have message == "LLM调用" (not followed by ": provider=")
hits_llmcall_logmethod = []  # From logger.py line 484 self.info("LLM调用", ...)
hits_llmcall_provider = []   # From react_engine.py line 803 info("LLM调用: provider=")
hits_llm_write_fail = []     # "LLM日志写入失败"

for i, line in enumerate(lines):
    try:
        d = json.loads(line)
        msg = d.get('message', '')
        extra = d.get('extra', {})
        if msg == 'LLM调用':
            # Could be from logger.py line 484
            turn = extra.get('turn', 'NA')
            child = extra.get('child_turn', 'NA')
            full_path = extra.get('full_path', '')
            hits_llmcall_logmethod.append((i+1, d.get('timestamp',''), turn, child, full_path, line[:200]))
        elif 'LLM调用: provider=' in msg:
            hits_llmcall_provider.append((i+1, d.get('timestamp',''), extra.get('session_id', '')))
        if 'LLM日志写入失败' in msg:
            hits_llm_write_fail.append((i+1, d.get('timestamp',''), line[:300]))
    except:
        pass

print(f'\n=== Method "LLM调用" from logger.log_llm_call self.info(): {len(hits_llmcall_logmethod)} entries ===')
for line_num, ts, turn, child, fp, content in hits_llmcall_logmethod[-20:]:
    print(f'  Line {line_num} [{ts}] turn={turn} child={child} path={fp}')

print(f'\n=== "LLM调用: provider=" from react_engine.info(): {len(hits_llmcall_provider)} entries ===')
for line_num, ts, sid in hits_llmcall_provider[-5:]:
    print(f'  Line {line_num} [{ts}] session={sid}')

print(f'\n=== "LLM日志写入失败": {len(hits_llm_write_fail)} entries ===')
for line_num, ts, content in hits_llm_write_fail:
    print(f'  Line {line_num} [{ts}]: {content}')
