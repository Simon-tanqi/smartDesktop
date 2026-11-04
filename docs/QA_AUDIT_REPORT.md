# smartDesktop 质量审计报告

- **审计对象**：`D:\smartDesktop`（main @ `156c274`，含本地修复提交）
- **审计日期**：2026-11-04
- **方法**：全模块导入、工具声明/注册表比对、纯函数边界探针、Flask 测试客户端接口冒烟、文档与实现逐条对照、冗余文件扫描
- **未覆盖**：真实 LLM 端到端 70 用例（缺 API Key）、7×24 稳定性、真实磁盘/注册表破坏性操作

---

## 一、结论概览

| 级别 | 数量 | 说明 |
|---|---|---|
| P0 高危 | 2 | 路径保护可绕过、上传路径穿越 |
| P1 严重 | 6 | 幻影工具、技能域失配、500 错误、空消息、循环导入等 |
| P2 中等 | 7 | 上下文预算、重复实现、文档漂移、死文件 |
| P3 建议 | 4 | 上传大小、空壳模块、统计口径 |

**基础健康度**：61 个模块全部可导入；`pytest` 采集 386 项；CI 范围 262 项、安全回归 116 项全绿；L0/L1 直答与 SSE 心跳链路正常；无 API Key 时能优雅返回错误事件。

---

## 二、P0 高危（本轮已修复）

### P0-1 Windows 长路径前缀绕过所有路径保护

