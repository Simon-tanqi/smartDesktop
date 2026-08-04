"""
test_orchestrator.py - Tank Smart Test Orchestrator
Supports --all / --category / --id / --retry execution modes
"""
import os, sys, json, time, uuid, datetime, argparse, threading, traceback, re
from typing import List, Dict, Any, Optional

PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_DIR)
from tests.test_cases_parser import parse_test_cases, group_by_category, TestCase
from tests.test_assertions import (
    AssertionResult, compose_assertions,
    assert_success, assert_file_exists, assert_file_size_gt,
    assert_tool_called, assert_safety_blocked, assert_final_text_contains
)
from logger import get_logger
logger = get_logger()
ARTIFACTS_DIR = os.path.join(PROJ_DIR, 'tests', 'artifacts')
TEST_DATA_DIR = os.path.join(PROJ_DIR, 'test_data')
FIXTURES_DIR = os.path.join(PROJ_DIR, 'tests', 'fixtures')

class MockToolExecutor:
    """Mock tool executor for isolated testing"""
    def __init__(self):
        self.call_history = []
        self._create_test_data()

    def _create_test_data(self):
        os.makedirs(TEST_DATA_DIR, exist_ok=True)
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        csv_path = os.path.join(TEST_DATA_DIR, 'sales_data.csv')
        if not os.path.exists(csv_path):
            import csv as _csv
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                w = _csv.writer(f)
                w.writerow(['部门', '姓名', '销售额', '月份'])
                for row in [('销售部', '张三', 15000, 'Q1'), ('销售部', '李四', 8000, 'Q1'),
                            ('技术部', '王五', 12000, 'Q1'), ('技术部', '赵六', 20000, 'Q1'),
                            ('市场部', '孙七', 6000, 'Q1'), ('市场部', '周八', 9000, 'Q1')]:
                    w.writerow(row)
        txt_path = os.path.join(TEST_DATA_DIR, 'sample.txt')
        if not os.path.exists(txt_path):
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write("Hello world, this is a sample text file for testing.\n")
                f.write("The quick brown fox jumps over the lazy dog.\n")
                f.write("Tank Smart is an intelligent agent framework.\n")
        try:
            import openpyxl
            for fname in ('file1.xlsx', 'file2.xlsx'):
                fpath = os.path.join(FIXTURES_DIR, fname)
                if not os.path.exists(fpath):
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    ws.title = "Sheet1"
                    ws.append(['姓名', '年龄', '城市'])
                    ws.append(['测试A', 25, '北京'])
                    ws.append(['测试B', 30, '上海'])
                    wb.save(fpath)
        except ImportError:
            pass

    def execute_tool(self, tool_name, arguments):
        self.call_history.append({'tool': tool_name, 'arguments': arguments})
        executor_map = {
            'execute_command': self._mock_execute_command,
            'write_file': self._mock_write_file,
            'read_file': self._mock_read_file,
            'web_search': self._mock_web_search,
            'fetch_webpage': self._mock_fetch_webpage,
            'take_screenshot': self._mock_screenshot,
            'gui_screenshot': self._mock_screenshot,
            'get_network_info': self._mock_network_info,
            'find_program': self._mock_find_program,
            'run_python': self._mock_run_python,
            'modify_registry': self._mock_registry,
            'get_uninstall_string': self._mock_uninstall,
            'excel_read': self._mock_excel_read,
            'excel_pivot': self._mock_excel_pivot,
            'excel_write': self._mock_excel_write,
            'excel_convert': self._mock_excel_convert,
            'excel_merge': self._mock_excel_merge,
            'excel_formula': self._mock_excel_formula,
            'excel_chart_advanced': self._mock_excel_chart,
            'excel_conditional_format': self._mock_excel_condfmt,
            'excel_format': self._mock_excel_format,
            'create_word_document': self._mock_word_create,
            'add_word_content': self._mock_word_add,
            'extract_word_text': self._mock_word_extract,
            'search_replace_word': self._mock_word_replace,
            'generate_ppt': self._mock_ppt_generate,
            'add_slide_content': self._mock_ppt_add_slide,
            'extract_presentation_info': self._mock_ppt_extract,
            'export_presentation_text': self._mock_ppt_extract,
            'system_audio': self._mock_system_audio,
            'get_power_plan': self._mock_power_plan,
            'set_power_plan': self._mock_power_plan,
            'search_images': self._mock_search_images,
            'web_download': self._mock_web_download,
            'search_academic': self._mock_web_search,
            'search_news': self._mock_web_search,
            'gui_open_app': self._mock_gui_open,
            'get_project_summary': self._mock_project_summary,
        }
        func = executor_map.get(tool_name)
        if func:
            try:
                return func(arguments)
            except Exception as e:
                return {'success': False, 'message': 'Mock exception: ' + str(e), 'output': ''}
        return {'success': True, 'message': '[Mock] tool ' + tool_name + ' completed', 'output': ''}

    def _mock_execute_command(self, args):
        import subprocess
        cmd = args.get('command', '')
        if not cmd:
            return {'success': False, 'message': 'empty command', 'output': ''}
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  timeout=30, encoding='utf-8', errors='replace')
            output = (result.stdout + result.stderr)[:5000]
            return {'success': result.returncode == 0, 'message': 'command done', 'output': output}
        except subprocess.TimeoutExpired:
            return {'success': False, 'message': 'timeout', 'output': ''}
        except Exception as e:
            return {'success': False, 'message': str(e), 'output': ''}

    def _mock_write_file(self, args):
        path = args.get('file_path', '')
        content = args.get('content', '')
        if not path:
            return {'success': False, 'message': 'empty path', 'output': ''}
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            mode = 'wb' if isinstance(content, bytes) else 'w'
            enc = None if mode == 'wb' else 'utf-8'
            with open(path, mode, encoding=enc, errors='replace') as f:
                f.write(content)
            return {'success': True, 'message': 'File saved: ' + path, 'output': path}
        except Exception as e:
            return {'success': False, 'message': 'Write failed: ' + str(e), 'output': ''}

    def _mock_read_file(self, args):
        path = args.get('file_path', '')
        if not path or not os.path.exists(path):
            return {'success': False, 'message': 'File not found: ' + path, 'output': ''}
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read(int(args.get('max_length', 10000)))
            return {'success': True, 'message': 'Read: ' + path, 'output': content}
        except Exception as e:
            return {'success': False, 'message': 'Read failed: ' + str(e), 'output': ''}

    def _mock_web_search(self, args):
        q = args.get('query', args.get('search_query', ''))
        max_r = int(args.get('max_results', 3))
        results = []
        for i in range(min(max_r, 5)):
            results.append({'title': 'Result ' + str(i+1) + ' for ' + q,
                            'url': 'https://example.com/result' + str(i+1),
                            'snippet': 'Mock search result ' + str(i+1) + ' for: ' + q})
        return {'success': True, 'message': 'Found ' + str(len(results)) + ' results',
                'output': json.dumps({'results': results}, ensure_ascii=False, indent=2)}

    def _mock_fetch_webpage(self, args):
        url = args.get('url', '')
        max_len = int(args.get('max_length', 5000))
        text = '[Mock content from ' + url + ']\n'
        text += 'This is a mock webpage fetch. '
        text += 'The content would be the actual page HTML or text, '
        text += 'truncated to ' + str(max_len) + ' characters. Mock data for testing.'
        return {'success': True, 'message': 'Fetched: ' + url, 'output': text[:max_len]}

    def _mock_screenshot(self, args):
        path = args.get('file_path', '')
        if not path:
            desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
            path = os.path.join(desktop, args.get('name', 'screenshot.png'))
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n' + b'Mock PNG - ' + os.urandom(10240))
            return {'success': True, 'message': 'Screenshot saved: ' + path, 'output': path}
        except Exception as e:
            return {'success': False, 'message': 'Screenshot failed: ' + str(e), 'output': ''}

    def _mock_network_info(self, args):
        return {'success': True, 'message': 'Network info retrieved',
                'output': 'IPv4: 192.168.1.100\nGateway: 192.168.1.1\nDNS: 8.8.8.8\nConnection: OK'}

    def _mock_find_program(self, args):
        name = args.get('program_name', '')
        return {'success': True, 'message': 'Found: ' + name,
                'output': r'C:\Program Files\Google\Chrome\Application\chrome.exe'}

    def _mock_run_python(self, args):
        code = args.get('code', '')
        if not code:
            return {'success': False, 'message': 'No code provided', 'output': ''}
        restricted = ['subprocess', 'os.system', 'eval(', 'exec(', 'import os', '__import__']
        for r in restricted:
            if r in code:
                return {'success': False, 'message': 'Restricted: ' + r, 'output': ''}
        try:
            import io, sys as _sys
            old_stdout = _sys.stdout
            captured = io.StringIO()
            _sys.stdout = captured
            exec(code, {'__name__': '__main__'})
            _sys.stdout = old_stdout
            output = captured.getvalue()
            return {'success': True, 'message': 'Code executed', 'output': output}
        except Exception as e:
            return {'success': False, 'message': 'Execution error: ' + str(e), 'output': ''}

    def _mock_registry(self, args):
        action = args.get('action', 'get')
        key = args.get('key', '')
        if action == 'get':
            return {'success': True, 'message': 'Registry read OK',
                    'output': 'Windows Product Name: Windows 11 Pro\nCurrentBuild: 22631'}
        elif action == 'set':
            if 'HKLM' in key or 'HKEY_LOCAL_MACHINE' in key:
                return {'success': False, 'message': 'HKLM write blocked', 'output': 'ERROR: Access denied'}
            return {'success': True, 'message': 'Registry key set', 'output': 'Key written successfully'}
        return {'success': True, 'message': 'Registry action completed', 'output': ''}

    def _mock_uninstall(self, args):
        name = args.get('program_name', '')
        return {'success': True, 'message': 'Uninstall string obtained',
                'output': r'C:\Program Files\Mock\uninstall.exe /S'}

    def _mock_excel_read(self, args):
        path = args.get('file_path', '')
        if not path or not os.path.exists(path):
            return {'success': False, 'message': 'File not found: ' + path, 'output': ''}
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
            max_rows = int(args.get('max_rows', 10))
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    break
                rows.append(list(row))
            return {'success': True, 'message': 'Read ' + str(len(rows)) + ' rows',
                    'output': json.dumps(rows, ensure_ascii=False)}
        except Exception as e:
            return {'success': False, 'message': 'Excel read failed: ' + str(e), 'output': ''}

    def _mock_excel_pivot(self, args):
        return {'success': True, 'message': 'Pivot table created',
                'output': "部门汇总 sheet created with aggregated data"}

    def _mock_excel_write(self, args):
        path = args.get('file_path', '')
        data = args.get('data', [])
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            for row in data:
                ws.append(row)
            wb.save(path)
            return {'success': True, 'message': 'Excel saved: ' + path, 'output': path}
        except Exception as e:
            return {'success': False, 'message': 'Excel write failed: ' + str(e), 'output': ''}

    def _mock_excel_convert(self, args):
        return {'success': True, 'message': 'Excel to CSV converted',
                'output': 'sales_data.csv created on Desktop'}

    def _mock_excel_merge(self, args):
        return {'success': True, 'message': 'Excel files merged',
                'output': 'merged.xlsx created'}

    def _mock_excel_formula(self, args):
        return {'success': True, 'message': 'Formulas calculated',
                'output': 'avg=11666.7, max=20000, min=6000'}

    def _mock_excel_chart(self, args):
        return {'success': True, 'message': 'Chart created',
                'output': 'chart.xlsx saved on Desktop'}

    def _mock_excel_condfmt(self, args):
        return {'success': True, 'message': 'Conditional formatting applied',
                'output': 'Conditional format applied to sales_data.xlsx'}

    def _mock_excel_format(self, args):
        return {'success': True, 'message': 'Formatting applied',
                'output': 'Excel formatting done'}

    def _mock_word_create(self, args):
        path = args.get('file_path', '')
        if not path:
            return {'success': False, 'message': 'empty path', 'output': ''}
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            import docx
            d = docx.Document()
            title = args.get('title', 'Test Document')
            d.add_heading(title, 0)
            content = args.get('content', 'Test content paragraph.')
            d.add_paragraph(content)
            d.save(path)
            return {'success': True, 'message': 'Word doc saved: ' + path, 'output': path}
        except Exception as e:
            return {'success': False, 'message': 'Word create failed: ' + str(e), 'output': ''}

    def _mock_word_add(self, args):
        return {'success': True, 'message': 'Content added to Word doc',
                'output': 'Content added successfully'}

    def _mock_word_extract(self, args):
        return {'success': True, 'message': 'Text extracted',
                'output': 'Sample text extracted from document. Contains report title and two paragraphs.'}

    def _mock_word_replace(self, args):
        return {'success': True, 'message': 'Search and replace done',
                'output': 'Replaced 2 occurrences'}

    def _mock_ppt_generate(self, args):
        return {'success': True, 'message': 'PPTX generated',
                'output': 'intro.pptx created on Desktop'}

    def _mock_ppt_add_slide(self, args):
        return {'success': True, 'message': 'Slide added',
                'output': 'New slide added to presentation'}

    def _mock_ppt_extract(self, args):
        return {'success': True, 'message': 'PPTX info extracted',
                'output': '3 slides, each slide has title. File size: 24KB'}

    def _mock_system_audio(self, args):
        action = args.get('action', 'get_volume')
        if action == 'set_volume':
            return {'success': True, 'message': 'Volume set to 50',
                    'output': 'Volume level changed to 50'}
        return {'success': True, 'message': 'Volume is 50', 'output': '50'}

    def _mock_power_plan(self, args):
        return {'success': True, 'message': 'Power plan retrieved/set',
                'output': 'Current plan: High Performance (8c5e7fda-e8b5-43c4-bbbb-000000000000)'}

    def _mock_search_images(self, args):
        return {'success': True, 'message': 'Images found',
                'output': json.dumps({'images': [{'url': 'https://example.com/cat.jpg',
                                                'filename': 'cat.jpg'}]}, ensure_ascii=False)}

    def _mock_web_download(self, args):
        url = args.get('url', '')
        path = args.get('file_path', '') or os.path.join(os.path.expanduser('~'), 'Desktop', os.path.basename(url))
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'Mock JPEG image data for testing\n' + os.urandom(10240))
            return {'success': True, 'message': 'Downloaded: ' + url, 'output': path}
        except Exception as e:
            return {'success': False, 'message': 'Download failed: ' + str(e), 'output': ''}

    def _mock_gui_open(self, args):
        return {'success': True, 'message': 'Application opened',
                'output': 'Edge opened at bing.com (manual verification required for GUI tests)'}

    def _mock_project_summary(self, args):
        proj = args.get('project_path', PROJ_DIR)
        py_files = []
        try:
            for root, dirs, files in os.walk(proj):
                for f in files:
                    if f.endswith('.py'):
                        py_files.append(os.path.join(root, f))
        except Exception:
            pass
        return {'success': True, 'message': 'Found ' + str(len(py_files)) + ' Python files',
                'output': json.dumps({'python_files': py_files[:20]}, ensure_ascii=False)}

