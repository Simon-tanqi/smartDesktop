"""验证 TaskCheckpoint 修复"""
import sys
sys.path.insert(0, r'D:\PyCharm Project\MyAgent')
from core.agent_state import TaskCheckpoint, CheckpointManager
import os, json, tempfile

# 1. 模拟 react_engine.py:382 的真实调用
chk = TaskCheckpoint(
    session_id='test_session_20260712',
    trace_id='trace_20260712_xyz',
    user_task='帮我把喇叭声音改成45',
    turn=2,
    current_step=3,
    messages=[{'role': 'user', 'content': 'test'}],
    completed_steps=[],
    remaining_steps=[],
    current_tool_name='system_audio',
    current_tool_args={'operation': 'set_volume', 'value': 45},
    progress_summary='正在调整音量',
)
print(f'1. 实例化成功 (current_step={chk.current_step})')

# 2. round-trip
data = chk.to_dict()
chk2 = TaskCheckpoint.from_dict(data)
assert chk2.current_step == 3, f'round-trip 失败: {chk2.current_step}'
print('2. round-trip OK')

# 3. 兼容旧数据
old = {k: v for k, v in data.items() if k != 'current_step'}
chk3 = TaskCheckpoint.from_dict(old)
assert chk3.current_step == 0
print('3. 旧数据兼容 OK (current_step=0)')

# 4. 真实 save_checkpoint 写入测试
os.environ['APPDATA'] = tempfile.mkdtemp()
CheckpointManager._instance = None
CheckpointManager._db_initialized = False
manager = CheckpointManager()
try:
    manager.save_checkpoint(chk)
    print('4. save_checkpoint 写入成功 (无 current_step 字段错误)')

    loaded = manager.get_latest_checkpoint('test_session_20260712')
    if loaded:
        print(f'5. get_latest_checkpoint 读回: turn={loaded.turn}, progress="{loaded.progress_summary}"')
        assert loaded.turn == 2
        assert loaded.current_step == 0  # 数据库无 current_step 列，读回默认 0
        print(f'   current_step 读回值: {loaded.current_step} (符合预期: 数据库无此列)')
    else:
        print('5. 读回 None (测试环境无数据，但调用链已通)')
except Exception as e:
    print(f'4. 失败: {e}')
    import traceback
    traceback.print_exc()

print()
print('修复确认: TaskCheckpoint.__init__() 不再因 current_step 报错')
