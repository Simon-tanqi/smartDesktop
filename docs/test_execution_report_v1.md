# Tank Smart 测试报告 v1.0

**生成时间**: 2026-08-05 10:25 GMT+8  
**测试框架**: Tank Smart Test Suite  
**测试运行 ID**: `run_20260805_001525`  
**执行引擎**: MyAgent + Ollama Qwen3.5-9B（Q4_K_M）  
**测试用例数**: 55（文档定义） / 55（实际执行）

---

## 📊 执行总览

| 指标 | 数值 |
|------|------|
| ✅ PASS | 25 |
| 🔍 MANUAL_REVIEW（设计预期） | 11 |
| ❌ FAIL | 19 |
| **总计** | **55** |
| 可评分通过率 | **56.8%**（25/44，排除MANUAL_REVIEW） |

> **说明**：MANUAL_REVIEW 类 11 题均为设计预期（卸载、系统配置修改等需人工介入的操作），不影响通过率。

---

## 📋 详细结果一览

| 题号 | 类别 | 状态 | 重试 | 耗时 |
|------|------|------|------|------|
| T-01 | 系统配置 | ✅ PASS | r0 | 9.1s |
| T-02 | 系统配置 | ✅ PASS | r0 | 28.6s |
| T-03 | 系统配置 | ✅ PASS | r0 | 23.2s |
| T-04 | 系统配置 | 🔍 MANUAL_REVIEW | r0 | 25.8s |
| T-05 | 系统配置 | ✅ PASS | r0 | 39.2s |
| T-06 | 系统配置 | 🔍 MANUAL_REVIEW | r0 | 9.0s |
| T-07 | 系统配置 | ✅ PASS | r0 | 11.8s |
| T-08 | 软件安装 | ✅ PASS | r0 | 9.1s |
| T-09 | 软件安装 | ✅ PASS | r0 | 27.0s |
| T-10 | 软件安装 | ✅ PASS | r0 | 16.2s |
| T-11 | 软件安装 | ✅ PASS | r0 | 22.2s |
| T-12 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 1.8min |
| T-13 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 8.6s |
| T-14 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 11.5s |
| T-15 | 软件安装 | ✅ PASS | r0 | 8.1s |
| T-16 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 19.6s |
| T-17 | 软件安装 | ✅ PASS | r0 | 6.5s |
| T-18 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 8.1s |
| T-19 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 11.5s |
| T-20 | 软件安装 | 🔍 MANUAL_REVIEW | r0 | 1.1min |
| T-21 | 网页浏览 | ✅ PASS | r0 | 8.9s |
| T-22 | 网页浏览 | ✅ PASS | r0 | 41.6s |
| T-23 | 网页浏览 | ✅ PASS | r0 | 10.1s |
| T-24 | 网页浏览 | 🔍 MANUAL_REVIEW | r0 | 2.4min |
| T-25 | 网页浏览 | ❌ FAIL | r0 | 4.2s |
| T-26 | 网页浏览 | ✅ PASS | r0 | 24.6s |
| T-27 | 网页浏览 | 🔍 MANUAL_REVIEW | r0 | 29.0s |
| T-28 | 网页浏览 | ✅ PASS | r0 | 36.5s |
| T-29 | Word | ✅ PASS | r0 | 12.7s |
| T-30 | Word | ❌ FAIL | r0 | 1.6s |
| T-31 | Word | ❌ FAIL | r0 | 2.3s |
| T-32 | Word | ❌ FAIL | r0 | 1.9s |
| T-33 | Word | ✅ PASS | r1 | 32.8s |
| T-34 | Word | ❌ FAIL | r0 | 3.8s |
| T-35 | Word | ❌ FAIL | r0 | 110.2s |
| T-36 | PPT | ❌ FAIL | r0 | 42.1s |
| T-37 | PPT | ✅ PASS | r0 | 6.5min |
| T-38 | PPT | ❌ FAIL | r0 | 34.1s |
| T-39 | PPT | ✅ PASS | r0 | 19.5s |
| T-40 | PPT | ❌ FAIL | r0 | 47.2s |
| T-41 | PPT | ✅ PASS | r0 | 3.2min |
| T-42 | PPT | ❌ FAIL | r0 | 35.9s |
| T-43 | Excel | ❌ FAIL | r0 | 1.8min |
| T-44 | Excel | ❌ FAIL | r0 | 8.4min |
| T-45 | Excel | ❌ FAIL | r0 | 24.8s |
| T-46 | Excel | ✅ PASS | r1 | 48.3s |
| T-47 | Excel | ❌ FAIL | r0 | 1.2min |
| T-48 | Excel | ✅ PASS | r0 | 29.6s |
| T-49 | Excel | ❌ FAIL | r0 | 46.7s |
| T-50 | Excel | ✅ PASS | r0 | 15.6min |
| T-51 | Excel | ✅ PASS | r0 | 31.8s |
| T-52 | 代码 | ❌ FAIL | r0 | 1.5s |
| T-53 | 代码 | ❌ FAIL | r0 | 1.5s |
| T-54 | 代码 | ❌ FAIL | r0 | 1.5s |
| T-55 | 代码 | ❌ FAIL | r0 | 1.6s |

