# MyAgent 测试方案

> 版本：v1.0 | 日期：2026-06-14

---

## 一、测试体系总览

```
MyAgent 测试金字塔
┌─────────────────────────┐
│   E2E 集成测试 (3个)     │  ← Flask API + 完整 ReAct 循环
├─────────────────────────┤
│   模块集成测试 (5个)     │  ← 跨模块调用链路
├─────────────────────────┤
│   单元测试 (80+个)       │  ← 每个函数独立验证
└─────────────────────────┘
```

**测试框架**：pytest
**运行命令**：`pytest tests/ -v --tb=short`
**覆盖率目标**：核心模块 ≥ 80%

---

## 二、测试目录结构

```
tests/
├── __init__.py
├── conftest.py              # 共享 fixtures（临时目录、样本数据）
├── test_llm_client.py       # LLM 客户端：加解密、JSON解析、路径规范化
├── test_intent_router.py    # 意图路由：分类、参数提取、注册表
├── test_memory.py           # 记忆系统：长期/压缩/会话/聊天记录
├── test_tools.py            # 工具函数：14个工具 + 注册表 + Prompt组装
├── test_app.py              # Flask API：路由、会话管理、模型列表
└── test_react_loop.py       # ReAct循环：完整推理链路、格式纠错、轮次上限
```

---

## 三、各模块测试用例明细

### 3.1 llm_client.py — 14 个用例

| # | 用例名 | 测试内容 | 输入 | 预期输出 |
|---|--------|----------|------|----------|
| 1 | `test_encrypt_decrypt_roundtrip` | 加密→解密还原 | `{"backend":"ollama"}` | 原文一致 |
| 2 | `test_encrypt_unicode` | 中文+Emoji加密 | `"中文测试 🎉"` | 解密后一致 |
| 3 | `test_decrypt_wrong_key_fails` | 错误密钥解密 | wrong_key | 抛异常 |
| 4 | `test_encrypt_empty_string` | 空字符串加密 | `""` | 解密得 `""` |
| 5 | `test_parse_valid_json` | 标准JSON解析 | `{"tool_name":"read_file",...}` | 正确解析dict |
| 6 | `test_parse_json_with_think_tags` | 含think标签的JSON | `<think>...</think>{"final_message":"..."}` | 提取final_message |
| 7 | `test_parse_json_in_code_block` | 代码块中的JSON | ````json\n{...}\n```` | 正确解析 |
| 8 | `test_parse_invalid_json` | 纯文本非JSON | `"这不是JSON"` | 返回None |
| 9 | `test_parse_final_message` | final_message类型 | `{"final_message":"完成"}` | 提取成功 |
| 10 | `test_parse_progress_message` | progress类型 | `{"progress":"处理中"}` | 提取成功 |
| 11 | `test_parse_empty_content` | 空内容 | `""` | 返回None |
| 12 | `test_chinese_drive_letter` | 中文盘符 | `"D盘\\test"` | `"D:\\test"` |
| 13 | `test_forward_slash_to_backslash` | 斜杠转换 | `"D:/test/file.py"` | `"D:\\test\\file.py"` |
| 14 | `test_add_backslash_after_drive` | 补盘符斜杠 | `"D:test"` | `"D:\\test"` |

### 3.2 intent_router.py — 18 个用例

