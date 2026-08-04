"""
test_cleanup.py — Tank Smart 测试环境清理模块
在每次测试后回滚变更，确保测试可独立重复执行
"""
import os
import json
import time
import shutil
import subprocess
from typing import List, Dict, Any, Optional


# ── 测试产生的临时文件路径 ──────────────────────────────────────────
# 这些文件由测试生成，清理时需要移除
_DESKTOP_TEMP_PATTERNS = [
    'sysinfo.txt',
    'report.docx',
    'table_test.docx',
    'image_doc.docx',
    'header_footer.docx',
    'professional_report.docx',
    'intro.pptx',
    'photo_slides.pptx',
    'chart_demo.pptx',
    'tech_presentation.pptx',
    'intro_content.txt',
    'screenshot_test.png',
    'region.png',
    'cat.jpg',
    'chart.xlsx',
    'output.xlsx',
    'merged.xlsx',
    'installed_software.csv',
]

_PROJECT_TEMP_PATTERNS = [
    'hello.py',
    'app.py',
    'test_script.py',
]


def get_desktop_path() -> str:
    """获取用户桌面路径"""
    return os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')


def get_test_artifacts_dir() -> str:
    """获取测试产物根目录"""
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(proj, 'tests', 'artifacts')


def cleanup_test_artifacts(run_id: str) -> List[str]:
    """清理指定运行目录下的测试产物（可选）"""
    artifacts_dir = get_test_artifacts_dir()
    run_dir = os.path.join(artifacts_dir, f'run_{run_id}')
    cleaned = []
    if os.path.exists(run_dir):
        for item in os.listdir(run_dir):
            if item.startswith('T-'):
                item_path = os.path.join(run_dir, item)
                if os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        cleaned.append(os.path.join(item_path, f))
    return cleaned


def cleanup_generated_files(files_created: List[str]) -> List[str]:
    """清理测试过程中生成的文件"""
    cleaned = []
    for fpath in files_created:
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
                cleaned.append(fpath)
        except Exception:
            pass
    return cleaned


def restore_registry_keys(backup_file: str) -> bool:
    """从备份恢复注册表"""
    if not os.path.exists(backup_file):
        return False
    try:
        result = subprocess.run(
            ['reg', 'import', backup_file],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def backup_registry(key_path: str, backup_file: str) -> bool:
    """备份注册表项"""
    try:
        result = subprocess.run(
            ['reg', 'export', key_path, backup_file, '/y'],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


def uninstall_test_packages(packages: List[str]) -> List[str]:
    """卸载测试安装的 Python 包"""
    uninstalled = []
    for pkg in packages:
        try:
            r = subprocess.run(
                [os.sys.executable, '-m', 'pip', 'uninstall', pkg, '-y'],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode == 0:
                uninstalled.append(pkg)
        except Exception:
            pass
    return uninstalled


def uninstall_winget_package(package_id: str) -> bool:
    """卸载 winget 安装的软件包"""
    try:
        r = subprocess.run(
            ['winget', 'uninstall', '--id', package_id, '--silent'],
            capture_output=True, text=True, timeout=120
        )
        return r.returncode == 0
    except Exception:
        return False


def cleanup_desktop_temps() -> List[str]:
    """清理桌面上测试残留文件"""
    desktop = get_desktop_path()
    cleaned = []
    if not os.path.exists(desktop):
        return cleaned
    for fname in _DESKTOP_TEMP_PATTERNS:
        fpath = os.path.join(desktop, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                cleaned.append(fpath)
            except Exception:
                pass
    return cleaned


def cleanup_workspace_temps(proj_dir: str) -> List[str]:
    """清理 workspace 目录下测试残留文件"""
    workspace = os.path.join(proj_dir, 'workspace')
    cleaned = []
    if not os.path.exists(workspace):
        return cleaned
    for fname in _PROJECT_TEMP_PATTERNS:
        fpath = os.path.join(workspace, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                cleaned.append(fpath)
            except Exception:
                pass
    return cleaned


def reset_test_data_files(proj_dir: str) -> None:
    """重置 test_data 目录（重建标准测试文件）"""
    import csv
    test_data = os.path.join(proj_dir, 'test_data')
    os.makedirs(test_data, exist_ok=True)

    # 重建 sales_data.csv
    csv_path = os.path.join(test_data, 'sales_data.csv')
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['部门', '姓名', '销售额', '月份'])
        writer.writerow(['销售部', '张三', 15000, 'Q1'])
        writer.writerow(['销售部', '李四', 8000, 'Q1'])
        writer.writerow(['技术部', '王五', 12000, 'Q1'])
        writer.writerow(['技术部', '赵六', 20000, 'Q1'])
        writer.writerow(['市场部', '孙七', 6000, 'Q1'])
        writer.writerow(['市场部', '周八', 9000, 'Q1'])

    # 重建 sample.txt
    txt_path = os.path.join(test_data, 'sample.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('Hello world, this is a sample text file for testing.\n')
        f.write('The quick brown fox jumps over the lazy dog.\n')
        f.write('Tank Smart is an intelligent agent framework.\n')


def full_cleanup(run_id: str, proj_dir: str, uninstall_packages: bool = False) -> Dict[str, Any]:
    """
    完整清理：执行所有清理操作，返回清理报告
    """
    report = {}

    # 1. 桌面残留
    desktop_cleaned = cleanup_desktop_temps()
    report['desktop_cleaned'] = desktop_cleaned

    # 2. workspace 残留
    workspace_cleaned = cleanup_workspace_temps(proj_dir)
    report['workspace_cleaned'] = workspace_cleaned

    # 3. 重置测试数据
    try:
        reset_test_data_files(proj_dir)
        report['test_data_reset'] = True
    except Exception as e:
        report['test_data_reset'] = str(e)

    # 4. 卸载测试包
    if uninstall_packages:
        test_pkgs = ['pyperclip', 'requests', 'pandas', 'numpy', 'matplotlib']
        report['uninstalled_packages'] = uninstall_test_packages(test_pkgs)

    return report


if __name__ == '__main__':
    import sys
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    report = full_cleanup('latest', proj)
    print('清理完成:', json.dumps(report, ensure_ascii=False, indent=2))
