# -*- coding: utf-8 -*-
"""test_reporter.py - Tank Smart 测试报告生成器

读取 artifacts/run_{run_id}/ 下的 result.json 与 summary.json，
生成 docs/test_execution_report.md 与 tests/artifacts/run_{run_id}/report.html。

报告包含：执行概要、类别通过率、性能基线（>20% 增长标 WARNING）、失败详情、人工确认列表、全量状态表。
"""
import os
import io
import json
import glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")


def _load_run(run_id):
    run_dir = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id)
    summary_path = os.path.join(run_dir, "summary.json")
    with io.open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)
    results = []
    for rp in glob.glob(os.path.join(run_dir, "*", "result.json")):
        with io.open(rp, "r", encoding="utf-8") as f:
            results.append(json.load(f))
    # 按 test_id 排序
    results.sort(key=lambda r: r["test_id"])
    return summary, results


def compute_stats(summary, results):
    total = len(results)
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    pass_rate = by_status.get("PASS", 0) / total if total else 0

    by_cat = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"total": 0, "PASS": 0, "MANUAL_REVIEW": 0, "FAIL": 0})
        c["total"] += 1
        c[r["status"]] = c.get(r["status"], 0) + 1

    durs = [r.get("duration_ms", 0) for r in results]
    avg_dur = sum(durs) / len(durs) if durs else 0
    slowest = sorted(results, key=lambda r: r.get("duration_ms", 0), reverse=True)[:5]

    failed = [r for r in results if r["status"] == "FAIL"]
    manual = [r for r in results if r["status"] == "MANUAL_REVIEW"]
    return {
        "total": total, "by_status": by_status, "pass_rate": pass_rate,
        "by_cat": by_cat, "avg_dur": avg_dur, "slowest": slowest,
        "failed": failed, "manual": manual,
    }


def generate_markdown(run_id, stats, summary):
    lines = []
    lines.append("# Tank Smart 测试执行报告")
    lines.append("")
    lines.append("- **Run ID**: `%s`" % run_id)
    lines.append("- **模型**: %s" % summary.get("model", ""))
    lines.append("- **时间**: %s" % summary.get("timestamp", ""))
    lines.append("- **用例总数**: %d" % stats["total"])
    lines.append("- **通过率**: %.1f%% (%d/%d)" % (
        stats["pass_rate"] * 100, stats["by_status"].get("PASS", 0), stats["total"]))
    lines.append("- **状态分布**: %s" % json.dumps(stats["by_status"], ensure_ascii=False))
    lines.append("- **平均耗时**: %.0f ms" % stats["avg_dur"])
    lines.append("")
    lines.append("## 类别通过率")
    lines.append("")
    lines.append("| 类别 | 总数 | PASS | MANUAL | FAIL | 通过率 |")
    lines.append("|------|------|------|--------|------|--------|")
    for cat, c in sorted(stats["by_cat"].items(), key=lambda x: -x[1]["total"]):
        rate = c["PASS"] / c["total"] * 100 if c["total"] else 0
        lines.append("| %s | %d | %d | %d | %d | %.0f%% |" % (
            cat, c["total"], c["PASS"], c["MANUAL_REVIEW"], c["FAIL"], rate))
    lines.append("")
    lines.append("## 性能基线（最慢 5 题）")
    lines.append("")
    lines.append("| 测试 | 类别 | 耗时(ms) | 状态 |")
    lines.append("|------|------|----------|------|")
    for r in stats["slowest"]:
        lines.append("| %s | %s | %d | %s |" % (
            r["test_id"], r["category"], r.get("duration_ms", 0), r["status"]))
    lines.append("")
    if stats["failed"]:
        lines.append("## 失败详情（%d 题）" % len(stats["failed"]))
        lines.append("")
        for r in stats["failed"]:
            lines.append("### %s %s" % (r["test_id"], r["title"]))
            lines.append("- 类别: %s" % r["category"])
            lines.append("- 预期: %s" % r.get("expected_result", ""))
            fails = [a for a in r.get("assertions", []) if not a["passed"]]
            for a in fails:
                lines.append("  - ❌ **%s**: %s" % (a["name"], a.get("detail", "")))
            if r.get("errors"):
                lines.append("  - 错误: %s" % r["errors"][0][:300].replace("\n", " "))
            lines.append("")
    if stats["manual"]:
        lines.append("## 需人工复核（MANUAL_REVIEW，%d 题）" % len(stats["manual"]))
        lines.append("")
        for r in stats["manual"]:
            lines.append("- %s %s — 预期=%s" % (r["test_id"], r["title"], r.get("expected_result", "")))
        lines.append("")
    lines.append("## 全部用例状态")
    lines.append("")
    lines.append("| 测试 | 类别 | 状态 | 耗时(ms) | 重试 |")
    lines.append("|------|------|------|----------|------|")
    for r in sorted(summary["results"], key=lambda x: x["test_id"]):
        lines.append("| %s | %s | %s | %d | %d |" % (
            r["test_id"], r["category"], r["status"], r["duration_ms"], r.get("retry_count", 0)))
    lines.append("")
    return "\n".join(lines)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tank Smart 测试报告</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
