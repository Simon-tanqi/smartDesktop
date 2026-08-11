# -*- coding: utf-8 -*-
"""test_orchestrator.py - Tank Smart 测试编排器（真实执行）

用法:
  python tests/test_orchestrator.py --all
  python tests/test_orchestrator.py --category 代码编写
  python tests/test_orchestrator.py --id T-52
  python tests/test_orchestrator.py --all --retry 2

特性:
  - 直接调用 backend.agent.run_agent，真实驱动 Agent（本地 Ollama 真推理 + 工具真执行）
  - 每题独立 session_id，失败自动重试最多 2 次（取最佳结果）
  - 裁判(test_assertions)只依据真实产物打分
  - 落盘 artifacts/run_{run_id}/T-XX/{input.txt,trace.log,result.json,output/}
"""
import os
import io
import sys
import json
import glob
import time
import argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from tests.test_cases_parser import parse_test_cases, CATEGORY_ORDER
from tests.real_runner import run_single_test, write_result, ARTIFACTS_ROOT
from tests.test_assertions import assert_test
from tests.confirm_gate import ConfirmGate


def _load_cases():
    """加载全部测试用例"""
    md = os.path.join(BASE, "docs", "test_cases.md")
    return parse_test_cases(md)


def select_cases(all_cases, args):
    """按参数筛选测试用例"""
    if args.ids:
        wanted = {i.strip().upper() for i in args.ids.split(",") if i.strip()}
        return [c for c in all_cases if c.id.upper() in wanted]
    if args.id:
        return [c for c in all_cases if c.id.upper() == args.id.upper()]
    if args.category:
        return [c for c in all_cases if args.category in c.category]
    return all_cases


def run_one(tc, run_id, gate, max_retry, per_case_timeout=900):
    """执行单题并评分，支持重试取最佳结果"""
    rank = {"PASS": 3, "MANUAL_REVIEW": 2, "FAIL": 1}
    best = None
    for i in range(max_retry + 1):
        rr, test_dir = _run_single_with_timeout(tc, run_id, gate, i, per_case_timeout)
        status, assertions, stats = assert_test(tc, rr)
        rr["status"] = status
        rr["assertions"] = assertions
        rr["assert_stats"] = stats
        rr["retry_count"] = i
        # 即时落盘：每次运行结束后立即写入 result.json，
        # 避免引擎/进程崩溃导致该题结果全部丢失（P2 稳定性修复）。
        write_result(test_dir, rr)
        tag = "RETRY%d" % i if i > 0 else "RUN"
        print("[%s] %s %s (%dms, %d asserts, retry=%d)" % (
            tag, tc.id, status, rr["duration_ms"], len(assertions), i), flush=True)
        if best is None:
            best = (status, rr, test_dir)
        elif rank.get(status, 0) > rank.get(best[0], 0):
            best = (status, rr, test_dir)
        if status == "PASS":
            break
    status, rr, test_dir = best
    return status, rr


def _run_single_with_timeout(tc, run_id, gate, retry_index, per_case_timeout):
    """带每题超时保护的真实执行。

    超过 per_case_timeout 秒仍未返回时，不再等待，直接构造超时 FAIL 记录落盘；
    后台 daemon 线程继续运行但不会覆盖 result.json（run_single_test 本身不写 result）。
    """
    import threading

    out = {}

    def _worker():
        """后台线程执行单题真实运行"""
        try:
            rr, test_dir = run_single_test(tc, run_id, gate, retry_index=retry_index)
            out["rr"], out["dir"] = rr, test_dir
        except Exception as e:
            out["error"] = repr(e)

    t = threading.Thread(target=_worker, name="case_%s_r%d" % (tc.id, retry_index), daemon=True)
    t.start()
    t.join(timeout=per_case_timeout)
    if t.is_alive():
        run_dir = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id)
        test_dir = os.path.join(run_dir, tc.id)
        rr = {
            "test_id": tc.id,
            "started_at": time.time(),
            "category": tc.category,
            "title": tc.title,
            "input": tc.user_input,
            "status": "pending",
            "duration_ms": per_case_timeout * 1000,
            "retry_count": retry_index,
            "final_text": "",
            "errors": ["PER_CASE_TIMEOUT: 超过 %ds 未完成，已强制终止该题" % per_case_timeout],
            "warnings": [],
            "tool_calls": [],
            "confirms": [],
            "files_created": [],
            "danger_attempts": list(gate.danger_attempts),
            "expected_result": tc.expected_result,
            "verification": tc.verification,
            "prerequisites": tc.prerequisites,
        }
        return rr, test_dir
    if "error" in out:
        raise RuntimeError("run_single_test 异常: %s" % out["error"])
    return out["rr"], out["dir"]


def run_all(args):
    """运行全部选中用例并汇总结果与报告"""
    cases = _load_cases()
    selected = select_cases(cases, args)
    if not selected:
        print("无匹配测试用例", flush=True)
        return
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    gate = ConfirmGate()
    run_dir = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id)
    os.makedirs(run_dir, exist_ok=True)

    results = []
    t0 = time.time()
    for tc in selected:
        status, rr = run_one(tc, run_id, gate, args.retry)
        results.append(rr)

    elapsed = int((time.time() - t0) * 1000)
    # 合并 run_dir 内全部 result.json（支持部分续跑合入同一 run_id）
    merged = []
    for rp in glob.glob(os.path.join(run_dir, "*", "result.json")):
        with io.open(rp, "r", encoding="utf-8") as f:
            merged.append(json.load(f))
    merged.sort(key=lambda r: r["test_id"])
    if merged:
        results = merged
    summary = {
        "run_id": run_id,
        "model": "Qwen3.5-9B (local ollama)",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(results),
        "elapsed_ms": elapsed,
        "by_status": {},
        "results": [
            {"test_id": r["test_id"], "category": r["category"], "title": r["title"],
             "status": r["status"], "duration_ms": r["duration_ms"],
             "retry_count": r.get("retry_count", 0),
             "expected_result": r.get("expected_result")}
            for r in results
        ],
    }
    for r in results:
        summary["by_status"][r["status"]] = summary["by_status"].get(r["status"], 0) + 1

    with io.open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n==== Tank Smart 测试完成 ====", flush=True)
    print("run_id=%s 总数=%d 耗时=%ds" % (run_id, len(results), elapsed // 1000), flush=True)
    print("状态分布: %s" % summary["by_status"], flush=True)

    # 生成报告
    try:
        from tests.test_reporter import generate_reports
        generate_reports(run_id)
    except Exception as e:
        print("报告生成失败: %s" % e, flush=True)
        import traceback
        traceback.print_exc()

    return summary


def main():
    """命令行入口"""
    p = argparse.ArgumentParser(description="Tank Smart 真实测试编排器")
    p.add_argument("--all", action="store_true", help="运行全部 55 题")
    p.add_argument("--category", help="按类别筛选（子串匹配）")
    p.add_argument("--id", help="按测试编号运行，如 T-52")
    p.add_argument("--ids", help="逗号分隔的测试编号列表（续跑用），如 T-32,T-33,T-34")
    p.add_argument("--retry", type=int, default=2, help="失败最大重试次数（默认2）")
    p.add_argument("--run-id", help="指定 run_id（默认时间戳）")
    args = p.parse_args()
    if not (args.all or args.category or args.id or args.ids):
        p.print_help()
        return
    run_all(args)


if __name__ == "__main__":
    main()
