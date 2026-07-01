---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: ef5b15bc948ed1a80cab91ca175eedba_4f5cd81472c811f1b2f55254006c9bbf
    ReservedCode1: xd7j+NO0eaJNNGFP3ZiPQ9CJq97MIKgcxaDBtPtxbbp+Cy4cRdL7472peRuR5wQ9uwGRoWaNH+aCY+mNVsn87xWVIg/n0I8kaoEIj6dvg6VqVCFvWZGxJvdeWbmA6ZwS/Z6QOsppWUw3OesJsURy4MY/p5cnXYXWX0Jvf90dLi/hQqO4TQBdCRV8sMQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: ef5b15bc948ed1a80cab91ca175eedba_4f5cd81472c811f1b2f55254006c9bbf
    ReservedCode2: xd7j+NO0eaJNNGFP3ZiPQ9CJq97MIKgcxaDBtPtxbbp+Cy4cRdL7472peRuR5wQ9uwGRoWaNH+aCY+mNVsn87xWVIg/n0I8kaoEIj6dvg6VqVCFvWZGxJvdeWbmA6ZwS/Z6QOsppWUw3OesJsURy4MY/p5cnXYXWX0Jvf90dLi/hQqO4TQBdCRV8sMQ=
---

# MyAgent

Windows 本地智能助手，采用 **ReAct (Reasoning + Acting)** 循环架构，通过 LLM 驱动工具调用来完成文件操作、代码生成、数据处理、系统配置、网页搜索等任务。

## 架构概览

```
用户输入 (Web SPA)
    │  POST /api/chat  (Flask + SSE streaming)
    ▼
┌─────────────────────────────────────┐
│         backend/agent.py            │
│  ┌─ Phase 1: 域/角色匹配 ──────────┤
│  ├─ Phase 2: 构建 System Prompt ───┤
│  │   core.md + tools.yaml +        │
│  │   skills/*.md + 长期记忆注入     │
│  └─ Phase 3: ReAct 循环 (≤15轮) ──┤
│      思考 → 工具调用 → 结果注入 → 循环  │
└─────────────────────────────────────┘
    │  SSE: thinking / step / confirm / final
    ▼
前端实时渲染 + 确认弹窗
```

## 目录结构

```
├── app.py                  Flask 主入口，API 路由 + SSE
├── backend/
│   ├── agent.py            ReAct 循环引擎
│   └── upload_api.py       文件上传 API
├── llm_client.py           LLM 调用抽象层（Ollama / OpenAI 兼容）
├── tools/
│   ├── __init__.py         工具注册 + Prompt 构建 + 域匹配
│   ├── file_tools.py       文件操作 + execute_command
│   ├── code_tools.py       execute_command / run_python
│   ├── search_tools.py     文件/内容搜索
│   ├── registry_tools.py   注册表操作
│   ├── web_download.py     文件下载
│   └── safe_exec.py          安全执行四层防护
├── memory/                 记忆系统 v4
│   ├── session.py          会话生命周期
│   ├── long_term.py        长期记忆（Markdown 追加 + 关键词检索）
│   ├── extractor.py        异步事实提取
│   ├── compressor.py       Token 预算制异步压缩
│   └── stores/
│       ├── json_store.py   会话元数据
│       └── sqlite_store.py 消息 + 摘要 + 日志
├── prompts/
│   ├── core.md            角色定义 + 行为准则
│   ├── output.md           输出格式规范
│   ├── tools.yaml          工具声明式配置（唯一真相源）
│   ├── skills/             7 个域技能（code/system/excel/web/db/memory/general）
│   └── modes/              7 种角色模式（architect/review/pm/debugger/growth/boss/plan）
├── web/
│   └── index.html          SPA 入口（原生 HTML+CSS+JS）
└── utils/
    └── error_mapper.py     错误脱敏/翻译
```

## ReAct 循环控制

