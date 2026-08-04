"""
test_reporter.py — Tank Smart 测试报告生成器
从 artifacts/run_*/ 读取结果，生成 Markdown + HTML 双格式报告
"""
import os
import sys
import json
import datetime
import argparse
from typing import List, Dict, Any, Optional

# ── 项目路径注入 ──────────────────────────────────────────────────
PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_DIR)

ARTIFACTS_DIR = os.path.join(PROJ_DIR, 'tests', 'artifacts')
DOCS_DIR = os.path.join(PROJ_DIR, 'docs')


# ══════════════════════════════════════════════════════════════════
#  结果读取
# ══════════════════════════════════════════════════════════════════

def load_results(run_id: str) -> tuple[List[Dict], Dict]:
    """加载指定运行目录下的全部 result.json"""
    run_dir = os.path.join(ARTIFACTS_DIR, f'run_{run_id}')
    if not os.path.exists(run_dir):
        print(f'运行目录不存在: {run_dir}')
        return [], {}

    summary_path = os.path.join(run_dir, 'summary.json')
    summary = {}
    if os.path.exists(summary_path):
        with open(summary_path, 'r', encoding='utf-8') as f:
            summary = json.load(f)

    results = []
    for item in sorted(os.listdir(run_dir)):
        if item.startswith('T-'):
            result_file = os.path.join(run_dir, item, 'result.json')
            if os.path.exists(result_file):
                with open(result_file, 'r', encoding='utf-8') as f:
                    try:
                        results.append(json.load(f))
                    except json.JSONDecodeError:
                        pass

    results.sort(key=lambda r: r.get('test_id', ''))
    return results, summary


# ══════════════════════════════════════════════════════════════════
#  统计计算
# ══════════════════════════════════════════════════════════════════

def compute_stats(results: List[Dict]) -> Dict[str, Any]:
    """计算统计信息"""
    total = len(results)
    passed = [r for r in results if r.get('status') == 'PASS']
    failed = [r for r in results if r.get('status') == 'FAIL']
    manual = [r for r in results if r.get('status') == 'MANUAL_REVIEW']
    errors = [r for r in results if r.get('status') == 'ERROR']

    pass_rate = len(passed) / total * 100 if total > 0 else 0

    # 按类别统计
    categories = {}
    for r in results:
        cat = r.get('category', '未知')
        categories.setdefault(cat, {'total': 0, 'passed': 0, 'failed': 0, 'manual': 0, 'errors': 0})
        categories[cat]['total'] += 1
        s = r.get('status', '')
        if s == 'PASS':
            categories[cat]['passed'] += 1
        elif s == 'FAIL':
            categories[cat]['failed'] += 1
        elif s == 'MANUAL_REVIEW':
            categories[cat]['manual'] += 1
        else:
            categories[cat]['errors'] += 1

    # 耗时统计
    durations = [r.get('duration_ms', 0) for r in results if r.get('duration_ms', 0) > 0]
    avg_duration = sum(durations) / len(durations) if durations else 0
    max_duration = max(durations) if durations else 0
    min_duration = min(durations) if durations else 0

    # 最慢的5个用例
    slowest = sorted(results, key=lambda r: r.get('duration_ms', 0), reverse=True)[:5]

    return {
        'total': total,
        'passed': len(passed),
        'failed': len(failed),
        'manual': len(manual),
        'errors': len(errors),
        'pass_rate': f'{pass_rate:.1f}%',
        'categories': categories,
        'avg_duration_ms': avg_duration,
        'max_duration_ms': max_duration,
        'min_duration_ms': min_duration,
        'slowest': slowest,
        'passed_results': passed,
        'failed_results': failed,
    }


# ══════════════════════════════════════════════════════════════════
#  Markdown 报告生成
# ══════════════════════════════════════════════════════════════════

