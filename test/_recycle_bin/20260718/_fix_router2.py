import pathlib

p = pathlib.Path("D:\\PyCharm Project\\MyAgent\\memory\\context_router.py")
t = p.read_text("utf-8")

# ---- 1. Change the short-check threshold from 4 to 8 ----
old_check = "if len(stripped) <= 4:"
new_check = "if len(stripped) <= 8:"
t = t.replace(old_check, new_check)

# ---- 2. Update the comment ----
old_comment = "# 情形 2：极短指令 / 续写指令 → 继承上下文"
new_comment = "# 情形 2：极短指令 / 续写指令 / 短短语 → 继承上下文"
t = t.replace(old_comment, new_comment)

# ---- 3. Update the reason string ----
old_reason = "短指令（≤4字），继承上下文"
new_reason = "短短语（≤8字），继承上下文"
t = t.replace(old_reason, new_reason)

p.write_text(t, "utf-8")
print("done")