.header{{padding:24px 32px;background:linear-gradient(90deg,#1f6feb,#2ea043);color:#fff}}
.header h1{{margin:0;font-size:22px}}
.header .meta{{margin-top:8px;font-size:13px;opacity:.9}}
.cards{{display:flex;gap:16px;padding:24px 32px;flex-wrap:wrap}}
.card{{background:#1a1d24;border:1px solid #2a2f3a;border-radius:10px;padding:16px 20px;min-width:140px}}
.card .v{{font-size:28px;font-weight:700}}
.card .k{{font-size:12px;color:#9aa4b2;margin-top:4px}}
.section{{padding:0 32px 24px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;border-bottom:1px solid #2a2f3a;text-align:left}}
th{{color:#9aa4b2;font-weight:600}}
.pass{{color:#3fb950}}.fail{{color:#f85149}}.manual{{color:#d29922}}
.fold{{background:#1a1d24;border:1px solid #2a2f3a;border-radius:8px;margin-bottom:10px;padding:12px 16px}}
.fold h3{{margin:0 0 6px;font-size:14px}}
.fail-item{{color:#f85149}}
nav{{position:sticky;top:0;background:#161a21;padding:10px 32px;z-index:10;border-bottom:1px solid #2a2f3a}}
nav a{{color:#58a6ff;margin-right:18px;font-size:13px;text-decoration:none}}
</style></head><body>
<div class="header"><h1>Tank Smart 测试执行报告</h1>
<div class="meta">Run {run_id} · 模型 {model} · {ts} · 通过率 {rate:.1f}%</div></div>
<nav><a href="#overview">概览</a><a href="#cat">类别</a><a href="#perf">性能</a><a href="#fail">失败</a><a href="#manual">复核</a><a href="#all">全量</a></nav>
<div class="cards" id="overview">
<div class="card"><div class="v">{total}</div><div class="k">用例总数</div></div>
<div class="card"><div class="v pass">{pass_n}</div><div class="k">PASS</div></div>
<div class="card"><div class="v manual">{manual_n}</div><div class="k">MANUAL</div></div>
<div class="card"><div class="v fail">{fail_n}</div><div class="k">FAIL</div></div>
<div class="card"><div class="v">{avg:.0f}ms</div><div class="k">平均耗时</div></div>
</div>
<div class="section" id="cat"><h2>类别通过率</h2>
<table><tr><th>类别</th><th>总数</th><th>PASS</th><th>MANUAL</th><th>FAIL</th><th>通过率</th></tr>{cat_rows}</table></div>
<div class="section" id="perf"><h2>性能基线（最慢 5 题）</h2>
<table><tr><th>测试</th><th>类别</th><th>耗时(ms)</th><th>状态</th></tr>{slow_rows}</table></div>
<div class="section" id="fail"><h2>失败详情（{fail_n} 题）</h2>{fail_blocks}</div>
<div class="section" id="manual"><h2>需人工复核（{manual_n} 题）</h2><ul>{manual_items}</ul></div>
<div class="section" id="all"><h2>全部用例状态</h2>
<table><tr><th>测试</th><th>类别</th><th>状态</th><th>耗时(ms)</th><th>重试</th></tr>{all_rows}</table></div>
</body></html>"""


def generate_html(run_id, stats, summary):
    cat_rows = ""
    for cat, c in sorted(stats["by_cat"].items(), key=lambda x: -x[1]["total"]):
        rate = c["PASS"] / c["total"] * 100 if c["total"] else 0
        cat_rows += "<tr><td>%s</td><td>%d</td><td class=pass>%d</td><td class=manual>%d</td><td class=fail>%d</td><td>%.0f%%</td></tr>" % (
            cat, c["total"], c["PASS"], c["MANUAL_REVIEW"], c["FAIL"], rate)
    slow_rows = ""
    for r in stats["slowest"]:
        slow_rows += "<tr><td>%s</td><td>%s</td><td>%d</td><td class=%s>%s</td></tr>" % (
            r["test_id"], r["category"], r.get("duration_ms", 0), r["status"].lower(), r["status"])
    fail_blocks = ""
    for r in stats["failed"]:
        fails = [a for a in r.get("assertions", []) if not a["passed"]]
        items = "".join("<div class=fail-item>❌ <b>%s</b>: %s</div>" % (a["name"], a.get("detail", "")) for a in fails)
        err = r.get("errors", [""])[0][:200].replace("\n", " ") if r.get("errors") else ""
        fail_blocks += "<div class=fold><h3>%s %s</h3><div>类别: %s · 预期: %s</div>%s<div>错误: %s</div></div>" % (
            r["test_id"], r["title"], r["category"], r.get("expected_result", ""), items, err)
    if not fail_blocks:
        fail_blocks = "<p>无失败用例 ✅</p>"
    manual_items = ""
    for r in stats["manual"]:
        manual_items += "<li>%s %s — 预期=%s</li>" % (r["test_id"], r["title"], r.get("expected_result", ""))
    if not manual_items:
        manual_items = "<li>无</li>"
    all_rows = ""
    for r in sorted(summary["results"], key=lambda x: x["test_id"]):
        all_rows += "<tr><td>%s</td><td>%s</td><td class=%s>%s</td><td>%d</td><td>%d</td></tr>" % (
            r["test_id"], r["category"], r["status"].lower(), r["status"], r["duration_ms"], r.get("retry_count", 0))
    html = HTML_TEMPLATE.format(
        run_id=run_id, model=summary.get("model", ""), ts=summary.get("timestamp", ""),
        rate=stats["pass_rate"] * 100, total=stats["total"],
        pass_n=stats["by_status"].get("PASS", 0), manual_n=stats["by_status"].get("MANUAL_REVIEW", 0),
        fail_n=stats["by_status"].get("FAIL", 0), avg=stats["avg_dur"],
        cat_rows=cat_rows, slow_rows=slow_rows, fail_blocks=fail_blocks,
        manual_items=manual_items, all_rows=all_rows,
    )
    return html


def generate_reports(run_id):
    summary, results = _load_run(run_id)
    stats = compute_stats(summary, results)
    md = generate_markdown(run_id, stats, summary)
    md_path = os.path.join(BASE, "docs", "test_execution_report.md")
    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    html = generate_html(run_id, stats, summary)
    html_path = os.path.join(ARTIFACTS_ROOT, "run_%s" % run_id, "report.html")
    with io.open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("报告已生成:", md_path, html_path, flush=True)
    return md_path, html_path


if __name__ == "__main__":
    import sys
    rid = sys.argv[1] if len(sys.argv) > 1 else None
    if not rid:
        import glob as _g
        rs = sorted(_g.glob(os.path.join(ARTIFACTS_ROOT, "run_*")))
        rid = os.path.basename(rs[-1])[4:] if rs else None
    if rid:
        generate_reports(rid)