**证据**
- `core/safety_policy.py:34-38` `norm_sensitive_path` 只做小写/斜杠/引号处理，不解析 `\\?\`、`\\.\`、组件尾点
- `core/safety_policy.py:41-70` 读取/复制保护均基于字符串前缀
- `core/tool_data_provider.py:1330-1365` 写入 P0 判定基于 `os.path.normpath` + 盘符，`\\?\C:\...` 盘符识别失败返回 `None`
- `core/tool_data_provider.py:1439` 删除 P0 判定同样受影响

**复现**（已实测）
```python
match_sensitive_read_root(r"\\?\C:\Windows\System32\config\SAM")        # -> None（应拦截）
_check_single_file_edit_p0("write_file", {"file_path": r"\\?\C:\Windows\System32\drivers\etc\hosts"})  # -> None
_check_delete_p0({"file_path": r"\\?\C:\Windows\System32\drivers\etc\hosts"})  # -> None
```
`C:\Windows\System32`、`c:/windows/...`、`C:\Windows\..\Windows` 等普通变体可正常拦截，只有设备路径前缀失效。

**影响**：模型被提示注入后可写/删/读系统目录，P0 硬拒绝被绕过（后续仍会走 P1/P2 确认，但确认层级与“永久拒绝”语义不符）。
**建议**：统一在入口做 `os.path.abspath(os.path.realpath(...))` 前先剥离 `\\?\`、`\\.\`；对 NTFS 组件尾点/空格做规范化；补 2 条回归用例。

### P0-2 上传接口路径穿越（session_id 与文件名未净化）

**证据**
- `backend/upload_api.py:45-60`：`session_id` 直接 `os.path.join(UPLOAD_ROOT, session_id)`；`unique_name = f"{uuid}_{file.filename}"` 未用 `secure_filename`
- `memory/config.py:202-206`：`get_session_dir` 同样直接拼接 `session_id` 并 `makedirs`

**复现**：`POST /api/upload` 传 `session_id=..\..\..\Users\Public\pwn`、文件名 `..\..\x.txt`（扩展名需在白名单内，白名单含 `.py/.exe/.msi`），文件会落到上传根目录之外。
**影响**：任意目录创建 + 任意位置写文件（当前服务默认仅监听 127.0.0.1，属于本机攻击面；若改 `AGENT_HOST=0.0.0.0` 则升级为远程任意写）。
**建议**：`session_id` 限定 `[A-Za-z0-9_-]{1,64}`；文件名走 `werkzeug.utils.secure_filename`；落盘前 `os.path.realpath` 校验仍在 `UPLOAD_ROOT` 下。

---

## 三、P1 严重（本轮已修复 5 项，1 项复核为非缺陷）

### P1-1 幻影工具：声明了但没实现，还会被注入给模型

`prompts/tools.yaml` 声明 55 个工具，注册表 85 个；其中 **4 个声明未实现**：
`append_to_log`、`display_config`、`get_weather`、`remember_user_info`。

域加载不做注册表过滤（`tools/__init__.py:136-172` + `core/tool_data_provider.py:403` `build_api_tools_param`），实测：
```
system_basic  -> 注入 13 个工具，其中含 display_config（未注册）
web_browse    -> 注入 14 个工具，其中含 get_weather（未注册）
```
**影响**：模型看到不存在的工具就会调用，必然失败并浪费轮次，是“任务跑偏/反复重试”的典型来源。
**建议**：`build_api_tools_param` 输出前与 `TOOL_REGISTRY` 求交；或直接从 tools.yaml 删除这 4 条。

### P1-2 技能域与提示词文件对不上（10 个域里 5 个没有技能说明）

`tools/__init__.py:255-274` 按 `prompts/skills/{domain}.md` 取名，实测：

| 域 | 提示词 |
|---|---|
| code_dev / excel_analyze / gui_automation / system_basic / system_high_risk | **缺失（长度 0）** |
| db_query / office_ppt / office_word / user_memory / web_browse | 正常 |

而 `prompts/skills/` 下的 `code_generate.md`、`data_excel.md`、`system_config.md`、`general_chat.md` 没有被任何域引用 → 死文件。
**影响**：README 宣称“技能域按需加载”，实际一半域只加了工具、没有加技能约束，模型行为与文档不符。
**建议**：建立 domain → skill 文件映射表（或在 tools.yaml 里显式写 `skill: data_excel.md`），并对缺失给出告警。

### P1-3 会话保存接口缺 id 时 500

`backend/chat.py:905` 的 `saved_count` 定义在 `if session_id:` 内，`chat.py:949` 的 return 在其外。
**复现**：`POST /api/sessions {"title":"x"}` → `500 {"技术详情": "cannot access local variable 'saved_count' ..."}`
**建议**：`saved_count/total` 提到 `if` 之前，缺 id 返回 400。

### P1-4 空消息不被拒绝，直接进入完整 Agent 流程

`backend/chat.py:313` 只做 `strip()`，未校验空串。实测 `message=""` 与 `"   "` 均返回 200，并依次发出“开始处理任务 → 正在调用模型思考”，在无 Key 时才报错。
**影响**：白白消耗一次完整链路（含 session 落库、意图路由、上下文构建、LLM 调用）。
**建议**：入口处空消息直接返回 400 或本地提示。

### P1-5 `import tools` 单独导入即失败（循环依赖）

`tools/__init__.py:65-71` → `core.tool_data_provider` → `core/__init__.py` → `core.agent_state` → `from tools import build_dynamic_system_prompt`（此时尚未定义）→ ImportError。
实测：`python -c "import tools"` 失败；`python -c "import core; import tools"` 成功。
**影响**：任何以 `tools` 为入口的脚本/新模块/第三方集成都会炸；当前能跑是因为 app 先导入了 core。
**建议**：把 `core/__init__.py` 的聚合导入改为惰性（`__getattr__`），或让 `agent_state` 在函数内延迟 import tools。

### P1-6 三个工具没有参数 schema，其中一个还是 always_inject

`invalidate_config_cache`、`get_workspace_path`、`gui_active_window` 注册时 `parameters` 为空；`get_workspace_path` 属于 `always_inject`，每轮都暴露给模型却没有参数定义。
**影响**：模型无法知道参数，只能猜；`get_workspace_path` 无参尚可，前两个需要参数时必错。
**建议**：补齐 docstring 里的 Args 或显式声明 schema。

---

## 四、P2 中等

### P2-1 上下文预算被系统提示词吃掉大半

`core/react_engine.py:86-87`：`MAX_CONTEXT_TOKENS=28000`、`SAFETY_MARGIN=4000`（阈值 24000）。实测：
- 基础 system prompt：21881 字符 ≈ **17010 tokens（61%）**
- 加载 2 个技能域：26787 字符 ≈ **19836 tokens（71%）**
- 默认 10 个工具 schema：≈2300 tokens

**影响**：留给“历史 + 当前问题 + 工具结果”的预算不足 4k tokens，压缩会被频繁触发，长任务容易丢上下文。
**建议**：静态提示词瘦身/按需拼接；把工具清单改成摘要+按需展开；预算按模型上下文动态计算，而不是写死 28000。

### P2-2 同名函数多份实现（潜在行为分叉）

| 函数 | 位置 |
|---|---|
| `build_dynamic_system_prompt` | `memory/session.py` 与 `tools/__init__.py` |
| `add_compressed_summary` | `memory/compressor.py` 与 `memory/long_term.py` |
| `get_llm_request_logger` / `get_tool_execution_logger` / `get_validation_logger` | `logger.py` 与 `core/logger.py` |
| `_get_confirmation_manager` | `tools/safe_exec.py`、`system_tools.py`、`web_download.py` |
| `_ollama_call` | `llm_client.py` 与 `memory/llm.py` |
| `verify_uninstall` | `tools/post_action_verify.py` 与 `tools/system_tools.py` |

**建议**：至少确认 `memory/session.py` 的 `build_dynamic_system_prompt` 是否仍在用；不用的删除，用的合并。

### P2-3 文档与实际不一致

- `README.md:258` 标注 `react_engine.py（~457 行）`，实际 470 行
- `docs/TEST_SUITE.md` 3.2/4.1/4.2 列了首字 <500ms、内存 <500MB、10 并发、7×24 等指标，仓库内没有任何测量记录
- `tests/test_cases/test_cases.md:1322` 仍写“域加载功能暂不可用”，与现状（可加载但含幻影工具）不符
- `README.md:13` 的测试口径已在本轮修正为 CI 262 / 全仓 386

### P2-4 死代码 / 空壳 / 未引用资源

| 对象 | 状态 |
|---|---|
| `core/agent.py` | 无任何 import 引用 |
| `backend/services.py` | 无任何 import 引用 |
| `memory/episode_store.py` | 未被 `memory/__init__.py` 或其他模块引用（仅注释提及） |
| `tools/db_tools.py` | 5 行空壳，0 个工具，但 README 标注“数据库操作”，`db_query` 域因此加载到 0 个工具 |
| `prompts/modes/plan.md` | 无 persona 引用 |
| `prompts/skills/general_chat.md`、`code_generate.md`、`data_excel.md`、`system_config.md` | 无域引用 |
| `web/api/requirements.txt` | 仅 flask+requests 的旧声明，未参与安装 |
| `tests/checkpoint.json` | 已入库，却同时出现在 `.gitignore`（规则对已跟踪文件无效，易误导） |

### P2-5 批量删除判定可被字符串绕过

`_collect_delete_targets({"paths": "a;b"})` → `(["a;b"], False)`：分号/逗号拼接的路径被当成单个目标，批量删除的 P1 挑战降级为 P2 普通确认。
**建议**：对字符串值按 `;`/`,`/换行切分后再计数。

### P2-6 存储型 XSS（会话标题）

`web/index.html:1221-1226` 把 `session.title`、`session.id` 直接插进 `innerHTML`，未 `escapeHtml`。重命名会话为 `<img src=x onerror=alert(1)>` 即可在会话列表执行脚本。模型输出路径（`renderMarkdown`）是转义的，这里漏了。

### P2-7 库函数对 None 不防御

实测：`tokenize(None)`、`ContextSwitchDetector.detect(None)`、`estimate_messages_tokens(None)` 均抛异常。当前 API 层不会传入 None，但作为库接口偏脆。

---

## 五、P3 建议

1. `app.config` 未设 `MAX_CONTENT_LENGTH`，上传 100MB 限制在 Flask 已缓冲落地之后才检查，无法防止大文件占满临时盘。
2. `/api/confirm` 无效令牌返回 404，语义上应为 400/410。
3. README “15 个内置工具模块”中含 0 工具的 `db_tools`，建议改为“14 个可用工具模块 + 1 个占位”。
4. `docs/TEST_SUITE.md` 的手动清单仍写“等待 5 分钟不确认”，实际 TTL 300s，建议统一表述。

---

## 六、修复优先级建议

1. **今天**：P0-1（`\\?\` 规范化）、P0-2（session_id/文件名净化）、P1-3（500）、P1-1（幻影工具过滤）
2. **本周**：P1-2（域→技能映射）、P1-4（空消息）、P1-5（循环依赖）、P1-6（schema）
3. **迭代内**：P2-1（预算重构）、P2-4（删死文件）、P2-3（文档校准）、P2-5/P2-6

---

## 七、修复记录（本轮，P0 + P1）

| 编号 | 修复内容 | 代码位置 | 回归测试 |
|---|---|---|---|
| P0-1 | 新增 `canonicalize_win_path`：剥离 `\\?\` / `\\.\` / `\\?\UNC\`、统一分隔符、去组件尾点空格；读取/复制/写入/删除四类判定统一走它 | `core/safety_policy.py:34-75`、`core/tool_data_provider.py:1319-1345` | `tests/test_core/test_path_protection.py` |
| P0-2 | `session_id` 限定 `[A-Za-z0-9_-]{1,64}`；上传文件名净化（保留中文，去掉路径分隔符/非法字符）；落盘前 `realpath` 校验仍在上传根内 | `backend/upload_api.py:34-83`、`memory/config.py:202-215`、`backend/chat.py:31-39` | `tests/test_api_boundary.py` |
| P1-1 | 注入前按运行时注册表过滤，幻影工具（`display_config`/`get_weather` 等）不再进入 schema 与域工具列表 | `core/tool_data_provider.py:301-310, 351-372` | `tests/test_core/test_tool_declaration.py` |
| P1-2 | 新增 `DOMAIN_SKILL_MAP`：`code_dev→code_generate`、`excel_analyze→data_excel`、`system_basic/system_high_risk→system_config`；`gui_automation` 因仓库无对应技能文件仍为空 | `tools/__init__.py:21-31, 262-270` | `tests/test_core/test_tool_declaration.py` |
| P1-3 | `/api/sessions` 缺 id 返回 400，`saved_count/total` 提前初始化，消除 500 | `backend/chat.py:899-910, 955` | `tests/test_api_boundary.py` |
| P1-4 | `/api/chat` 空消息/空白消息返回 400，不再进入完整 Agent 链路 | `backend/chat.py:320-327` | `tests/test_api_boundary.py` |
| P1-5 | `core/__init__.py` 改为惰性导出 `agent_state` / `react_engine`，`import tools` 单独导入不再触发循环依赖 | `core/__init__.py` | 全量回归 |
| P1-6 | **复核为非缺陷**：`get_workspace_path`、`invalidate_config_cache`、`gui_active_window` 本身无参数，空 `parameters` 是正确 schema，未改动 | — | — |

**验证**：`pytest` 全量 **408 passed**；CI 范围 **277 passed**（新增 13 项 test_core 回归）；`compileall` 通过；改动文件 `ruff` 全绿，仓库既有告警 78 → 77（未新增）。
