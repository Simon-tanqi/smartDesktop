"""一次性脚本：将已有的 user_profile.json 同步到长期记忆 .md"""
import os, json

APPDATA = os.environ.get('APPDATA', r'C:\Users\username\AppData\Roaming')
DATA_DIR = os.path.join(APPDATA, 'MyAgent')
LONG_TERM_DIR = os.path.join(DATA_DIR, 'memory', 'long_term')

profile_path = os.path.join(DATA_DIR, 'user_profile.json')
if not os.path.exists(profile_path):
    print("user_profile.json 不存在")
    exit(0)

with open(profile_path, 'r', encoding='utf-8') as f:
    profile = json.load(f)

print(f"Profile: {json.dumps(profile, ensure_ascii=False)}")

# 写 user.md
name = profile.get("name") or profile.get("nickname") or ""
lines = []
if name:
    lines.append(f"- 姓名: {name}")
if profile.get("nickname") and profile.get("name") and profile["nickname"] != profile["name"]:
    lines.append(f"- 称呼偏好: {profile['nickname']}")

user_md = os.path.join(LONG_TERM_DIR, 'user.md')
os.makedirs(os.path.dirname(user_md), exist_ok=True)
with open(user_md, 'w', encoding='utf-8') as f:
    f.write("# 用户信息\n\n")
    if lines:
        f.write("\n".join(lines) + "\n")
    else:
        f.write("(无)\n")
    f.write("\n# 偏好\n\n(无)\n")
print(f"已写入 {user_md}")

# 写 facts.md
facts_md = os.path.join(LONG_TERM_DIR, 'facts.md')
existing_facts = []
if os.path.exists(facts_md):
    with open(facts_md, 'r', encoding='utf-8') as f:
        existing_facts = f.readlines()

new_facts = []
if profile.get("background"):
    text = f"- [职业背景] {profile['background']}\n"
    if text not in existing_facts:
        new_facts.append(text)
        existing_facts.append(text)
if profile.get("hobbies"):
    text = f"- [兴趣爱好] {profile['hobbies']}\n"
    if text not in existing_facts:
        new_facts.append(text)

if new_facts:
    with open(facts_md, 'a', encoding='utf-8') as f:
        f.write("".join(new_facts))
    print(f"已追加 {len(new_facts)} 条事实到 {facts_md}")
else:
    print("facts.md 无新内容，跳过")

print("同步完成")