def generate_markdown_report(results: List[Dict], stats: Dict, run_id: str, summary: Dict) -> str:
    """生成 Markdown 格式测试报告"""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        f'# Tank Smart 功能测试执行报告',
        f'',
        f'> **项目**: Tank Smart（MyAgent）',
        f'> **运行ID**: {run_id}',
        f'> **执行时间**: {ts}',
        f'> **测试用例版本**: v1.0（55题）',
        f'',
        f'---',
        f'',
        f'## 一、执行概要',
        f'',
        f'| 指标 | 数值 |',
        f'|------|------|',
        f'| 总用例数 | {stats["total"]} |',
        f'| ✅ 通过（PASS） | {stats["passed"]} |',
        f'| ❌ 失败（FAIL） | {stats["failed"]} |',
        f'| ⚠️ 需人工确认 | {stats["manual"]} |',
        f'| 💥 执行错误 | {stats["errors"]} |',
        f'| 通过率 | **{stats["pass_rate"]}** |',
        f'| 平均耗时 | {stats["avg_duration_ms"]:.0f} ms |',
        f'| 最慢耗时 | {stats["max_duration_ms"]:.0f} ms |',
        f'',
        f'---',
        f'',
        f'## 二、类别通过率',
        f'',
        f'| 类别 | 总数 | 通过 | 失败 | 需确认 | 错误 | 通过率 |',
        f'|------|------|------|------|--------|------|--------|',
    ]

    for cat, s in sorted(stats['categories'].items()):
        rate = s['passed'] / s['total'] * 100 if s['total'] > 0 else 0
        lines.append(f'| {cat} | {s["total"]} | {s["passed"]} | {s["failed"]} | {s["manual"]} | {s["errors"]} | {rate:.1f}% |')

    lines += ['', '---', '', '## 三、失败用例详情', '']

    if stats['failed_results']:
        for r in stats['failed_results']:
            assertions = r.get('assertions', [])
            failed_asserts = [a for a in assertions if not a.get('passed')]
            lines += [
                f'### {r["test_id"]} — {r.get("title", "")}',
                f'',
                f'- **类别**: {r.get("category", "")}',
                f'- **状态**: ❌ FAIL',
                f'- **耗时**: {r.get("duration_ms", 0):.0f} ms',
                f'- **摘要**: {r.get("final_text_preview", "")[:200]}',
                f'- **失败断言** ({len(failed_asserts)}项):',
            ]
            for a in failed_asserts[:5]:
                lines.append(f'  - ❌ [{a.get("type", "")}] {a.get("message", "")}')
            lines += [
                f'- **日志文件**: `tests/artifacts/run_{run_id}/{r["test_id"]}/trace.log`',
                '',
            ]
    else:
        lines.append('🎉 所有用例均已通过！')

    lines += ['', '---', '', '## 四、性能数据', '']

    if stats['slowest']:
        lines += [
            '### 最慢的 5 个用例',
            '',
            '| 排名 | 测试ID | 标题 | 耗时(ms) |',
            '|------|--------|------|----------|',
        ]
        for i, r in enumerate(stats['slowest'], 1):
            lines.append(f'| {i} | {r["test_id"]} | {r.get("title", "")[:30]} | {r.get("duration_ms", 0):.0f} |')

    lines += ['', '---', '', '## 五、人工确认标记', '']

    manual_cases = [r for r in results if r.get('status') == 'MANUAL_REVIEW']
    if manual_cases:
        lines += [
            '| 测试ID | 标题 | 类别 | 备注 |',
            '|--------|------|------|------|',
        ]
        for r in manual_cases:
            lines.append(f'| {r["test_id"]} | {r.get("title", "")} | {r.get("category", "")} | 需要人工验证 |')
    else:
        lines.append('无')

    lines += ['', '---', '', '## 六、全部用例执行状态', '']

    lines += [
        '| 测试ID | 类别 | 标题 | 状态 | 耗时 | 断言通过/总数 |',
        '|--------|------|------|------|------|---------------|',
    ]
    for r in results:
        assertions = r.get('assertions', [])
        passed_a = sum(1 for a in assertions if a.get('passed'))
        total_a = len(assertions)
        status_icon = {'PASS': '✅', 'FAIL': '❌', 'MANUAL_REVIEW': '⚠️', 'ERROR': '💥'}.get(r.get('status', ''), '?')
        lines.append(
            f'| {r["test_id"]} | {r.get("category", "")[:8]} | {r.get("title", "")[:20]} | '
            f'{status_icon} {r.get("status", "")} | {r.get("duration_ms", 0):.0f}ms | {passed_a}/{total_a} |'
        )

    lines += [
        '',
        '---',
        '',
        f'*报告生成时间: {ts}*',
        f'*运行目录: `tests/artifacts/run_{run_id}/`*',
    ]

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════
#  HTML 报告生成
# ══════════════════════════════════════════════════════════════════