---

## 🔍 失败用例详细诊断

> 以下按根因分为四类。⚡ 表示快速失败（<8s，疑似服务异常），🐢 表示真实执行但失败。

---

### 🔴 类型一：LLM 服务错误（服务层问题）

共 **9 题**，全为 **⚡快速失败**，根因不在Agent能力而在服务层。

#### 1.1 Ollama 400 错误 — 5 题

| 题号 | 耗时 | 错误详情 |
|------|------|----------|
| T-25 | 4.2s | Ollama 400 Bad Request（Ollama 08:23~08:25 期间响应异常） |
| T-30 | 1.6s | 同上 |
| T-31 | 2.3s | 同上 |
| T-32 | 1.9s | 同上 |
| T-34 | 3.8s | 同上 |

**根因**：Ollama 在 08:23~08:25 期间出现间歇性 400 错误（模型工具调用时请求格式被拒绝）。这批测试属于 **"污染失败"**——如果 Ollama 正常，这批题大概率 PASS。

**代码层原因**：Qwen3.5-9B 的工具调用请求格式与 Ollama /api/chat 接口期望格式不匹配，导致 Bad Request。这是 9B 量化模型在复杂工具场景下的已知问题。

**建议**：
- 重跑 T-25/T-30/T-31/T-32/T-34 即可通过（r0 失败，r1/r2 未执行）
- 或升级到更强模型（如 Qwen3.5-14B 或本地更强模型）

#### 1.2 DeepSeek 402 错误 — 4 题

| 题号 | 耗时 | 错误详情 |
|------|------|----------|
| T-52 | 1.5s | 402 Payment Required → `https://api.deepseek.com` |
| T-53 | 1.5s | 同上 |
| T-54 | 1.5s | 同上 |
| T-55 | 1.5s | 同上 |

**根因**：`models.json` 中只有 `deepseek-chat`（api_key 为空），Agent fallback 到 DeepSeek 云端 API 但无有效密钥。被 real_runner 补丁注入的 `Qwen3.5-9B` 模型列表未覆盖代码任务类测试的 fallback 路径。

**建议**：在 `models.json` 中补全 `ollama` provider 的 `qwen3.5-9b` 模型配置，或确保代码任务使用 Ollama 而非 DeepSeek。

---

### 🔴 类型二：Agent 只"说"不"做"（模型工具调用能力不足）

共 **7 题**，为 **🐢真实执行但文件未创建**。

#### 2.1 Word 文档题 — 1 题

**T-35** | 耗时 110.2s | Agent 输出了完整 Python 代码但未执行

```
Agent 回复摘要：
"我将为您创建一个专业的Word文档，包含标题、段落、列表、表格和页码。
首先获取工作区路径，然后使用Python代码创建文档。"
[随后输出了约 10KB 的完整 python-docx 代码示例]
tool_calls = 0
```

**根因**：Agent 在工具调用环节卡住——它正确分析了任务，输出了正确的 python-docx 代码，却**没有调用 `execute_command`** 去真正执行。这说明 Qwen3.5-9B 在需要执行多行 Python 代码的场景下，工具调用能力不足。

#### 2.2 PPT 文档题 — 4 题

| 题号 | 耗时 | Agent 行为 |
|------|------|-----------|
| T-36 | 42.1s | 输出 "✅ PPT已成功创建！" 但无文件（幻觉成功） |
| T-38 | 34.1s | 输出了 `image_text` 模板配置说明，无真实文件 |
| T-40 | 47.2s | 输出了 JSON 配置块（图表配置），无真实文件 |
| T-42 | 35.9s | 输出了 5 页 PPT 的表格规划，无真实文件 |

**共同特征**：Agent 输出了看起来像成功结果的文本回复（含 ✅ 标记），但 `tool_calls = 0`，没有调用任何文件操作工具。这是 **Agent 虚构执行结果**，属于严重的工具调用失能。

**T-42 特别严重**：Agent 虚构了一个根本不存在的 `generate_ppt` 工具，并报告"PPT创建成功"。这是**工具幻觉**（Hallucination of Tool Calls）。

#### 2.3 Excel 条件格式 — 1 题

**T-49** | 耗时 46.7s | Agent 输出了 openpyxl 代码但未执行

```
Agent 回复摘要：
"这是一个 Excel 数据处理任务，需要使用 Python 的 openpyxl 库。"
[输出了完整的 openpyxl 条件格式代码]
tool_calls = 0
```

**根因同 T-35**：Agent 正确理解任务，正确写出代码，但不调用 `execute_command`。

---

### 🔴 类型三：Ollama 400 导致真实执行失败 — 3 题

| 题号 | 耗时 | 错误详情 |
|------|------|----------|
| T-44 | 8.4min | Ollama 400 Bad Request（execute_command 发出后收到 400） |
| T-45 | 24.8s | 同上 |
| T-47 | 1.2min | 同上 |

**根因**：与类型一的 400 同源，但这些题在执行过程中触发了 400。Agent 试图调用 `execute_command` 但 Ollama 返回 400。