| # | 用例名 | 测试内容 | 输入 | 预期输出 |
|---|--------|----------|------|----------|
| 1 | `test_has_required_intents` | 注册表完整性 | — | 4个核心意图存在 |
| 2 | `test_persona_modes_subset` | Persona子集检查 | — | 6个模式在注册表中 |
| 3 | `test_total_intent_count` | 意图总数 | — | ≥10 |
| 4 | `test_classify_file_manage` | 文件操作分类 | `"移动文件"` | `"file_manage"` |
| 5 | `test_classify_code_generate` | 代码生成分类 | `"写冒泡排序"` | `"code_generate"` |
| 6 | `test_classify_general_chat` | 闲聊分类 | `"你好"` | `"general_chat"` |
| 7 | `test_classify_db_query` | 数据库分类 | `"查询用户表"` | `"db_query"` |
| 8 | `test_classify_architect` | 架构师分类 | `"设计微服务"` | `"architect"` |
| 9 | `test_classify_returns_lowercase` | 大小写统一 | `"Code_Generate"` | `"code_generate"` |
| 10 | `test_unknown_fallback` | 未知意图降级 | `"xyz"` | `"general_chat"` |
| 11 | `test_llm_none_fallback` | LLM返回None | None | `"general_chat"` |
| 12 | `test_classify_multiple_intents` | 多意图取首个 | `"file_manage and code_generate"` | `"file_manage"` |
| 13 | `test_extract_file_manage_params` | 文件参数提取 | 源+目标路径 | `{source, destination}` |
| 14 | `test_extract_code_params` | 代码参数提取 | 语言+任务 | `{language, task}` |
| 15 | `test_extract_general_returns_empty` | 闲聊无参数 | `"你好"` | `{}` |
| 16 | `test_extract_bad_json` | 非法JSON容错 | `"不是JSON"` | `{}` |
| 17 | `test_extract_with_think_tags` | think标签处理 | `<think>...</think>{...}` | 正确提取 |
| 18 | `test_skill_prompts_complete` | 技能提示完整性 | — | 所有意图有提示 |

### 3.3 memory.py — 25 个用例

| # | 用例名 | 测试内容 | 预期 |
|---|--------|----------|------|
| 1 | `test_load_default` | 无文件时返回默认结构 | 含空user_info |
| 2 | `test_save_load_roundtrip` | 保存→读取还原 | 数据一致 |
| 3 | `test_load_merges_defaults` | 部分数据合并默认值 | 缺失字段有默认值 |
| 4 | `test_save_preserves_chinese` | 中文持久化 | 内容不变 |
| 5 | `test_add_compressed` | 添加压缩摘要 | 可检索到 |
| 6 | `test_compressed_filters_old` | 过期摘要过滤 | 返回空 |
| 7 | `test_compressed_limit_50` | 超限截断 | ≤50条 |
| 8 | `test_empty_compressed` | 空摘要返回空字符串 | `""` |
| 9 | `test_compressed_format` | 格式化输出 | 含`【历史对话摘要】` |
| 10 | `test_create_session` | 创建会话 | 返回非空ID |
| 11 | `test_unique_session_ids` | ID唯一性 | 两个ID不同 |
| 12 | `test_list_sessions` | 列出会话 | ≥2个 |
| 13 | `test_delete_session` | 删除会话 | 列表中消失 |
| 14 | `test_active_session_auto_create` | 自动创建活跃会话 | 返回ID |
| 15 | `test_set_get_active` | 设置/获取活跃会话 | 一致 |
| 16 | `test_session_messages_empty` | 新会话无消息 | `[]` |
| 17 | `test_add_get_chat_messages` | 聊天记录CRUD | 2条记录 |
| 18 | `test_count_messages` | 消息计数 | 3 |
| 19 | `test_delete_chat_session` | 删除聊天记录 | 0条 |
| 20 | `test_ignore_system_role` | 过滤非user/assistant | 不存储 |
| 21 | `test_log_operation` | 操作日志写入 | 不抛异常 |
| 22 | `test_log_sanitize` | 换行符清洗 | 内容正常 |
| 23 | `test_log_trace` | Trace写入 | 不抛异常 |
| 24 | `test_user_id_persists` | 用户ID持久化 | 两次相同 |
| 25 | `test_trace_id_format` | Trace ID格式 | 12位字母数字 |

### 3.4 tools/ — 30 个用例