def generate_html_report(results: List[Dict], stats: Dict, run_id: str, summary: Dict) -> str:
    """生成 HTML 格式可交互报告"""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 构建类别表格行
    cat_rows = ''
    for cat, s in sorted(stats['categories'].items()):
        rate = s['passed'] / s['total'] * 100 if s['total'] > 0 else 0
        cat_rows += f'<tr><td>{cat}</td><td>{s["total"]}</td><td class="text-success">{s["passed"]}</td><td class="text-danger">{s["failed"]}</td><td class="text-warning">{s["manual"]}</td><td>{s["errors"]}</td><td><div class="progress-bar-pass" style="width:{rate:.0f}%"></div>{rate:.1f}%</td></tr>'

    # 构建失败用例行
    failed_rows = ''
    for r in stats.get('failed_results', []):
        assertions = r.get('assertions', [])
        failed_asserts = [a for a in assertions if not a.get('passed')]
        log_path = f'tests/artifacts/run_{run_id}/{r["test_id"]}/trace.log'
        trace_path = f'tests/artifacts/run_{run_id}/{r["test_id"]}/trace.log'
        failed_rows += f'''
        <div class="test-card fail">
            <div class="test-header" onclick="toggleDetail(this)">
                <span class="test-id">{r["test_id"]}</span>
                <span class="test-title">{r.get("title", "")}</span>
                <span class="test-cat">{r.get("category", "")}</span>
                <span class="badge fail">FAIL</span>
                <span class="test-time">{r.get("duration_ms", 0):.0f}ms</span>
                <span class="toggle-icon">▸</span>
            </div>
            <div class="test-detail">
                <p><strong>摘要：</strong>{r.get("final_text_preview", "")[:300]}</p>
                <details>
                    <summary>失败断言 ({len(failed_asserts)}项)</summary>
                    <ul>
                        {"".join(f'<li class="assert-fail">[{a.get("type","")}] {a.get("message","")}</li>' for a in failed_asserts[:5])}
                    </ul>
                </details>
                <p><a href="{trace_path}" target="_blank">查看 trace.log</a></p>
            </div>
        </div>'''

    # 构建全部用例行
    all_rows = ''
    for r in results:
        assertions = r.get('assertions', [])
        passed_a = sum(1 for a in assertions if a.get('passed'))
        total_a = len(assertions)
        status_class = {'PASS': 'pass', 'FAIL': 'fail', 'MANUAL_REVIEW': 'manual', 'ERROR': 'error'}.get(r.get('status', ''), '')
        status_icon = {'PASS': '✅', 'FAIL': '❌', 'MANUAL_REVIEW': '⚠️', 'ERROR': '💥'}.get(r.get('status', ''), '?')
        all_rows += f'''<tr class="row-{status_class}" onclick="showDetail('{r["test_id"]}')">
            <td>{r["test_id"]}</td>
            <td>{r.get("category", "")}</td>
            <td>{r.get("title", "")[:35]}</td>
            <td><span class="badge {status_class}">{status_icon} {r.get("status", "")}</span></td>
            <td>{r.get("duration_ms", 0):.0f}ms</td>
            <td>{passed_a}/{total_a}</td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tank Smart 测试报告 — {run_id}</title>
<style>
    :root {{
        --bg: #f8f9fc;
        --card-bg: #ffffff;
        --border: #e2e8f0;
        --text: #1a202c;
        --text-muted: #718096;
        --accent: #4f46e5;
        --pass: #10b981;
        --fail: #ef4444;
        --manual: #f59e0b;
        --error: #6b21a8;
        --radius: 10px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 8px; }}
    h2 {{ font-size: 1.3rem; margin: 32px 0 16px; border-bottom: 2px solid var(--border); padding-bottom: 8px; }}
    .meta {{ color: var(--text-muted); font-size: 0.9rem; margin-bottom: 24px; }}

    /* 概览卡片 */
    .overview {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; margin-bottom: 24px; }}
    .stat-card {{ background: var(--card-bg); border-radius: var(--radius); padding: 20px; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
    .stat-card .value {{ font-size: 2rem; font-weight: 700; }}
    .stat-card .label {{ color: var(--text-muted); font-size: 0.85rem; margin-top: 4px; }}
    .pass-rate-card {{ grid-column: span 2; background: linear-gradient(135deg, #4f46e5, #7c3aed); color: white; }}
    .pass-rate-card .value {{ color: white; }}
    .pass-rate-card .label {{ color: rgba(255,255,255,.8); }}

    /* 表格 */
    table {{ width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: var(--radius); overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
    th {{ background: #f1f5f9; padding: 12px 16px; text-align: left; font-size: 0.85rem; color: var(--text-muted); }}
    td {{ padding: 10px 16px; border-top: 1px solid var(--border); font-size: 0.9rem; }}
    tr:hover td {{ background: #f8fafc; cursor: pointer; }}
    .row-fail td {{ background: #fff5f5; }}
    .row-pass td {{ background: #f0fdf4; }}
    .row-manual td {{ background: #fffbeb; }}
    .row-error td {{ background: #faf5ff; }}

    /* Badge */
    .badge {{ display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }}
    .badge.pass {{ background: #d1fae5; color: #065f46; }}
    .badge.fail {{ background: #fee2e2; color: #991b1b; }}
    .badge.manual {{ background: #fef3c7; color: #92400e; }}
    .badge.error {{ background: #f3e8ff; color: #6b21a8; }}

    /* 进度条 */
    .progress-bar-pass {{ height: 6px; background: var(--pass); border-radius: 3px; display: inline-block; vertical-align: middle; margin-right: 8px; }}

    /* 测试卡片 */
    .test-card {{ background: var(--card-bg); border-radius: var(--radius); margin-bottom: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); border-left: 4px solid var(--fail); }}
    .test-card.pass {{ border-left-color: var(--pass); }}
    .test-card.manual {{ border-left-color: var(--manual); }}
    .test-header {{ padding: 14px 16px; display: flex; align-items: center; gap: 12px; cursor: pointer; }}
    .test-id {{ font-weight: 700; color: var(--accent); min-width: 50px; }}
    .test-title {{ flex: 1; font-weight: 500; }}
    .test-cat {{ color: var(--text-muted); font-size: 0.85rem; }}
    .test-time {{ color: var(--text-muted); font-size: 0.85rem; }}
    .toggle-icon {{ color: var(--text-muted); transition: transform 0.2s; }}
    .test-detail {{ padding: 0 16px 16px; display: none; border-top: 1px solid var(--border); margin-top: 0; }}
    .test-detail.open {{ display: block; }}
    .assert-fail {{ color: var(--fail); font-size: 0.9rem; }}

    /* 折叠 */
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; font-weight: 500; color: var(--fail); }}

    /* 导航锚点 */
    nav {{ background: var(--card-bg); padding: 12px 24px; position: sticky; top: 0; z-index: 10; border-bottom: 1px solid var(--border); }}
    nav a {{ margin-right: 20px; color: var(--accent); text-decoration: none; font-weight: 500; }}
    nav a:hover {{ text-decoration: underline; }}

    /* 代码块 */
    pre {{ background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 0.85rem; }}
    code {{ font-family: "Fira Code", monospace; font-size: 0.85rem; }}
    .log-link {{ display: inline-block; margin-top: 8px; color: var(--accent); }}

    footer {{ text-align: center; padding: 32px; color: var(--text-muted); font-size: 0.85rem; }}
</style>
<script>
function toggleDetail(el) {{
    const card = el.parentElement;
    const detail = card.querySelector('.test-detail');
    const icon = card.querySelector('.toggle-icon');
    detail.classList.toggle('open');
    icon.textContent = detail.classList.contains('open') ? '▾' : '▸';
}}
function filterTable(status) {{
    const rows = document.querySelectorAll('table#all-tests tbody tr');
    rows.forEach(row => {{
        if (status === 'all' || row.className.includes('row-' + status)) {{
            row.style.display = '';
        }} else {{
            row.style.display = 'none';
        }}
    }});
}}
</script>
</head>
<body>
<nav>
    <a href="#overview">概览</a>
    <a href="#categories">类别</a>
    <a href="#failed">失败用例</a>
    <a href="#all">全部用例</a>
    <a href="#performance">性能</a>
</nav>

<div class="container">
    <h1>🧪 Tank Smart 功能测试执行报告</h1>
    <p class="meta">运行ID: <code>{run_id}</code> &nbsp;|&nbsp; 执行时间: {ts} &nbsp;|&nbsp; 产物目录: <code>tests/artifacts/run_{run_id}/</code></p>

    <section id="overview">
        <div class="overview">
            <div class="stat-card pass-rate-card">
                <div class="value">{stats['pass_rate']}</div>
                <div class="label">通过率</div>
            </div>
            <div class="stat-card">
                <div class="value">{stats['total']}</div>
                <div class="label">总用例数</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:var(--pass)">{stats['passed']}</div>
                <div class="label">✅ 通过</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:var(--fail)">{stats['failed']}</div>
                <div class="label">❌ 失败</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:var(--manual)">{stats['manual']}</div>
                <div class="label">⚠️ 需确认</div>
            </div>
            <div class="stat-card">
                <div class="value" style="color:var(--error)">{stats['errors']}</div>
                <div class="label">💥 错误</div>
            </div>
        </div>
    </section>

    <h2 id="categories">📊 类别通过率</h2>
    <table>
        <thead>
            <tr><th>类别</th><th>总数</th><th>通过</th><th>失败</th><th>需确认</th><th>错误</th><th>通过率</th></tr>
        </thead>
        <tbody>{cat_rows}</tbody>
    </table>

    <h2 id="failed">❌ 失败用例 ({len(stats.get('failed_results', []))})</h2>
    {failed_rows if failed_rows else '<p>🎉 所有用例均已通过！</p>'}

    <h2 id="all">📋 全部用例 ({stats['total']}) — <button onclick="filterTable('all')">全部</button> <button onclick="filterTable('pass')">仅通过</button> <button onclick="filterTable('fail')">仅失败</button></h2>
    <table id="all-tests">
        <thead>
            <tr><th>ID</th><th>类别</th><th>标题</th><th>状态</th><th>耗时</th><th>断言</th></tr>
        </thead>
        <tbody>{all_rows}</tbody>
    </table>

    <h2 id="performance">⚡ 性能数据</h2>
    <table>
        <thead><tr><th>指标</th><th>数值</th></tr></thead>
        <tbody>
            <tr><td>平均响应时间</td><td>{stats['avg_duration_ms']:.0f} ms</td></tr>
            <tr><td>最快耗时</td><td>{stats['min_duration_ms']:.0f} ms</td></tr>
            <tr><td>最慢耗时</td><td>{stats['max_duration_ms']:.0f} ms</td></tr>
        </tbody>
    </table>

    <h2 id="logs">📁 产物目录</h2>
    <p>完整产物位于：<code>tests/artifacts/run_{run_id}/</code></p>
    <p>每个测试用例（T-01 ~ T-55）对应一个子目录，内含：</p>
    <ul>
        <li><code>input.txt</code> — 用户输入</li>
        <li><code>trace.log</code> — 完整执行日志</li>
        <li><code>result.json</code> — 结构化结果（含断言详情）</li>
    </ul>
</div>

<footer>
    Tank Smart 测试框架 | {ts} | <a href="https://github.com/tankqiu/MyAgent">GitHub</a>
</footer>
</body>
</html>'''

    return html


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Tank Smart 测试报告生成器')
    parser.add_argument('--run-id', type=str, required=True, help='运行ID（如 20250804_143022）')
    parser.add_argument('--output-md', type=str, help='Markdown 报告输出路径（默认写入 docs/test_execution_report.md）')
    parser.add_argument('--output-html', type=str, help='HTML 报告输出路径（默认写入 tests/artifacts/run_xxx/report.html）')
    parser.add_argument('--list-runs', action='store_true', help='列出所有已完成的运行')
    args = parser.parse_args()

    if args.list_runs:
        print('已完成的运行：')
        if os.path.exists(ARTIFACTS_DIR):
            for d in sorted(os.listdir(ARTIFACTS_DIR)):
                if d.startswith('run_'):
                    run_dir = os.path.join(ARTIFACTS_DIR, d)
                    count = sum(1 for f in os.listdir(run_dir) if f.startswith('T-')) if os.path.exists(run_dir) else 0
                    print(f'  {d} ({count} 个测试)')
        return

    # 加载结果
    results, summary = load_results(args.run_id)
    if not results:
        print(f'未找到运行结果: run_{args.run_id}')
        print('使用 --list-runs 查看所有可用运行')
        return

    print(f'已加载 {len(results)} 个测试结果')

    # 计算统计
    stats = compute_stats(results)

    # 生成 Markdown
    md_content = generate_markdown_report(results, stats, args.run_id, summary)
    md_output = args.output_md or os.path.join(DOCS_DIR, 'test_execution_report.md')
    with open(md_output, 'w', encoding='utf-8') as f:
        f.write(md_content)
    print(f'Markdown 报告: {md_output}')

    # 生成 HTML
    html_content = generate_html_report(results, stats, args.run_id, summary)
    html_output = args.output_html or os.path.join(ARTIFACTS_DIR, f'run_{args.run_id}', 'report.html')
    with open(html_output, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f'HTML 报告: {html_output}')

    # 打印概览
    print()
    print(f'通过率: {stats["pass_rate"]} ({stats["passed"]}/{stats["total"]})')
    print(f'失败: {stats["failed"]} | 需确认: {stats["manual"]} | 错误: {stats["errors"]}')
    print(f'平均耗时: {stats["avg_duration_ms"]:.0f} ms')


if __name__ == '__main__':
    main()