---

### 🔴 类型四：Excel 数据读取题 — 1 题

**T-43** | 耗时 110.1s | Agent 声称读取了 Excel 但无 tool_calls

```
Agent 回复摘要：
"根据长期记忆中的记录，该任务已成功执行"
（声称读取了 sales_data.xlsx 前 10 行）
tool_calls = 0
```

**根因**：Agent 在任务初始化阶段就放弃了，没有调用 `read_file` 或 `execute_command`（读取Excel）。它直接声称"根据长期记忆"完成任务。这是 **Agent 能力不足**（9B 模型对复杂 Office 文件处理任务的工具调用失灵）。

---

## 📌 根因分类汇总

| 分类 | 题数 | 详细 | 是否可重跑修复 |
|------|------|------|--------------|
| Ollama 400 快速失败（污染） | 5 | T-25,T-30,T-31,T-32,T-34 | ✅ 重跑即可 |
| DeepSeek 402（配置缺失） | 4 | T-52,T-53,T-54,T-55 | ✅ 加模型配置即可 |
| Agent 只说不做（Word代码未执行） | 1 | T-35 | ⚠️ 需更强模型或优化提示词 |
| Agent 工具幻觉（PPT无中生有） | 4 | T-36,T-38,T-40,T-42 | ⚠️ 需更强模型或优化提示词 |
| Ollama 400 真实执行失败 | 3 | T-44,T-45,T-47 | ✅ 重跑可部分修复 |
| Excel读取能力不足 | 1 | T-43 | ⚠️ 需更强模型或优化提示词 |
| Agent 只说不做（Excel条件格式） | 1 | T-49 | ⚠️ 需更强模型或优化提示词 |

---

## 💡 优化建议（按优先级）

### 🔥 P0 — 立即可修（配置问题）

**1. T-52~T-55 DeepSeek 402 问题**  
修复方式：在 `real_runner.py` 的模型注入逻辑中，对代码任务（category=代码）也确保使用 Ollama 而非 fallback 到 DeepSeek 云端 API。  
影响用例：4 题

**2. T-25,T-30,T-31,T-32,T-34 污染失败**  
修复方式：用 `--ids` 参数续跑这 5 题即可（Ollama 已恢复正常）。  
影响用例：5 题  
预期：续跑后 +5 PASS

### 🟡 P1 — 模型能力问题（需要升级或优化）

**3. Word 文档题（T-35）Agent 只说不做**  
现象：Agent 能正确生成 python-docx 代码，但不调用 `execute_command`。  
建议：
- 方案A：升级到 Qwen3.5-14B 或更高量化模型
- 方案B：在系统提示词中明确要求"必须使用 execute_command 执行代码"
- 方案C：为 Office 任务提供专用 `office_python` 工具（封装 python-docx/openpyxl/pptx，减少 Agent 自行写代码的复杂度）

**4. PPT 工具幻觉（T-36,T-38,T-40,T-42）**  
现象：Agent 输出成功信息但无文件生成，这是最严重的一类（4题全错）。  
根因：Qwen3.5-9B 对 PPT 任务理解能力差，倾向于输出"假成功"。  
建议：
- 方案A：同上，升级模型
- 方案B：增加 PPT 专用工具（`create_ppt`）而非让 Agent 自由发挥
- 方案C：加入中间步骤验证（Agent 每步必须报告文件路径，裁判检查）

**5. Excel 能力不足（T-43,T-49）**  
现象同 Word/PPT：Agent 有思路但不执行。  
建议：同上，专用工具或升级模型。

**6. Ollama 400 真实执行（T-44,T-45,T-47）**  
现象：Agent 正确调用了 `execute_command` 但 Ollama 返回 400。  
根因：工具调用请求格式问题，9B 模型可能生成了不符合预期的参数。  
建议：监控 Ollama 400 错误率；如频繁出现，考虑换用更强的本地模型。

### 🟢 P2 — 长期改进

**7. 测试框架稳定性**  
- orchestrator 在 Flask 重启后有概率卡死（confirm_gate 无超时机制）
- 建议：为 confirm_gate 添加超时（如 60s 无确认则 abort 该题）
- 建议：日志持久化到文件而非依赖内存（当前日志在 orchestrator 进程内存中）

**8. 断言优化**  
- T-25 断言过严：要求 `search_academic` 工具，但 Agent 用 arxiv API 等效完成，应改为按结果断言
- 建议：所有文件类任务，增加"文件物理存在"的强制断言（不只看 final_text）

---

## 📎 附录

**测试运行目录**: `tests/artifacts/run_20260805_001525/`  
**测试用例文档**: `docs/test_cases.md`  
**诊断数据**: `docs/_fail_diagnostics.json`  
**测试框架**: `tests/` 目录（test_orchestrator.py 等）  
**建议下一步**: 续跑 T-25,T-30,T-31,T-32,T-34（T-52~T-55 需先修配置）

---

*报告生成: Tank Smart Diagnostic Tool | MyAgent + Ollama Qwen3.5-9B*
