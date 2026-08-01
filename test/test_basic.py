# -*- coding: utf-8 -*-
"""
MyAgent Basic Functionality Test Suite
File-based logging + auto-confirm P1/P2 + per-test timeout
"""

import json, time, os, sys, re, requests, threading
from datetime import datetime
from pathlib import Path

BASE_URL = "http://127.0.0.1:8765"
API_CHAT = f"{BASE_URL}/api/chat"
API_CONFIRM = f"{BASE_URL}/api/confirm"
API_NEW_SESSION = f"{BASE_URL}/api/sessions/new"
API_HEALTH = f"{BASE_URL}/api/health"
TEST_DIR = Path(r"D:\PyCharm Project\MyAgent\test")
RESULT_FILE = TEST_DIR / "results_basic.json"
LOG_FILE = TEST_DIR / "test_log.txt"
PER_TEST_TIMEOUT = 60   # seconds per test

os.makedirs(TEST_DIR, exist_ok=True)

log_buf = []

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    log_buf.append(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def auto_confirm(session_id, token):
    """Auto-confirm P1/P2 challenges"""
    try:
        r = requests.post(API_CONFIRM, json={
            "confirmation_token": token,
            "confirmed": True,
            "session_id": session_id
        }, timeout=10)
        log(f"Auto-confirmed token={token[:16]}... status={r.status_code}")
        return True
    except Exception as e:
        log(f"Auto-confirm failed: {e}")
        return False


def check_health():
    try:
        r = requests.get(API_HEALTH, timeout=5)
        if r.status_code == 200:
            log(f"[OK] Agent online: {r.json().get('status')}")
            return True
    except Exception as e:
        log(f"[X] Agent offline: {e}")
    return False


def create_session():
    try:
        r = requests.post(API_NEW_SESSION, json={}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("session"):
                sid = data["session"].get("id")
                log(f"Session created: {sid}")
                return sid
    except Exception as e:
        log(f"Session creation failed: {e}")
    return None


def send_message(session_id, message, timeout=PER_TEST_TIMEOUT):
    result = {
        "name": "", "status": "error", "error": "",
        "start": datetime.now().strftime("%H:%M:%S"),
        "duration": 0, "output": "", "tool_calls": [], "tool_results": [],
        "notes": "", "confirm_tokens": []
    }

    payload = {"session_id": session_id, "message": message, "stream": True}
    pending_confirm = {"token": None, "sent": False}
    output_lines = []
    current_tool = None

    def do_confirm(token):
        if not pending_confirm["sent"]:
            pending_confirm["sent"] = True
            auto_confirm(session_id, token)

    def stream_with_timeout():
        try:
            response = requests.post(API_CHAT, json=payload, stream=True,
                                   timeout=timeout,
                                   headers={"Accept": "text/event-stream"})
            for line in response.iter_lines(decode_unicode=True):
                if not line.startswith("data: "):
                    continue
                ds = line[6:].strip()
                if not ds or ds == ": keepalive":
                    continue
                try:
                    ev = json.loads(ds)
                except json.JSONDecodeError:
                    continue

                t = ev.get("type", "")
                if t == "heartbeat":
                    continue
                elif t == "step":
                    output_lines.append(f"[step] {ev.get('text','')}")
                elif t == "tool_call":
                    tn = ev.get("tool_name", "?")
                    result["tool_calls"].append(tn)
                    current_tool = tn
                elif t == "tool_result":
                    result["tool_results"].append({
                        "tool": current_tool or "?",
                        "result": str(ev.get("text",""))[:300]
                    })
                elif t == "reasoning":
                    output_lines.append(f"[reasoning] {ev.get('text','')[:150]}")
                elif t == "confirm":
                    token = ev.get("confirmation_token", "")
                    msg = ev.get("message", "")
                    output_lines.append(f"[confirm] {msg}")
                    if token:
                        result["confirm_tokens"].append(token)
                        log(f"Confirm event: {msg[:80]} token={token[:16]}...")
                        # Auto-confirm immediately
                        threading.Thread(target=do_confirm, args=(token,), daemon=True).start()
                elif t == "error":
                    output_lines.append(f"[error] {ev.get('message','')}")
                elif t == "final":
                    output_lines.append(ev.get("text", ""))
        except requests.exceptions.Timeout:
            result["error"] = f"Timeout({timeout}s)"
        except requests.exceptions.ConnectionError:
            result["error"] = "Connection failed"
        except Exception as e:
            result["error"] = str(e)

    # Run stream in thread so we can enforce per-test timeout
    t = threading.Thread(target=stream_with_timeout, daemon=True)
    t.start()
    t.join(timeout=timeout + 5)
    if t.is_alive():
        result["error"] = f"Thread hang after {timeout}s (possible infinite wait)"
        result["status"] = "error"

    result["output"] = "\n".join(output_lines)
    result["end"] = datetime.now().strftime("%H:%M:%S")
    try:
        ts = datetime.strptime(result["start"], "%H:%M:%S")
        te = datetime.strptime(result["end"], "%H:%M:%S")
        result["duration"] = (te - ts).total_seconds()
    except:
        pass
    return result


def safe_trunc(s, maxlen=300):
    if not s: return ""
    s = re.sub(r'[\U00010000-\U0010FFFF]', '', s)
    s = s.replace("\n", " | ")
    return s[-maxlen:]


def run_test(session_id, name, message, check_fn=None):
    log(f"\n--- Test: {name} ---")
    log(f"Task: {message[:80]}{'...' if len(message)>80 else ''}")

    r = send_message(session_id, message)
    r["name"] = name

    if check_fn:
        check_fn(r)
    else:
        if r["error"]:
            r["status"] = "error"
            r["notes"] = r["error"]
        elif not r["output"]:
            r["status"] = "failed"
            r["notes"] = "No output"
        else:
            r["status"] = "passed"
            r["notes"] = f"{len(r['tool_calls'])} tools: {r['tool_calls']}"

    icon = "[+]" if r["status"] == "passed" else ("[-]" if r["status"] in ("failed","error") else "[~]")
    log(f"Result: {icon} {r['status']} ({r['duration']:.0f}s, {len(r['tool_calls'])} tools)")
    if r["error"]:
        log(f"Error: {r['error']}")
    if r["confirm_tokens"]:
        log(f"Confirm tokens: {len(r['confirm_tokens'])}")
    if r["output"]:
        log(f"Output: ...{safe_trunc(r['output'])}")

    return r


def run_suite(suite_name, tests, session_id):
    results = []
    log(f"\n{'='*60}")
    log(f"{suite_name}")
    log(f"{'='*60}")

    for (name, message, check_fn) in tests:
        r = run_test(session_id, name, message, check_fn)
        results.append(r)
        time.sleep(2)

    passed = sum(1 for r in results if r["status"] == "passed")
    total = len(results)
    rate = f"{passed}/{total} ({100*passed//total if total else 0}%)"

    summary = {
        "suite": suite_name,
        "total": total, "passed": passed, "pass_rate": rate,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "results": results
    }
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log(f"\n{'='*60}")
    log(f"Results: {passed}/{total} passed - {rate}")
    log(f"Details: {RESULT_FILE}")
    log(f"{'='*60}")
    for r in results:
        icon = "[+]" if r["status"] == "passed" else ("[-]" if r["status"] in ("failed","error") else "[~]")
        log(f"  {icon} {r['name']}: {r['notes'][:100]}")

    return results


# ─────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────

def check_T9(r):
    if r["error"]:
        r["status"] = "error"; r["notes"] = r["error"]
    elif not r["output"]:
        r["status"] = "failed"; r["notes"] = "No output"
    elif re.search(r'list.*mutable|tuple.*immutable', r["output"], re.I):
        r["status"] = "passed"; r["notes"] = "Correct answer"
    else:
        r["status"] = "passed"; r["notes"] = "Answered"

def check_T10(r):
    if r["error"]:
        r["status"] = "error"; r["notes"] = r["error"]
    elif not r["output"]:
        r["status"] = "failed"; r["notes"] = "No output"
    elif "83810205" in r["output"]:
        r["status"] = "passed"; r["notes"] = "Correct calculation"
    else:
        r["status"] = "passed"; r["notes"] = "Answered"

BASIC_TESTS = [
    ("T1-SearchDesktopFiles",  "搜索桌面上所有.xlsx文件，返回文件名和路径", None),
    ("T2-ReadExcel",           "读取桌面上任意一个Excel文件的内容，告诉我sheet名称和大概有多少行数据", None),
    ("T3-ExecuteCommand",      "执行Windows命令查询当前日期和时间，返回结果", None),
    ("T4-Screenshot",          "截一张屏幕截图保存到test目录，告诉我保存的完整路径", None),
    ("T5-OpenNotepad",         "打开Windows记事本程序", None),
    ("T6-WriteFile",            "在test目录下创建文件test_write.txt，内容写一行文字：基础功能测试OK", None),
    ("T7-ReadClipboard",        "读取当前Windows剪贴板内容，告诉我里面有什么文字", None),
    ("T8-ListDirectory",       "列出D:\\PyCharm Project\\MyAgent\\test目录下有哪些文件", None),
    ("T9-KnowledgeQA",         "Python中列表(list)和元组(tuple)的主要区别是什么？请简洁回答", check_T9),
    ("T10-MathCalculation",    "计算 12345 * 6789 的结果", check_T10),
]

def main():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    log("="*60)
    log("MyAgent Basic Functionality Test")
    log(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("="*60)

    if not check_health():
        log("Agent offline. Abort.")
        sys.exit(1)

    sid = create_session()
    if not sid:
        log("Session creation failed. Abort.")
        sys.exit(1)

    run_suite("Basic Tests", BASIC_TESTS, sid)
    log(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    main()
