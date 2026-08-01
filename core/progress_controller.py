import hashlib, json, re, time
from enum import Enum

class Kind(str, Enum):
    READ, MUTATOR, EXEC, SEARCH, INTERACT = \
        "idempotent_read", "mutator", "execute", "search", "interact"

class Verdict(str, Enum):
    PROGRESS, STALLED, FUTILE = "progress", "stalled", "futile"

# 默认保守(READ)；只覆盖你确知的关键工具。未列出的工具走 READ 策略（比现状强，且不误杀）。
CONTRACTS = {
    "read_file": Kind.READ, "list_dir": Kind.READ, "get_uninstall_string": Kind.READ,
    "write_file": Kind.MUTATOR, "edit_file": Kind.MUTATOR,
    "delete_file": Kind.MUTATOR, "modify_registry": Kind.MUTATOR,
    "execute_command": Kind.EXEC, "web_search": Kind.SEARCH, "grep": Kind.SEARCH,
}

def _h(x):
    return hashlib.md5(json.dumps(x, sort_keys=True, ensure_ascii=False,
                                  default=str).encode()).hexdigest()

# 轮询启发式：误判方向是"放宽"，且有预算兜底，不会死循环（R9 注）
_POLL_RE = re.compile(r"(tasklist|findstr|ping\s+-n|Start-Sleep|timeout\s+/\d|while\s+|until\s+|--watch)", re.I)

class ProgressEvaluator:
    def __init__(self, futile_after_stall=2,
                 poll_max=20, poll_budget_s=120, gain_thr=0.15):
        self.win, self.stall = [], {}
        self.cfg = dict(futile_after_stall=futile_after_stall,
                        poll_max=poll_max, poll_budget_s=poll_budget_s, gain_thr=gain_thr)

    def decide(self, tool, args, result, changed=None, polling=None):
        kind = CONTRACTS.get(tool, Kind.READ)
        if kind == Kind.INTERACT:
            return Verdict.FUTILE, "交互类不应循环，应转引擎确认"

        ah, rh = _h(args), _h(result)
        polling = polling if polling is not None else bool(_POLL_RE.search(str(args)))
        hist = [r for r in self.win if r["tool"] == tool][-6:]      # 不含当前
        same_a = sum(r["ah"] == ah for r in hist)
        same_r = sum(r["rh"] == rh for r in hist)
        # R12 修正：存词集 tok 供增益计算（不存 raw，省内存且直接可用）
        self.win.append(dict(tool=tool, ah=ah, rh=rh, kind=kind, t=time.time(),
                             polling=polling,
                             tok=set(re.findall(r"\w+", str(result)[:2000]))))
        if len(self.win) > 100:
            self.win = self.win[-100:]

        if kind == Kind.READ:
            bad = same_a and same_r                       # 同参+同果=空转；同参异果=监控，放行
        elif kind == Kind.MUTATOR:
            bad = (changed is False) or (same_a and same_r)   # 没真改 / 重复写同内容
        elif kind == Kind.SEARCH:
            bad = same_a or self._low_gain(hist)          # 同query / 没新料
        elif kind == Kind.EXEC:
            if polling:
                return self._poll_budget(ah)              # 轮询走预算（有用也设上限）
            bad = same_a and same_r                       # 非轮询卡住重试
        else:
            bad = False

        return (Verdict.PROGRESS, "ok") if not bad else self._stall(tool, ah, kind)

    def _stall(self, tool, ah, kind):
        k = f"{tool}:{ah}"
        self.stall[k] = self.stall.get(k, 0) + 1
        if self.stall[k] >= self.cfg["futile_after_stall"]:
            return Verdict.FUTILE, f"[{kind.value}] 连续原地踏步，判为无用功，强制收敛"
        return Verdict.STALLED, (f"[{kind.value}] 疑似原地踏步，禁止重复上一步，"
                                 f"换参数/换工具或直接 final")

    def _poll_budget(self, ah):
        runs = [r for r in self.win if r["ah"] == ah and r["polling"]]
        if len(runs) > self.cfg["poll_max"] or \
           (runs and time.time() - runs[0]["t"] > self.cfg["poll_budget_s"]):
            return Verdict.FUTILE, "轮询等待超预算，未观察到预期变化，停止等待"
        return Verdict.PROGRESS, "polling: 等待中，放行"

    def _low_gain(self, hist):
        cur = self.win[-1]["tok"]
        old = set().union(*(r["tok"] for r in hist)) if hist else set()
        if not cur:
            return True
        return len(cur - old) / len(cur) < self.cfg["gain_thr"]   # 新词占比太低=没新信息