| # | 用例名 | 测试内容 | 预期 |
|---|--------|----------|------|
| 1 | `test_scan_existing_dir` | 扫描有内容目录 | 返回文件列表 |
| 2 | `test_scan_nonexistent` | 扫描不存在目录 | 返回错误信息 |
| 3 | `test_scan_empty_dir` | 扫描空目录 | `[]` |
| 4 | `test_move_success` | 移动文件成功 | 源消失，目标存在 |
| 5 | `test_move_nonexistent` | 移动不存在文件 | success=False |
| 6 | `test_move_no_overwrite` | 禁止覆盖 | success=False |
| 7 | `test_copy_success` | 复制文件成功 | 源+目标都存在 |
| 8 | `test_copy_nonexistent` | 复制不存在文件 | success=False |
| 9 | `test_resolve_folders` | 解析子文件夹 | 返回文件夹列表 |
| 10 | `test_resolve_nonexistent` | 解析不存在目录 | success=False |
| 11 | `test_execute_echo` | 执行echo命令 | success=True |
| 12 | `test_execute_forbidden` | 禁止危险命令 | 安全限制 |
| 13 | `test_execute_unknown` | 禁止未知程序 | 不允许 |
| 14 | `test_execute_empty` | 空命令 | success=False |
| 15 | `test_write_file_success` | 写入文件 | 文件存在且内容正确 |
| 16 | `test_write_outside_blocked` | 路径越界拦截 | 非法路径 |
| 17 | `test_write_no_overwrite` | 禁止覆盖 | success=False |
| 18 | `test_read_full` | 全量读取 | total_lines正确 |
| 19 | `test_read_range` | 范围读取 | 仅含指定行 |
| 20 | `test_read_nonexistent` | 读取不存在文件 | success=False |
| 21 | `test_read_large_truncate` | 大文件截断 | truncated=True |
| 22 | `test_search_finds` | 关键词搜索 | 找到匹配行 |
| 23 | `test_search_no_results` | 无匹配搜索 | `[]` |
| 24 | `test_replace_success` | 替换成功 | 内容已改 |
| 25 | `test_replace_not_found` | 替换目标不存在 | success=False |
| 26 | `test_replace_occurrence_exceed` | 替换序号超限 | success=False |
| 27 | `test_project_summary` | 项目结构分析 | 返回文件列表 |
| 28 | `test_search_files` | 文件名搜索 | 找到匹配文件 |
| 29 | `test_remember_user_info` | 记住用户信息 | 长期记忆更新 |
| 30 | `test_registry_has_tools` | 注册表完整性 | 10+工具已注册 |

### 3.5 app.py Flask API — 10 个用例

| # | 用例名 | 测试内容 | 预期 |
|---|--------|----------|------|
| 1 | `test_index_html` | 首页返回HTML | 200 + 含AI Agent |
| 2 | `test_get_sessions` | 获取会话列表 | 200 + list |
| 3 | `test_post_session` | 创建会话 | 200 + success=True |
| 4 | `test_delete_session` | 删除会话 | 200 |
| 5 | `test_get_active_session` | 获取活跃会话 | 含session_id |
| 6 | `test_get_session_messages` | 获取会话消息 | success + messages |
| 7 | `test_get_models` | 获取模型列表 | ≥1个模型 |
| 8 | `test_chat_missing_session` | 缺少session_id | 400 |
| 9 | `test_chat_empty_message` | 空消息 | 200或400 |
| 10 | `test_models_structure` | 模型字段完整 | 含display/provider/model |

### 3.6 ReAct 循环 — 8 个用例

| # | 用例名 | 测试内容 | 预期 |
|---|--------|----------|------|
| 1 | `test_single_tool_then_final` | 单工具→最终回复 | 2轮完成 |
| 2 | `test_multi_tool_calls` | 多工具连续调用 | 调用get_workspace+write_file |
| 3 | `test_format_correction_retry` | 格式错误重试 | 重试后成功 |
| 4 | `test_max_turns_limit` | 达到轮次上限 | 15轮停止 |
| 5 | `test_progress_continues_loop` | progress不终止循环 | 2次progress后完成 |
| 6 | `test_finished_signal` | finished信号 | 循环终止 |
| 7 | `test_llm_none_response` | LLM请求失败 | 降级为general_chat |
| 8 | `test_prompt_assembly_all_intents` | 所有意图有prompt | 每个>50字符 |