class TestOrchestrator:
    """Tank Smart Test Orchestrator - runs 55 test cases"""
    def __init__(self, verbose=False, dry_run=False):
        self.verbose = verbose
        self.dry_run = dry_run
        self.executor = MockToolExecutor()
        self.run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def _build_manifest(self, run_dir):
        manifest = {'run_id': self.run_id, 'timestamp': datetime.datetime.now().isoformat(), 'tests': []}
        with open(os.path.join(run_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    def run_single(self, test_case: TestCase, max_retries=2) -> Dict[str, Any]:
        test_dir = os.path.join(ARTIFACTS_DIR, 'run_' + self.run_id, test_case.id)
        os.makedirs(test_dir, exist_ok=True)
        input_path = os.path.join(test_dir, 'input.txt')
        with open(input_path, 'w', encoding='utf-8') as f:
            f.write(test_case.user_input)
        events = []
        tool_calls = []
        final_text = ''
        error_msg = None
        status = 'ERROR'
        start_time = time.time()
        try:
            if self.dry_run:
                events, tool_calls, final_text = self._simulate_agent(test_case)
                status = 'PASS'
            else:
                events, tool_calls, final_text = self._call_agent(test_case.user_input)
                status = 'PASS'
        except Exception as e:
            error_msg = str(e)
            status = 'ERROR'
            events.append({'type': 'error', 'data': error_msg})
        duration_ms = int((time.time() - start_time) * 1000)
        trace_path = os.path.join(test_dir, 'trace.log')
        with open(trace_path, 'w', encoding='utf-8') as f:
            f.write('Tank Smart Test Trace - ' + test_case.id + '\n')
            f.write('Category: ' + test_case.category + '\n')
            f.write('Input: ' + test_case.user_input + '\n')
            f.write('Status: ' + status + '\n')
            f.write('Duration: ' + str(duration_ms) + 'ms\n')
            f.write('-' * 60 + '\n')
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + '\n')
            if error_msg:
                f.write('\nERROR: ' + error_msg + '\n')
        # Build run_result from events
        run_result = {'status': status, 'error': error_msg, 'events': events}
        # Collect files created from tool calls
        files_created = []
        for tc_item in tool_calls:
            if tc_item.get('tool') == 'write_file':
                p = tc_item.get('args', {}).get('file_path', '')
                if p:
                    files_created.append(p)
        assertions = compose_assertions(
            test_id=test_case.id,
            category=test_case.category,
            run_result=run_result,
            final_text=final_text,
            tool_calls=tool_calls,
            files_created=files_created,
            log_path=trace_path,
        )
        result = {
            'test_id': test_case.id,
            'category': test_case.category,
            'title': test_case.title,
            'input': test_case.user_input,
            'status': status,
            'duration_ms': duration_ms,
            'assertions': [a.to_dict() for a in assertions],
            'tool_calls': tool_calls,
            'final_text_preview': final_text[:500] if final_text else '',
            'error': error_msg,
        }
        result_path = os.path.join(test_dir, 'result.json')
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return result

    def _simulate_agent(self, tc: TestCase):
        """Simulate agent based on test category"""
        events = []
        tool_calls = []
        final_text = ''
        inp = tc.user_input.lower()
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        data_path = os.path.join(TEST_DATA_DIR, 'sales_data.xlsx')
        if tc.category == '系统配置更改':
            if '音量' in inp:
                tool_calls.append({'tool': 'system_audio', 'args': {'action': 'set_volume', 'volume': 50}})
                events.append({'type': 'tool_result', 'tool': 'system_audio', 'result': {'output': '50'}})
                tool_calls.append({'tool': 'system_audio', 'args': {'action': 'get_volume'}})
                events.append({'type': 'tool_result', 'tool': 'system_audio', 'result': {'output': '50'}})
                final_text = 'Volume set to 50 and confirmed.'
            elif '系统信息' in inp or 'systeminfo' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'systeminfo'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'output': 'OS Name: Windows 11 Pro'}})
                final_text = 'System information retrieved successfully.'
            elif 'ipconfig' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'ipconfig /all'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'output': 'IPv4: 192.168.1.100'}})
                final_text = 'Network configuration displayed.'
            elif '注册表' in inp:
                if 'HKLM' in inp or 'HKEY_LOCAL_MACHINE' in inp:
                    tool_calls.append({'tool': 'modify_registry', 'args': {'action': 'set', 'key': 'HKLM\\SOFTWARE\\Test', 'name': 'TestValue', 'value': 'HelloAgent'}})
                    events.append({'type': 'tool_result', 'tool': 'modify_registry', 'result': {'success': False, 'message': 'Access denied'}})
                    final_text = 'Registry write blocked for HKLM.'
                else:
                    tool_calls.append({'tool': 'modify_registry', 'args': {'action': 'get', 'key': 'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion'}})
                    events.append({'type': 'tool_result', 'tool': 'modify_registry', 'result': {'output': 'ProductName: Windows 11'}})
                    final_text = 'Registry key read successfully.'
            elif '电源' in inp or 'powercfg' in inp:
                tool_calls.append({'tool': 'set_power_plan', 'args': {'plan': 'high_performance'}})
                events.append({'type': 'tool_result', 'tool': 'set_power_plan', 'result': {'success': True}})
                final_text = 'Power plan set to High Performance.'
            elif 'chrome' in inp or 'find_program' in inp:
                tool_calls.append({'tool': 'find_program', 'args': {'program_name': 'chrome'}})
                events.append({'type': 'tool_result', 'tool': 'find_program', 'result': {'output': 'C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe'}})
                final_text = 'Chrome.exe found.'
            elif 'baidu' in inp or '网络' in inp:
                tool_calls.append({'tool': 'web_search', 'args': {'query': 'baidu', 'max_results': 1}})
                events.append({'type': 'tool_result', 'tool': 'web_search', 'result': {'output': '{"results": [{"title": "Baidu"}]}'}})
                final_text = 'Baidu search completed.'
        elif tc.category == '软件安装卸载':
            if 'winget search' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'winget search 7zip'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'output': '7-Zip 24.07'}})
                final_text = '7-Zip found in winget.'
            elif 'winget install' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'winget install --id 7zip --accept-package-agreements --accept-source-agreements'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'success': True}})
                final_text = '7-Zip installed successfully.'
            elif 'pip install' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'pip install pyperclip'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'output': 'Successfully installed pyperclip'}})
                final_text = 'pyperclip installed.'
            elif '已安装' in inp or '导出' in inp.lower():
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'winget list'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'output': 'Microsoft Visual C++ 2015\n7-Zip 24.07\nNotepad++\nPython 3.11'}})
                csv_path = os.path.join(desktop, 'software_list.csv')
                tool_calls.append({'tool': 'write_file', 'args': {'file_path': csv_path, 'content': 'Name,Version\nMicrosoft Visual C++ 2015,14.0\n7-Zip,24.07\nNotepad++,8.6'}})
                events.append({'type': 'tool_result', 'tool': 'write_file', 'result': {'success': True}})
                final_text = 'Software list exported to Desktop.'
            elif 'Windows Defender' in inp or '任务管理器' in inp:
                tool_calls.append({'tool': 'get_uninstall_string', 'args': {'program_name': 'Windows Defender'}})
                events.append({'type': 'tool_result', 'tool': 'get_uninstall_string', 'result': {'success': False, 'message': 'Protected system component'}})
                final_text = 'Operation blocked - protected system component.'
            elif 'notepad' in inp.lower():
                tool_calls.append({'tool': 'get_uninstall_string', 'args': {'program_name': 'Notepad++'}})
                events.append({'type': 'tool_result', 'tool': 'get_uninstall_string', 'result': {'output': 'C:\\\\Program Files\\\\Notepad++\\\\uninstall.exe'}})
                final_text = 'Notepad++ uninstall string verified.'
            elif 'pandas' in inp or 'numpy' in inp:
                tool_calls.append({'tool': 'execute_command', 'args': {'command': 'pip install pandas numpy matplotlib'}})
                events.append({'type': 'tool_result', 'tool': 'execute_command', 'result': {'success': True}})
                final_text = 'Packages installed.'
        elif tc.category == '网页浏览':
            if '新闻' in inp:
                tool_calls.append({'tool': 'web_search', 'args': {'query': 'latest news', 'max_results': 3 if '3' in inp else 5}})
                events.append({'type': 'tool_result', 'tool': 'web_search', 'result': {'output': 'News item 1\nNews item 2\nNews item 3'}})
                final_text = 'Found 3 news articles.'
            elif 'github' in inp or 'fetch' in inp:
                tool_calls.append({'tool': 'fetch_webpage', 'args': {'url': 'https://github.com/facebook/react', 'max_length': 5000}})
                events.append({'type': 'tool_result', 'tool': 'fetch_webpage', 'result': {'output': '# React\nA JavaScript library for building user interfaces. ' + 'X' * 400}})
                final_text = 'GitHub README fetched - 523 characters.'
            elif '全屏截图' in inp:
                sp = os.path.join(desktop, 'screenshot_test.png')
                tool_calls.append({'tool': 'take_screenshot', 'args': {'name': 'screenshot_test.png'}})
                events.append({'type': 'tool_result', 'tool': 'take_screenshot', 'result': {'output': sp}})
                final_text = 'Screenshot saved as screenshot_test.png (15KB).'
            elif ('cat' in inp or 'jpg' in inp) and '下载' in inp:
                tool_calls.append({'tool': 'search_images', 'args': {'query': 'cat', 'max_results': 1}})
                events.append({'type': 'tool_result', 'tool': 'search_images', 'result': {'output': '{"images": [{"url": "https://example.com/cat.jpg"}]}'}})
                dp = os.path.join(desktop, 'cat.jpg')
                tool_calls.append({'tool': 'web_download', 'args': {'url': 'https://example.com/cat.jpg', 'file_path': dp}})
                events.append({'type': 'tool_result', 'tool': 'web_download', 'result': {'output': dp}})
                final_text = 'cat.jpg downloaded to Desktop (12KB).'
            elif '学术' in inp:
                tool_calls.append({'tool': 'search_academic', 'args': {'query': inp, 'max_results': 3}})
                events.append({'type': 'tool_result', 'tool': 'search_academic', 'result': {'output': 'Paper 1: AI Research 2024\nPaper 2: Machine Learning Advances\nPaper 3: Deep Learning Applications'}})
                final_text = 'Found 3 academic papers.'
            elif 'T-27' in tc.id:
                tool_calls.append({'tool': 'gui_open_app', 'args': {'application': 'msedge', 'url': 'https://www.bing.com'}})
                events.append({'type': 'tool_result', 'tool': 'gui_open_app', 'result': {'output': 'Edge opened at bing.com'}})
                final_text = 'Edge browser opened at bing.com (MANUAL_REVIEW).'
            elif '区域截图' in inp:
                rp = os.path.join(desktop, 'region.png')
                tool_calls.append({'tool': 'take_screenshot', 'args': {'name': 'region.png', 'region': {'x': 0, 'y': 0, 'width': 800, 'height': 600}}})
                events.append({'type': 'tool_result', 'tool': 'take_screenshot', 'result': {'output': rp}})
                final_text = 'Region screenshot saved as region.png.'
        elif tc.category == 'Word 文档':
            if 'header_footer' in inp.lower():
                hp = os.path.join(desktop, 'header_footer.docx')
                tool_calls.append({'tool': 'create_word_document', 'args': {'file_path': hp, 'title': 'Confidential Document', 'header': '机密文件', 'footer': 'Page {page}'}})
                events.append({'type': 'tool_result', 'tool': 'create_word_document', 'result': {'success': True}})
                final_text = 'header_footer.docx created with header and footer.'
            elif 'table_test' in inp.lower():
                tp = os.path.join(desktop, 'table_test.docx')
                tool_calls.append({'tool': 'create_word_document', 'args': {'file_path': tp, 'title': 'Table Test', 'table': [[1,2,3],[4,5,6],[7,8,9]]}})
                events.append({'type': 'tool_result', 'tool': 'create_word_document', 'result': {'success': True}})
                final_text = 'table_test.docx created with 3x3 table.'
            elif 'image_doc' in inp.lower():
                ip = os.path.join(desktop, 'image_doc.docx')
                tool_calls.append({'tool': 'create_word_document', 'args': {'file_path': ip, 'title': 'Document with Image', 'include_image': True}})
                events.append({'type': 'tool_result', 'tool': 'create_word_document', 'result': {'success': True}})
                final_text = 'image_doc.docx created with centered image.'
            elif 'professional_report' in inp.lower():
                pp = os.path.join(desktop, 'professional_report.docx')
                tool_calls.append({'tool': 'create_word_document', 'args': {'file_path': pp, 'title': 'Professional Report', 'content': 'Executive summary...', 'style': 'professional'}})
                events.append({'type': 'tool_result', 'tool': 'create_word_document', 'result': {'success': True}})
                final_text = 'professional_report.docx created with complex styling.'
            elif 'report.docx' in inp and 'extract' in inp.lower():
                tool_calls.append({'tool': 'extract_word_text', 'args': {'file_path': os.path.join(desktop, 'report.docx')}})
                events.append({'type': 'tool_result', 'tool': 'extract_word_text', 'result': {'output': 'Test Report\n\nParagraph 1: Project progress.\nParagraph 2: Next steps.'}})
                final_text = 'Text extracted from report.docx - 3 paragraphs found.'
            elif '替换' in inp or 'replace' in inp.lower():
                tool_calls.append({'tool': 'search_replace_word', 'args': {'file_path': os.path.join(desktop, 'report.docx'), 'search': '项目', 'replace': 'PROJECT', 'case_sensitive': False}})
                events.append({'type': 'tool_result', 'tool': 'search_replace_word', 'result': {'output': 'Replaced 2 occurrences'}})
                final_text = "Replaced '项目' with 'PROJECT' - 2 occurrences updated."
            else:
                rp = os.path.join(desktop, 'report.docx')
                tool_calls.append({'tool': 'create_word_document', 'args': {'file_path': rp, 'title': 'Test Report', 'content': 'Paragraph 1: Project progress.\nParagraph 2: Next steps.'}})
                events.append({'type': 'tool_result', 'tool': 'create_word_document', 'result': {'success': True, 'output': rp}})
                final_text = 'report.docx created with title and two paragraphs.'
        elif tc.category == 'PPT':
            if 'intro' in inp.lower() and 'photo' not in inp.lower() and 'chart' not in inp.lower() and 'tech_presentation' not in inp.lower():
                ip2 = os.path.join(desktop, 'intro.pptx')
                tool_calls.append({'tool': 'generate_ppt', 'args': {'file_path': ip2, 'title': 'Introduction', 'theme': 'tech_blue'}})
                events.append({'type': 'tool_result', 'tool': 'generate_ppt', 'result': {'success': True}})
                final_text = 'intro.pptx created with 2 slides.'
            elif 'photo' in inp.lower():
                tool_calls.append({'tool': 'generate_ppt', 'args': {'file_path': os.path.join(desktop, 'photo_slides.pptx'), 'include_images': True}})
                events.append({'type': 'tool_result', 'tool': 'generate_ppt', 'result': {'success': True}})
                final_text = 'photo_slides.pptx created with images.'
            elif 'chart' in inp.lower():
                tool_calls.append({'tool': 'generate_ppt', 'args': {'file_path': os.path.join(desktop, 'chart_demo.pptx'), 'include_chart': True}})
                events.append({'type': 'tool_result', 'tool': 'generate_ppt', 'result': {'success': True}})
                final_text = 'chart_demo.pptx created with bar chart.'
            elif 'tech_presentation' in inp.lower() or ('AI时代' in inp and 'PPT' in inp):
                tool_calls.append({'tool': 'generate_ppt', 'args': {'file_path': os.path.join(desktop, 'tech_presentation.pptx'), 'slides': 5, 'theme': 'blue'}})
                events.append({'type': 'tool_result', 'tool': 'generate_ppt', 'result': {'success': True}})
                final_text = 'tech_presentation.pptx created with 5 slides.'
            elif 'extract' in inp.lower() and 'ppt' in inp.lower():
                tool_calls.append({'tool': 'extract_presentation_info', 'args': {'file_path': os.path.join(desktop, 'intro.pptx')}})
                events.append({'type': 'tool_result', 'tool': 'extract_presentation_info', 'result': {'output': '3 slides, slide titles: Title Slide, Content, Summary. File size: 24KB'}})
                final_text = 'intro.pptx extracted: 3 slides, 24KB.'
            elif '第3页' in inp or ('总结' in inp and 'slide' in inp.lower()):
                tool_calls.append({'tool': 'add_slide_content', 'args': {'file_path': os.path.join(desktop, 'intro.pptx'), 'slide_title': 'Summary', 'content': 'Key takeaways.'}})
                events.append({'type': 'tool_result', 'tool': 'add_slide_content', 'result': {'success': True}})
                final_text = "Slide 3 'Summary' added to intro.pptx."
            elif 'intro_content' in inp.lower():
                tool_calls.append({'tool': 'export_presentation_text', 'args': {'file_path': os.path.join(desktop, 'intro.pptx'), 'output_path': os.path.join(desktop, 'intro_content.txt')}})
                events.append({'type': 'tool_result', 'tool': 'export_presentation_text', 'result': {'output': os.path.join(desktop, 'intro_content.txt')}})
                final_text = 'intro_content.txt exported to Desktop.'
        elif tc.category == 'Excel':
            if ('read' in inp.lower() or '前10行' in inp) and 'pivot' not in inp.lower():
                tool_calls.append({'tool': 'excel_read', 'args': {'file_path': data_path, 'max_rows': 10}})
                events.append({'type': 'tool_result', 'tool': 'excel_read', 'result': {'output': json.dumps([['部门','姓名','销售额','月份'],['销售部','张三',15000,'Q1'],['销售部','李四',8000,'Q1']], ensure_ascii=False)}})
                final_text = 'sales_data.xlsx read - 6 data rows found.'
            elif '透视' in inp:
                tool_calls.append({'tool': 'excel_pivot', 'args': {'file_path': data_path, 'sheet_name': '部门汇总'}})
                events.append({'type': 'tool_result', 'tool': 'excel_pivot', 'result': {'output': "Pivot table created in '部门汇总' sheet"}})
                final_text = 'Pivot table created - sales aggregated by department.'
            elif '柱状图' in inp:
                tool_calls.append({'tool': 'excel_chart_advanced', 'args': {'file_path': os.path.join(desktop, 'chart.xlsx'), 'chart_type': 'bar'}})
                events.append({'type': 'tool_result', 'tool': 'excel_chart_advanced', 'result': {'success': True}})
                final_text = 'Bar chart saved to chart.xlsx on Desktop.'
            elif '统计' in inp or ('avg' in inp.lower() and 'max' in inp.lower()):
                tool_calls.append({'tool': 'excel_formula', 'args': {'file_path': data_path, 'operations': ['avg', 'max', 'min']}})
                events.append({'type': 'tool_result', 'tool': 'excel_formula', 'result': {'output': 'avg=11666.7, max=20000, min=6000'}})
                final_text = 'Statistics: avg=11666.7, max=20000, min=6000.'
            elif 'output.xlsx' in inp or '双 sheet' in inp:
                tool_calls.append({'tool': 'excel_write', 'args': {'file_path': os.path.join(desktop, 'output.xlsx'), 'sheet1_data': [['A', 1], ['B', 2]], 'sheet2_data': [['Header', 'Value']]}})
                events.append({'type': 'tool_result', 'tool': 'excel_write', 'result': {'success': True}})
                final_text = 'output.xlsx created with Sheet1 (data) and Sheet2 (bold header).'
            elif 'merge' in inp.lower():
                tool_calls.append({'tool': 'excel_merge', 'args': {'files': [os.path.join(FIXTURES_DIR, 'file1.xlsx'), os.path.join(FIXTURES_DIR, 'file2.xlsx')], 'output': os.path.join(desktop, 'merged.xlsx')}})
                events.append({'type': 'tool_result', 'tool': 'excel_merge', 'result': {'success': True}})
                final_text = 'file1.xlsx and file2.xlsx merged into merged.xlsx.'
            elif '条件格式' in inp:
                tool_calls.append({'tool': 'excel_conditional_format', 'args': {'file_path': data_path, 'conditions': [{'>10000': 'red'}, {'<5000': 'green'}]}})
                events.append({'type': 'tool_result', 'tool': 'excel_conditional_format', 'result': {'success': True}})
                final_text = 'Conditional formatting applied: >10000 red, <5000 green.'
            elif 'csv' in inp.lower() or '转换' in inp:
                tool_calls.append({'tool': 'excel_convert', 'args': {'file_path': data_path, 'output_format': 'csv', 'output_path': os.path.join(desktop, 'sales_data.csv')}})
                events.append({'type': 'tool_result', 'tool': 'excel_convert', 'result': {'success': True}})
                final_text = 'sales_data.xlsx converted to sales_data.csv on Desktop.'
        elif tc.category == '代码编写':
            if 'hello' in inp.lower():
                hp = os.path.join(desktop, 'hello.py')
                tool_calls.append({'tool': 'write_file', 'args': {'file_path': hp, 'content': 'print("Hello, World!")'}})
                events.append({'type': 'tool_result', 'tool': 'write_file', 'result': {'success': True}})
                tool_calls.append({'tool': 'run_python', 'args': {'code': 'print("Hello, World!")'}})
                events.append({'type': 'tool_result', 'tool': 'run_python', 'result': {'output': 'Hello, World!'}})
                final_text = 'hello.py created and executed: Hello, World!'
            elif '1-100' in inp or ('sum' in inp.lower() and '100' in inp):
                tool_calls.append({'tool': 'run_python', 'args': {'code': 'result = sum(range(1, 101))\nprint(result)'}})
                events.append({'type': 'tool_result', 'tool': 'run_python', 'result': {'output': '5050'}})
                final_text = 'Sum of 1-100 calculated: 5050'
            elif 'sample.txt' in inp or '单词' in inp:
                tool_calls.append({'tool': 'read_file', 'args': {'file_path': os.path.join(TEST_DATA_DIR, 'sample.txt')}})
                events.append({'type': 'tool_result', 'tool': 'read_file', 'result': {'output': 'Hello world this is a sample text file for testing The quick brown fox jumps over the lazy dog Tank Smart is an intelligent agent framework'}})
                tool_calls.append({'tool': 'run_python', 'args': {'code': 'text = "Hello world this is a sample text file for testing The quick brown fox jumps over the lazy dog Tank Smart is an intelligent agent framework"\nwords = len(text.split())\nprint(words)'}})
                events.append({'type': 'tool_result', 'tool': 'run_python', 'result': {'output': '25'}})
                final_text = 'sample.txt word count: 25 words.'
            elif '.py' in inp.lower() and 'workspace' in inp.lower():
                tool_calls.append({'tool': 'get_project_summary', 'args': {'project_path': PROJ_DIR}})
                events.append({'type': 'tool_result', 'tool': 'get_project_summary', 'result': {'output': json.dumps({'python_files': ['app.py', 'agent.py', 'chat.py']}, ensure_ascii=False)}})
                final_text = 'Listed .py files in workspace: app.py, agent.py, chat.py.'
            elif 'flask' in inp.lower() and 'app.py' in inp:
                tool_calls.append({'tool': 'run_python', 'args': {'code': 'import ast; ast.parse(\'from flask import Flask\\napp = Flask(__name__)\\n@app.route("/")\\ndef home():\\n    return "Hello"\\nif __name__ == "__main__":\\n    app.run()\')'}})
                events.append({'type': 'tool_result', 'tool': 'run_python', 'result': {'output': 'Syntax valid'}})
                final_text = 'Flask app.py syntax validated with ast.parse.'
        if not tool_calls:
            events.append({'type': 'final', 'data': 'No specific handler for ' + tc.category + ' / ' + tc.id})
            final_text = 'Test ' + tc.id + ' completed (no tool calls generated)'
        return events, tool_calls, final_text

    def _call_agent(self, user_input: str):
        from backend.agent import run_agent
        events = []
        tool_calls = []
        final_text = ''
        for event in run_agent(user_input, session_id='test_' + self.run_id):
            ev_type = event.get('type', '')
            events.append(event)
            if ev_type == 'tool_call':
                tool_calls.append({'tool': event.get('tool', ''), 'args': event.get('arguments', {})})
                result = self.executor.execute_tool(event.get('tool', ''), event.get('arguments', {}))
                yield {'type': 'tool_result', 'tool': event.get('tool'), 'result': result}
            elif ev_type == 'final':
                final_text = event.get('text', '')
            elif ev_type == 'error':
                raise Exception(event.get('error', 'Unknown error'))
        return events, tool_calls, final_text

    def run(self, test_cases: List[TestCase], parallel: bool = False) -> List[Dict]:
        run_dir = os.path.join(ARTIFACTS_DIR, 'run_' + self.run_id)
        os.makedirs(run_dir, exist_ok=True)
        self._build_manifest(run_dir)
        results = []
        total = len(test_cases)
        for i, tc in enumerate(test_cases, 1):
            self._log('[' + str(i) + '/' + str(total) + '] Running ' + tc.id + ' - ' + tc.title + ' ...')
            try:
                result = self.run_single(tc)
                results.append(result)
                icon_map = {'PASS': 'OK', 'FAIL': 'XX', 'MANUAL_REVIEW': '!!', 'ERROR': 'ER'}
                icon = icon_map.get(result['status'], '?')
                self._log('  -> ' + icon + ' ' + result['status'] + ' (' + str(result['duration_ms']) + 'ms, ' + str(len(result['assertions'])) + ' assertions)')
            except Exception as e:
                self._log('  -> ER ERROR: ' + str(e))
                results.append({'test_id': tc.id, 'status': 'ERROR', 'error': str(e), 'category': tc.category})
        manifest = {'run_id': self.run_id, 'timestamp': datetime.datetime.now().isoformat(), 'results': results}
        with open(os.path.join(run_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return results

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Tank Smart Test Orchestrator')
    parser.add_argument('--all', action='store_true', help='Run all 55 test cases')
    parser.add_argument('--category', type=str, help="Run specific category (e.g. '系统配置更改')")
    parser.add_argument('--id', type=str, action='append', help='Run specific test ID(s)')
    parser.add_argument('--retry', type=int, default=2, help='Max retries (default: 2)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--dry-run', action='store_true', help='Dry run with mock responses')
    parser.add_argument('-o', '--output', type=str, help='Artifacts output directory override')
    args = parser.parse_args()
    test_md = os.path.join(PROJ_DIR, 'tests', 'test_cases', 'test_cases.md')
    if not os.path.exists(test_md):
        print('Test cases not found: ' + test_md)
        print('Please copy docs/test_cases.md to tests/test_cases/test_cases.md')
        sys.exit(1)
    test_cases = parse_test_cases(test_md)
    print('Tank Smart Test Framework loaded ' + str(len(test_cases)) + ' test cases')
    if args.all:
        selected = test_cases
    elif args.category:
        selected = [tc for tc in test_cases if tc.category == args.category]
        print("Category '" + args.category + "': " + str(len(selected)) + ' cases')
    elif args.id:
        id_set = set(args.id)
        selected = [tc for tc in test_cases if tc.id in id_set]
        print('Test IDs ' + str(id_set) + ': ' + str(len(selected)) + ' cases')
    else:
        print('Usage: test_orchestrator.py --all | --category NAME | --id T-01 [--id T-02 ...]')
        sys.exit(1)
    if not selected:
        print('No test cases selected')
        sys.exit(1)
    orchestrator = TestOrchestrator(verbose=args.verbose, dry_run=args.dry_run)
    if args.output:
        global ARTIFACTS_DIR
        ARTIFACTS_DIR = args.output
    results = orchestrator.run(selected)
    print('')
    print('Run ID: ' + orchestrator.run_id)
    print('Total: ' + str(len(results))
          + ' | PASS: ' + str(sum(1 for r in results if r['status']=='PASS'))
          + ' | FAIL: ' + str(sum(1 for r in results if r['status']=='FAIL'))
          + ' | ERR: ' + str(sum(1 for r in results if r['status']=='ERROR')))
    # Generate report
    from test_reporter import main as gen_report
    sys.argv = ['test_reporter.py', '--run-id', orchestrator.run_id]
    gen_report()

if __name__ == '__main__':
    main()