| 机制 | 说明 |
|------|------|
| 最大轮次 | 15 轮，倒数第 2 轮注入收尾提示 |
| 回路护板 | 连续同工具 ≥5 次拦截，≥8 次强制终止；完全相同参数 ≥2 次拦截 |
| 工具缓存 | 只读工具结果缓存复用，写入工具执行后清空 |
| 结果截断 | 大文件 4000 字符，其他 2000 字符 |
| 确认拦截 | `execute_command` 高风险命令通过 SSE `confirm` 事件 + threading.Event 挂起等待用户确认 |
| JSON 容错 | `json_repair` + 多行 JSON 检测 + 括号配对提取兜底 |

## 工具体系

**常驻注入**（所有场景可用）：`execute_command`, `run_python`, `write_file`, `read_file`, `scan_directory`, `web_download`, `search_files`, `modify_registry`, `get_project_summary`

**域专属**（按关键词匹配加载）：`search_content`, `replace_in_file`, `web_search`, `fetch_webpage`, `fetch_multiple`, `get_weather`, `append_to_log`, `remember_user_info`

### 域匹配（7 领域）

| 域 | 触发示例 | 核心工具 |
|----|---------|---------|
| code_generate | 代码/编程/脚本/实现 | search_content, replace_in_file |
| web_browse | 搜索/天气/新闻/网页 | web_search, fetch_webpage |
| system_config | 注册表/安装软件/桌面图标 | modify_registry, web_search |
| data_excel | Excel/表格/数据分析 | run_python, read_file |
| user_memory | 记住/我叫/个人信息 | 长期记忆读写 |
| db_query | 数据库/SQL/表结构 | run_python |
| general_chat | 兜底 | 纯对话 |

## LLM 支持

| Provider | 默认模型 | 后端 |
|----------|---------|------|
| Ollama | `deepseek-r1:7b` | `http://localhost:11434` |
| DeepSeek | `deepseek-chat` | `api.deepseek.com` |
| 混元 | `hunyuan-turbos-latest` | `api.hunyuan.cloud.tencent.com` |
| 通义千问 | `qwen-turbo` | `dashscope.aliyuncs.com` |
| 智谱GLM | `glm-4-flash` | `open.bigmodel.cn` |
| 豆包 | `doubao-lite-128k` | `ark.cn-beijing.volces.com` |
| OpenAI | `gpt-4o` | `api.openai.com` |

- 前端 `/api/models` 热切换模型
- API Key 通过 Fernet 加密存储（`config.enc` + `.myagent_key`）
- 环境变量 `API_KEY` / `API_BASE_URL` / `API_MODEL` 可覆盖配置

## 记忆系统

| 组件 | 说明 |
|------|------|
| SQLite messages | 会话消息持久化（per-session） |
| SQLite summaries | Token 预算制异步压缩摘要 |
| 长期记忆 | Markdown 文件按类别存储，关键词检索注入 |
| 异步事实提取 | 从对话中自动提取用户个人信息 |

## API 路由

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | SPA 入口 |
| `/api/chat` | POST | SSE 流式对话 |
| `/api/sessions` | GET/POST | 会话列表 / 新建 |
| `/api/sessions/<id>` | GET/DELETE | 获取/删除会话 |
| `/api/sessions/<id>/rename` | POST | 重命名 |
| `/api/sessions/active` | GET | 活跃会话 |
| `/api/models` | GET | 模型列表 |
| `/api/confirm` | POST | 命令确认回调 |
| `/api/config/models` | GET/POST | 模型配置 |
| `/api/config/user_profile` | GET/POST | 用户信息 |
| `/api/logs/query` | GET | 日志查询 |
| `/api/logs/llm_call` | GET | LLM 调用详情 |
| `/logs/view` | GET | 日志查看器 |

## 快速开始

```bash
pip install flask flask-cors flask-limiter json-repair openpyxl pandas cryptography

python app.py
```

浏览器打开 `http://localhost:8765`。

首次启动会进入配置向导（`setup_config.py`），引导设置 API Key 和默认模型。

## SSE 事件类型

| 事件 | 说明 |
|------|------|
| `thinking` | LLM 思维链实时输出 |
| `step` | 工具执行状态（等待中/完成/错误） |
| `confirm` | 命令确认弹窗 |
| `final` | 最终回复 |
| `error` | 错误信息 |
*（内容由AI生成，仅供参考）*