---

## 四、运行方式

### 4.1 安装依赖

```bash
pip install pytest json_repair
```

### 4.2 运行全部测试

```bash
cd D:\PyCharm Project\MyAgent
pytest tests/ -v --tb=short
```

### 4.3 运行指定模块

```bash
pytest tests/test_llm_client.py -v      # LLM客户端
pytest tests/test_intent_router.py -v   # 意图路由
pytest tests/test_memory.py -v          # 记忆系统
pytest tests/test_tools.py -v           # 工具函数
pytest tests/test_app.py -v             # Flask API
pytest tests/test_react_loop.py -v      # ReAct循环
```

### 4.4 生成覆盖率报告

```bash
pip install pytest-cov
pytest tests/ --cov=llm_client --cov=memory --cov=tools --cov=intent_router --cov=app --cov-report=term-missing
```

---

## 五、测试题目（面试/自测）

### 题目1：意图分类准确率
> 给定 20 条用户输入，手动判断意图分类是否正确。
>
> | 输入 | 期望意图 |
> |------|----------|
> | "把D盘的报告移动到备份文件夹" | file_manage |
> | "帮我写一个快速排序算法" | code_generate |
> | "查询MySQL用户表的所有记录" | db_query |
> | "设计一个分布式缓存方案" | architect |
> | "审查这段代码有没有安全漏洞" | code_review |
> | "今天天气怎么样" | general_chat |
> | "帮我规划一下产品上线流程" | product_manager |
> | "排查这个NullPointerException" | debugger |
> | "分析竞品的用户增长策略" | growth |
> | "评估这个项目的投入产出比" | boss |

### 题目2：工具调用链路
> 用户说："帮我把 D:\docs 下所有 .txt 文件备份到 E:\backup"
>
> 写出 Agent 应该执行的完整工具调用链（至少3步），并说明每步的参数。

### 题目3：记忆压缩触发条件
> 短期记忆存储在 JSON 文件中，`max_turns=8`。
>
> Q1: 当用户第 9 次发送消息时，系统会做什么？
> Q2: 压缩后的摘要保存在哪个文件？
> Q3: 下次对话时，压缩摘要如何注入到 LLM 上下文？

### 题目4：ReAct 循环的容错设计
> Q1: 如果 LLM 返回了不是 JSON 的纯文本，系统如何处理？
> Q2: 如果 LLM 调用了不存在的工具名，会发生什么？
> Q3: 如果连续 15 轮都没有返回 final_message，系统如何收尾？

### 题目5：安全机制
> Q1: 用户让 Agent 执行 `rm -rf /`，系统的防护链路是什么？
> Q2: 用户让 Agent 读取 `C:\Users\.ssh\id_rsa`，会发生什么？
> Q3: `write_file` 工具为什么限制只能在 `work_space` 内写入？

### 题目6：Prompt 工程
> Q1: 五层 System Prompt 的组装顺序是什么？每层的作用？
> Q2: 当意图是 `architect` 时，Persona 模式如何替换 core.md？
> Q3: `safety.md` 为什么在所有意图下都加载？

---

## 六、通过标准

| 指标 | 目标 | 实际 |
|------|------|------|
| 单元测试通过率 | 100% | **128/128 (100%)** |
| 全部测试数量 | ≥ 90 个 | **128 个** |
| ReAct 循环测试 | 8/8 通过 | **11/11 通过** |
| Flask API 测试 | 10/10 通过 | **9/9 通过** |
| 意图路由测试 | 12/12 通过 | **22/22 通过** |
| LLM 客户端测试 | 14/14 通过 | **18/18 通过** |
| 记忆系统测试 | 20/20 通过 | **24/24 通过** |
| 工具函数测试 | 25/25 通过 | **34/34 通过** |
