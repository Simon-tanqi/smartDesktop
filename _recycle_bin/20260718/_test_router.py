"""快速测试 BM25 上下文路由"""
import sys
sys.path.insert(0, r"D:\PyCharm Project\MyAgent")
sys.stdout.reconfigure(encoding="utf-8")

from memory.context_router import ContextSwitchDetector

detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)

def check(step, msg, expected_switch=None):
    r = detector.detect(msg)
    status = "SWITCH" if r["is_switch"] else "KEEP"
    verdict = ""
    if expected_switch is not None:
        if r["is_switch"] == expected_switch:
            verdict = " ✅"
        else:
            verdict = f" ❌ (期望={'切换' if expected_switch else '不切换'})"
    print(f"  [{step}] {msg[:40]:<42} -> {status:6s} sim={r['norm_similarity']:.3f}  terms={r.get('new_unique_terms', [])}{verdict}")
    print(f"        原因: {r['reason']}")
    detector.commit_task(msg)

print("=" * 70)
print("场景1: 卸载Qorder → 生成AI PPT（应该切换）")
print("=" * 70)
check(1, "帮我卸载Qorder")                                                      # 首次，不切换
check(2, "帮我查找Qoder的卸载程序")                                              # 同任务，不切换
check(3, "帮我把Qoder卸载了")                                                   # 同任务，不切换
check(4, "生成2026年AI发展PPT，普通人有什么机会", expected_switch=True)         # 切换 ✅

print()
print("=" * 70)
print("场景2: 生成PPT → 继续加页（不应该切换）")
print("=" * 70)
detector2 = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
check(1, "生成2026年AI发展PPT")
check(2, "再帮我加上一页AIGC应用场景", expected_switch=False)                   # 同任务 ✅
check(3, "继续", expected_switch=False)                                          # 短指令 ✅
check(4, "可以了就这样", expected_switch=False)                                 # 短指令 ✅

print()
print("=" * 70)
print("场景3: 跨领域杂项（应该切换）")
print("=" * 70)
detector3 = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
check(1, "今天天气怎么样")
check(2, "帮我写一封邮件给张总", expected_switch=True)                          # 切换 ✅
check(3, "调一下Python代码", expected_switch=True)                              # 切换 ✅

print()
print("=" * 70)
print("场景4: 同类型但不同软件（不应切换或模糊区域）")
print("=" * 70)
detector4 = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
check(1, "帮我卸载Qoder")
check(2, "把微信也卸载了吧")                                                    # 同为卸载 → 不切换
check(3, "删除Chrome浏览器")                                                    # 同为卸载 → 不切换
