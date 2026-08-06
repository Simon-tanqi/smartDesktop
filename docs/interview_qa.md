# MyAgent 项目 Top 30 面试问题与答案

> 所有答案均基于项目真实源码，引用具体文件路径和函数名。标注"基于设计意图推断"的部分为合理补充。

---

## 目录

1. [架构设计与整体流程](#1-架构设计与整体流程)（5题）
2. [记忆与上下文管理](#2-记忆与上下文管理)（4题）
3. [工具系统与执行安全](#3-工具系统与执行安全)（4题）
4. [日志与可观测性](#4-日志与可观测性)（4题）
5. [搜索与外部知识](#5-搜索与外部知识)（4题）
6. [文件生成与数据处理](#6-文件生成与数据处理)（3题）
7. [异常处理与鲁棒性](#7-异常处理与鲁棒性)（4题）
8. [性能与扩展性](#8-性能与扩展性)（3题）

---

## 1. 架构设计与整体流程

### Q1. 为什么选择 ReAct 模式而非其他 Agent 范式？你的主循环具体如何编排？

**参考答案**：

- **设计思路**：ReAct（Reasoning + Acting）模式将推理和行动交替进行，天然适合需要多步工具调用的场景。相比于仅推理（Chain-of-Thought）或仅行动（Tool-Use only），ReAct 在每一步都有结构化的思考轨迹，便于调试和复盘。我选择 ReAct 而非 Plan-and-Execute 的原因是：我的场景中任务经常动态变化（如用户中途追加需求），ReAct 的逐步决策更灵活。

- **代码实现**：主循环位于 `core/react_engine.py` 的 `ReActEngine.run()` 方法（第 141 行）。核心流程：

```python
# core/react_engine.py:141-391
class ReActEngine:
    MAX_TURNS = 15  # 硬上限防止无限循环

    def run(self, user_message, model_choice, ...):
        # 阶段1：准备运行状态（记忆加载、域匹配、BM25上下文路由）
        result = yield from prepare_run_state(self, rs, user_message, ...)

        # 阶段2：主循环
        for step in range(start_step, self.MAX_TURNS):
            # 2a. 调用 LLM 并解析响应
            _flow, visible_content, parsed_action, _llm_native_tool_calls = \
                yield from call_llm_and_parse(...)

            # 2b. 无工具调用 → 提取 final_message 直接返回
            if not _llm_native_tool_calls and not parsed_action.get("tool_name"):
                yield from safe_final(self, state, final_text)
                return

            # 2c. 逐个执行 tool_call（支持多 tool_calls 协议）
            for tc in pending_tool_calls:
                _flow = yield from execute_one_step(...)

        # 阶段3：达到最大步数，生成进度摘要收尾
        yield from do_final_cleanup(...)
```

- **挑战与解决**：最初 `run()` 方法超过 500 行，维护困难。我进行了大规模重构——将阶段方法（`prepare_run_state`、`execute_one_step`、`handle_tool_success`、`handle_tool_failure`）提取到独立文件 `core/agent_state.py` 和 `core/tool_data_provider.py` 中，通过 `RunState` 数据类传递可变状态，主循环文件缩减到 392 行。

- **扩展思考**：如果重新设计，我会引入 TaskPlan 抽象层——先用 LLM 生成高层计划，然后 ReAct 在计划约束下逐步执行。这样可以在 WARNING_TURN=12 时自动对比"已完成 vs 计划"，做出更智能的"继续/放弃"决策。

---

### Q2. 你的系统如何处理 LLM 调用的 JSON 解析失败？如果 LLM 返回的不是合法 JSON 怎么办？

**参考答案**：

- **设计思路**：LLM 输出不可靠是 Agent 开发的核心挑战之一。我采用"多层容错解析 + 降级策略"而非严格的格式校验，因为过度约束会降低 LLM 的灵活性。

- **代码实现**：

1. **JSON 修复库**：`core/streaming.py:_safe_json_loads()`（第 328 行）使用 `json_repair` 库作为首选解析器，它可修复截断/多余的逗号、缺失引号等常见问题：

```python
def _safe_json_loads(data: str) -> Any:
    # 优先使用 json_repair（可修复不完整 JSON）
    try:
        from json_repair import loads as repair_loads
        return repair_loads(data)
    except ImportError:
        pass
    # 降级到标准 json.loads
    # 再降级到正则提取 {...} 后再解析
    # 完全失败返回 {"success": False, "raw_data": data}
```

2. **推理轨迹解析**：`core/streaming.py:parse_reasoning_trace()`（第 392 行）支持多种格式——```reasoning 代码块、内联 JSON 对象（brace counting 解析）、纯文本 fallback：

```python
# 1. 提取 ```reasoning 代码块
# 2. 查找内联 JSON {"reasoning_trace": {...}}
# 3. 使用 brace-counting 正确匹配嵌套花括号
# 4. 都失败时，截取纯文本前 200 字符作为 current_intent
```

3. **解析失败计数**：`AgentTurnState.parse_fail`（第 301 行）跟踪连续解析失败次数，防止陷入"始终返回非法格式"的死循环。

- **挑战与解决**：早期版本中，DeepSeek 模型偶尔在 JSON 中间插入自然语言解释（如"好的，我使用以下工具：{...}"），导致解析失败。我在 `strip_code_blocks()` 中增加了 brace-counting 算法来精准定位 JSON 边界——通过逐字符扫描跟踪 `{` `}` 深度和在字符串内的状态。

- **扩展思考**：更好的方案是使用 constrained decoding（如 Outlines 库或 JSON Schema guided generation），从模型层面保证输出合法。但当前依赖的 DeepSeek API 不支持此特性，所以容错解析是务实之选。

---

### Q3. 你的 Agent 状态管理是如何做的？如何保证多线程安全？

**参考答案**：

- **设计思路**：Agent 运行期间的状态管理是并发安全的基石。我设计了有限状态机（FSM）+ deepcopy 隔离 + 线程锁的组合方案。

- **代码实现**：

1. **有限状态机**：`core/agent_state.py:AgentState`（第 368 行），定义了 7 种状态和合法转移表：

```python
_VALID_TRANSITIONS = {
    "IDLE": {"RUNNING", "CANCELLED"},
    "RUNNING": {"PAUSED", "CANCELLED", "TIMEOUT", "COMPLETED", "WAITING_CONFIRMATION"},
    "PAUSED": {"RUNNING", "CANCELLED"},
    "WAITING_CONFIRMATION": {"RUNNING", "CANCELLED", "TIMEOUT"},
    "TIMEOUT": {"IDLE", "RUNNING", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": {"IDLE"},
}
```

2. **Deepcopy 隔离**：`get_state()` 和 `get_request()` 返回 `copy.deepcopy()` 的副本而非内部引用（第 376-386 行），防止外部代码意外修改内部状态。这是一个常见但容易被忽视的 bug 来源。

3. **线程锁**：每个 `AgentState` 实例持有一个 `threading.Lock`，所有读写操作都通过 `with self._lock` 保护。

4. **超时强制**：`_enforce_timeouts()`（第 416 行）在每次状态变更前自动检查——如果状态创建超过 3600 秒（`_STATE_TTL_SECONDS`），自动转到 TIMEOUT。

- **挑战与解决**：最初设计没有状态转移表（`_VALID_TRANSITIONS`），导致出现"COMPLETED → RUNNING"这类非法转换。修复方案是引入严格的 FSM 验证——`transition_to()` 方法在每次状态变更时检查 `valid_targets`，不合法的转移直接抛 `ValueError`。

- **扩展思考**：单进程 FSM 适合当前场景。若扩展到多实例部署，需要用 Redis + 分布式锁替代内存锁，状态转移表逻辑可复用。

---

### Q4. 你是如何做话题切换检测的？为什么不直接用 LLM 来判断？

**参考答案**：

- **设计思路**：这是我最自豪的模块之一。我自研了一个**零外部依赖的 BM25 上下文路由**——核心原因是：① 如果每次都用 LLM 判断"这是新任务吗"，不仅增加延迟（1-3秒），还消耗 token（每次约 200-500 token）；② LLM 的判断有随机性，同样的话两次判断结果可能不同；③ BM25 算法是 Elasticsearch 和 Lucene 的核心检索算法，数学上稳定可靠。

- **代码实现**：`memory/context_router.py`，两个核心类：

1. **BM25 类**（第 80 行）：纯 Python 实现 Okapi BM25（k1=1.5, b=0.75），包含 `fit()`（构建倒排索引）、`score_all()`（计算查询与每篇历史文档的 BM25 分数）、`self_score()`（计算查询与自身的理论最高分）、`top_distinctive_terms()`（提取区分性关键词）。

2. **ContextSwitchDetector 类**（第 189 行）：

```python
def detect(self, user_message: str) -> Dict[str, Any]:
    # 情形1：首次使用 → 不切换
    # 情形2：≤8字短指令 / 延续指令（"继续""然后""好的"）→ 不切换
    # 情形3：正常 BM25 计算
    norm_sim = max_raw / max(self_score, 0.001)  # 归一化到 [0,1]
    is_switch = norm_sim < 0.25  # 阈值判断
```

3. **中文分词**：`tokenize()`（第 36 行）使用双字 bigram + 单字 fallback，对中文做了专门优化。例如"卸载Qoder"的 bigram 是 `[卸载, qoder]`，导致跟"卸载程序"的 bigram `[卸载, 程序]` 有交集（"卸载"），不会误判为切换。

- **挑战与解决**：核心难点是中文分词。我一直避免引入 jieba 等外部依赖（打包分发时会严重增大体积）。最终的双字 bigram 方案在实践中效果非常好——因为中文的语义基本单元就是双字词组，bigram 恰好覆盖了这个粒度。附加的单字 fallback 提供了 recall 兜底。

- **扩展思考**：BM25 的局限是无法处理语义相近但用词不同的情况（如"买电脑" vs "选购笔记本"）。如果未来项目需要更高准确率，可以引入轻量 embedding 模型（如 bge-small-zh，仅 24MB）计算余弦相似度作为补充特征，和 BM25 的分数做加权融合。

---

### Q5. 你的 Agent 框架和 LangChain 的核心区别是什么？为什么不用 LangChain？

**参考答案**：

- **设计决策**：我在项目初期评估过 LangChain，但最终选择自研。核心原因：

1. **抽象层级过深**：LangChain 的 `Chain → LLMChain → ConversationChain → AgentExecutor` 继承链导致调试时需要在 5-6 层抽象间跳转。我的 `ReActEngine.run()` 方法只有 250 行核心逻辑，一眼能看到完整的数据流。

2. **工具系统灵活性**：LangChain 的 `@tool` 装饰器和 `StructuredTool` 类在复杂参数场景下不够灵活。我的 `ToolDataProvider`（`core/tool_data_provider.py`）支持 YAML 配置驱动 + 运行时注册双路径，参数 schema 自动从函数签名推断，且内部参数自动从 LLM 可见 schema 中过滤。

3. **记忆系统控制力**：LangChain 的记忆模块是黑盒的，很难精细控制什么信息进入上下文。我的分层记忆系统（工作记忆 → 情节缓存 → 压缩摘要 → 长期记忆）每一步都可以精确控制 Token 预算。

4. **安全需求**：我的场景需要执行系统命令（`execute_command`），LangChain 没有内置安全沙箱。我自研了 L0-L3 四层防护的 `safe_exec.py`。

- **从 LangChain 借鉴了什么**：ReAct 论文的思想、工具调用链的 `agent → tool → agent` 循环模式、以及 conversation memory 的分层理念。

- **扩展思考**：现在我正在考虑实现 MCP（Model Context Protocol）兼容层，让我的 Agent 可以调用 Claude Code/Cursor 的 MCP 工具，实现生态互通。

---

## 2. 记忆与上下文管理

### Q6. 你的分层记忆系统具体如何设计？每一层存储什么？

**参考答案**：

- **设计思路**：记忆管理的核心矛盾是"有限的上下文窗口 vs 无限的对话历史"。我的方案是四层金字塔——越活跃的信息越完整，越久远的信息越精简。

- **代码实现**：顶层入口 `memory/session.py:get_layered_memory()`（第 1059 行）：

| 层级 | 存储位置 | 内容 | 容量/生命周期 | 代码位置 |
|:---|:---|:---|:---|:---|
| **工作记忆** | JSON（sessions_meta） | 状态变量（key-value）、当前计划、最近观察 | 单次任务 | `_get_working_memory()` (L43) |
| **情节缓存** | SQLite（per-session messages.db） | 最近 3 轮完整结构化交互（含工具调用链） | 近 3 轮 | `get_layered_memory()` L1093-1111 |
| **历史摘要** | SQLite（per-session summaries.db） | 超 3 轮后的 LLM 压缩自然语言摘要 | 800 token 预算 | `get_compressed_summary()` L1176 |
| **长期记忆** | Markdown 文件（user.md/projects.md/skills.md/facts.md） | 关键词检索的用户信息、偏好、技能 | 跨会话 | `long_term.py:get_relevant_memory()` |

- **情节缓存的裁剪策略**（`memory/session.py:_build_structured_history()` L599）：
  - 近 2 个子轮次：完整保留 `reasoning_trace` JSON
  - 更早子轮次：剥离思考轨迹，仅保留 `tool_name + arguments + result`

- **压缩触发条件**（`memory/compressor.py` + `config.py`）：
  - `TOKEN_SOFT_THRESHOLD = 30000`：警告，准备压缩
  - `TOKEN_HARD_THRESHOLD = 50000`：强制压缩，目标 `TOKEN_TARGET = 15000`
  - `COMPRESS_THRESHOLD = 3`：超过 3 轮用户对话触发增量压缩

- **挑战与解决**：增量压缩的质量控制是最大的难点——如果压缩摘要丢失了关键信息，后续对话会出现"失忆"。我的优化措施：① 压缩 Prompt 中显式要求保留"未解决的任务、用户偏好/否定指令、关键参数值、已确认的方案"；② 摘要模型独立配置（默认 deepseek-chat，可使用更强模型）；③ 新旧摘要合并时保留最近 2000 字符的旧摘要作为上下文。

- **扩展思考**：当前长期记忆使用关键词匹配（而非向量检索），这是我已识别的主要改进方向。引入 embedding 模型 + ChromaDB 后，可将"卸载程序"和"移除软件"这类语义相近的查询正确关联。

---

### Q7. 你是如何做上下文拼接的？System Prompt 如何构建？

**参考答案**：

- **设计思路**：上下文拼接是 Agent 的核心工程问题。我采用"方案 B"（API 规范 format）——严格按照 `[system, user, assistant(tool_calls), tool, ...]` 的消息数组格式，这是 OpenAI/DeepSeek API 的标准格式，可以正确利用 prompt caching 和 tool calling 机制。

- **代码实现**：`memory/session.py:build_context_messages()`（第 1571 行）：

```python
def build_context_messages(session_id, user_query="", ...):
    # 模式1：无用户查询 → 仅触发压缩副作用
    # 模式2：构建 API 兼容 messages 数组
    result = [
        {"role": "system", "content": build_dynamic_system_prompt(session_id, persona_mode)}
    ]
    # 注入历史消息（最近3轮完整历史，按轮次边界切分）
    for record in load_recent_rounds(session_id, rounds=3):
        if role == "assistant" and tool_calls_raw:
            result.append({"role": "assistant", "tool_calls": parsed_tool_calls, ...})
        elif role == "tool":
            result.append({"role": "tool", "tool_call_id": ..., "content": ...})
    # 注入当前用户消息（附带上一轮工具报错）
    result.append({"role": "user", "content": user_content})
```

- **System Prompt 构建**：`build_dynamic_system_prompt()`（第 1461 行）分为静态部分和动态部分：

```
[静态前缀] core.md + output.md + safety.md
==== 动态上下文（本轮生效） ====
[长期记忆]
[历史摘要]
[当前任务规划]
[上一轮思考摘要]
```

静态/动态分离的意图是：静态部分跨轮次不变，利于 LLM API 的前缀缓存（如 DeepSeek 的 Context Caching）。

- **跨 Turn 历史注入**（`core/agent_state.py:load_cross_turn_history()` L705）：按完整轮次边界注入 user → assistant(tool_call) → tool → assistant(final_answer)，保证 tool_calls 和 tool 消息成对出现，不存在"孤儿 tool 消息"。序列合法性校验包括：清理孤儿 tool、丢弃无 tool_calls 的 tool_call 行、修剪尾部孤悬 assistant(tool_call)。

- **挑战与解决**：我注意到一个信息重复问题——`load_cross_turn_history()` 注入完整历史到 messages 数组，同时 `inject_memory_if_needed()`（`agent_state.py:491`）又将近期交互以 system 消息注入，造成同一工具结果在上下文中出现两次。当前尚未完全解决，优化方向是让记忆注入层感知跨 Turn 历史层的内容，做去重。

---

### Q8. 你是如何估计 Token 数量的？为什么不用 tiktoken？

**参考答案**：

- **设计思路**：tiktoken 库约 2MB，且需要额外下载编码文件。考虑到项目分发简便性，我选择了启发式估算——对 CJK 字符和 ASCII 字符使用不同的比例因子。

- **代码实现**：`memory/compressor.py:estimate_tokens()`（第 33 行）：

```python
def estimate_tokens(text):
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    ascii_count = len(text) - cjk_count
    # CJK: ~1.5 tokens/char, ASCII: ~0.25 tokens/char
    return max(1, int(cjk_count * 1.5 + ascii_count * 0.25))
```

- **准确性评估**：
  - 中文：每个汉字在 GPT/DeepSeek tokenizer 中约 1.5-2 个 token，系数 1.5 偏保守（倾向于高估，宁可早压缩不晚压缩）
  - 英文：约 0.25 token/字符（平均 4 字符/词，1 token/词），基本准确
  - 误差范围：通常在 ±20% 以内，对于 Token 预算管理足够

- **扩展思考**：如果未来需要精确 Token 计数（如按 token 计费场景），可以引入离线 tokenizer（如 `deepseek-tokenizer` 的 Python 绑定），在模型配置中指定 tokenizer 类型，按需加载。

---

### Q9. 你的压缩摘要策略是什么？如何保证压缩后的摘要不丢失关键信息？

**参考答案**：

- **设计思路**：压缩摘要质量直接决定长对话的可用性。我的策略是"增量合并 + 关键信息保护 + 独立压缩模型"。

- **代码实现**：

1. **增量合并**（`memory/session.py:_trigger_increment_compression()` L911）：
   - 每完成 1 个用户轮次且总轮次 > 3 时，取出最老 1 轮合并进摘要
   - 存入 summarie.db，标注覆盖轮次范围

2. **摘要生成 Prompt**（`memory/compressor.py:SUMMARY_PROMPT_TEMPLATE` L52）：
```python
"必须保留：未解决的任务、用户偏好/否定指令、关键参数值、已确认的技术方案。"
"省略：纯闲聊内容（问候/谢谢/表情等），简短确认语。"
```

3. **压缩独立性**：压缩使用独立的模型配置（`COMPRESS_MODEL_CONFIG`），默认为 `deepseek-chat`，可以配置为更强的模型以提高摘要质量，而不影响主对话的模型选择。

4. **新旧合并**：`increment_merge_summary()` 将旧摘要（最近 2000 字符）作为新摘要的前置上下文，保证信息的连续性。

- **挑战与解决**：我观察到的一个实际问题是——压缩后的摘要没有轮次范围标记。当摘要说"用户之前提到要生成 PPT"时，无法追溯这是第几轮的信息。修复方案在 `add_summary()` 中已经支持 `cover_turn_start`/`cover_turn_end` 字段，但 `compressor.py` 目前未充分利用这个元数据。这是待改进项。

- **扩展思考**：更好的方案是分层压缩——对每一轮生成"微观摘要"（100 字），然后对 N 轮微观摘要再生成"宏观摘要"（500 字）。这种层次化压缩类似于"论文摘要 ← 段落中心句 ← 原始段落"的金字塔结构。

---

## 3. 工具系统与执行安全

### Q10. 你的工具注册机制是怎么设计的？如果要新增一个工具，需要改哪些地方？

**参考答案**：

- **设计思路**：我实现了双路径工具注册——YAML 静态配置 + Python 运行时注册，以 `ToolDataProvider` 为单一真相源。新增一个简单工具只需一步（加 @register_tool 装饰器），无需修改任何核心代码。

- **代码实现**：

1. **运行时注册**：`tools/__init__.py:register_tool()` 装饰器（第 28 行）：

```python
@register_tool(timeout=60)
def web_search(query: str, max_results: int = 10) -> str:
    """使用多源搜索引擎搜索网页..."""
```

装饰器自动将函数名、docstring、参数签名注册到 `TOOL_REGISTRY`，然后通过 `_ensure_tool_registration()` 同步到 `ToolDataProvider`。

2. **YAML 配置增强**：`prompts/tools.yaml` 中可定义更丰富的工具元数据（描述、参数、域归属、标签、风险等级），运行时注册的参数会自动与 YAML 配置合并。
3. **Schema 自动生成**（`core/tool_data_provider.py:_build_parameter_schema()` L441）：从函数签名自动推断 JSON Schema（类型映射、required 字段识别、Optional/Union 处理）。内部参数（`_INTERNAL_PARAM_NAMES` 白名单：confirmed/challenge_token 等）自动从 Schema 中过滤，防止 LLM 看到并尝试使用。

4. **新增工具的完整流程**：
   - Step 1：在工具文件中编写函数 + `@register_tool(timeout=...)` 装饰器
   - Step 2：（可选）在 tools.yaml 中为工具添加域归属和增强描述
   - 完成。无需修改 `react_engine.py`、`ToolDataProvider`、`build_tools_param` 等任何核心管道代码。

- **挑战与解决**：我遇到过"运行时注册的工具参数描述缺失"问题——Python 函数签名的参数没有 description 字段，而 LLM 需要参数说明才能正确调用。解决方案是在 `_sync_all()` 中做参数级合并：YAML 的 `desc` 字段优先级高于运行时自动推断的 `desc`，两者互补。

---

### Q11. 你的安全执行沙箱是如何设计的？如果 LLM 试图执行 `rm -rf /` 会发生什么？

**参考答案**：

- **设计思路**：安全沙箱是我最重视的模块。LLM 本质上是不可信的——它可能被 prompt injection 诱导、可能"误解"用户意图（用户说"清空那个文件夹"，LLM 选错了路径）、甚至可能产生幻觉。我设计了四层递进式防护。

- **代码实现**：`tools/safe_exec.py`，核心函数 `run_powershell()`（第 473 行）：

**L0 - 永久拦截**（`always_blocked`）：无条件拒绝，不提供任何绕过方式。从 `safety_rules.yaml` 热加载。匹配逻辑在 `run_powershell()` 第 516-523 行：
```python
for i, pattern in enumerate(config.always_blocked):
    if re.search(pattern, decoded_script, re.IGNORECASE):
        return {"success": False, "message": "此命令被永久禁止执行"}
```

**L1 - 路径硬拦截**（`protected_path_patterns`）：修改类命令触碰受保护路径时直接拒绝。`_check_protected_paths()`（第 262 行）支持环境变量展开、通配符匹配、PowerShell 编码命令解码。例如 `del C:\Windows\System32\*` → 检测到 System32 受保护 → 硬拒绝。

```python
def _check_protected_paths(command, cwd=None, args_list=None):
    # 从原始命令和解码命令中提取所有路径
    # 路径展开环境变量 %APPDATA% → 实际路径
    # 通配符匹配：C:\Windows\* → 匹配 C:\Windows\System32
    # 返回 (block_hit, challenge_hit)
```

**L2 - 挑战短语验证**（`challenge_phrase`）：危险操作（如 `rm`、`del`、`Invoke-Expression`）需要用户输入完整授权文本才能执行。`_check_danger()`（第 424 行）含 15+ 个内置危险模式 + `safety_rules.yaml` 可扩展：

```python
builtin_rules = [
    (r'(?<![A-Za-z])rm\s+', '删除文件/目录 (rm 别名)', 'challenge'),
    (r'\b(?:iex|Invoke-Expression)\s+', '间接执行', 'challenge'),
    (r'\.\s*[\\/]?\s*[A-Za-z0-9_\-]+\s*\.ps1', '点源执行脚本', 'challenge'),
    # ... 15+ 条规则
]
```

特殊处理：PowerShell 编码命令会被解码后再检测（`_decode_powershell_script()`），字符串字面量会先剥离（`_strip_string_literals()`）降低误报。

**L3 - `subprocess.run(list, shell=False)`**：切断 OS shell，命令和参数分离，防止 shell 注入。

**如果 LLM 试图执行 `rm -rf /`**：
1. L2 首先命中 `(?<![A-Za-z])rm\s+` 模式 → 判定为危险操作
2. 系统返回 `challenge_required: True`，要求用户输入完整挑战短语
3. 如果用户拒绝或超时（300s），操作取消
4. 即使通过 L2，L3 使用 `shell=False` 执行，命令解析由 `shlex.split` 完成，不经过 shell
5. 如果命令涉及受保护路径（`/` 对应的 Windows 保护目录），L1 还会额外拦截

- **挑战与解决**：一个棘手的问题是误报——正常操作可能被检测为危险。例如用户在 PowerShell 中用 `Where-Object` 过滤后传递给 `Remove-Item` 是合理的。`_check_dangerous_pipeline()`（第 363 行）实现了管道安全白名单，识别安全的只读 cmdlet（`Get-*`/`Select-*`/`Where-*` 等 20+ 个前缀），减少误报。

---

### Q12. 你是如何防止 LLM 绕过安全确认的？比如 LLM 可能自学到在参数中传 `confirmed=True`？

**参考答案**：

- **设计思路**：这个问题非常关键。LLM 会从 system prompt 和工具描述中学习所有可用参数，一旦它发现 `confirmed` 参数并理解其作用，就可能尝试注入。我的防护策略是"从源头切断信息泄露"。

- **代码实现**：`core/tool_data_provider.py` 中的 `_INTERNAL_PARAM_NAMES`（第 196 行）：

```python
_INTERNAL_PARAM_NAMES = frozenset({
    "confirmed", "confirmation_token", "challenge_token", "challenge_response",
    "domain_confirmed", "_skip_validation", "prevalidated_uninstall_paths",
})
```

这个白名单在三个层面生效：

1. **JSON Schema 生成时过滤**（`_build_parameter_schema()` L445）：
```python
if self._is_internal_param(pname):
    continue  # 内部参数不出现在 LLM 可见的 OpenAPI schema 中
```

2. **系统提示词参数摘要时过滤**（`_format_params()` L387）：
```python
if self._is_internal_param(pname):
    continue  # 双保险：防止出现在工具列表文字描述中
```

3. **函数签名自省时过滤**（`register_tool()` L219）：`**kwargs` 和 `*args` 也不会出现在 schema 中。
- **挑战与解决**：我还处理了一个更隐蔽的绕过路径——LLM 可能尝试通过 `command` 参数中的 shell 语法间接执行危险操作（如 `cmd /c del file && reg delete ...`）。`_check_dangerous_pipeline()` 检测管道 `|` 和分号 `;` 后的危险命令，以及 `&` 调用运算符、点源执行（`. .\script.ps1`）等间接执行方式。

- **扩展思考**：更彻底的方案是使用 Docker 沙箱——工具执行完全隔离在容器内，只读挂载文件系统。但对于桌面 Agent 来说，这会严重限制功能。当前方案是在"功能完整性"和"安全防护"之间取的平衡点。

---

### Q13. 你的工具执行是如何做超时和错误处理的？子进程挂了怎么办？

**参考答案**：

- **设计思路**：工具执行失败是常态而非异常——网络超时、文件不存在、权限不足都可能发生。我的策略是"子进程隔离 + 超时强杀 + 分级熔断 + 自适应降级"。

- **代码实现**：`core/tool_data_provider.py:execute_one_step()`（第 948 行）：

**子进程隔离**（第 1249-1268 行）：
```python
worker_process = multiprocessing.Process(
    target=_tool_worker, args=(result_container, tool_func, arguments)
)
worker_process.start()
worker_process.join(timeout=timeout)  # 每个工具有独立 timeout

if worker_process.is_alive():
    worker_process.terminate()    # 第一级：SIGTERM
    worker_process.join(timeout=5)
    if worker_process.is_alive():
        worker_process.kill()     # 第二级：SIGKILL
```

**分级熔断**（第 1270-1283 行）：
- 同名同参数连续失败 3 次 → 任务终止（`MAX_CONSECUTIVE_FAILURES = 3`）
- 全局连续失败 4 次（跨工具） → 任务终止（`MAX_GLOBAL_FAILURES = 4`）
- 熔断逻辑：用 `tool_call_signature()` 生成 MD5 哈希比较签名

**失败分类**（`core/streaming.py:classify_tool_failure()` L495）：
```python
def classify_tool_failure(tool_result: dict) -> str:
    # fatal: 认证失败/权限不足/工具不存在 → 不计数，建议换方案
    # retryable: 超时/限流/网络错误 → 累计计数
    # file_not_found: 文件不存在 → 不计入任何熔断，允许换路径重试
    # empty_result: 搜索等返回空 → 不计入熔断
    # unknown: 其他错误
```

**自适应降级**（`handle_tool_failure()` L1575-1593）：连续 2 次空搜索结果 → 注入 system 提示要求 LLM 停止搜索，直接基于模型知识生成。

- **挑战与解决**：Windows 上的 `multiprocessing.Process` 有一个坑——子进程必须能 pickle 目标函数和参数。如果 `tool_func` 是 lambda 或闭包，pickle 会失败。我的解决方式是：① 将工作函数提取为模块级函数 `_tool_worker()`（`streaming.py:384`）；② 轻量工具（`load_domain_skills`/`ask_user_clarification`/`web_download`）直接在 `_IN_PROCESS_TOOLS` 中标记，走主进程执行路径（`execute_one_step()` L1153）。

---

## 4. 日志与可观测性

### Q14. 你的日志系统是如何设计的？如何追踪一次完整的 LLM 请求-响应？

**参考答案**：

- **设计思路**：我从一开始就设计了结构化日志而非简单 print——因为 Agent 的调试非常困难，需要跨文件、跨进程追踪信息流。我的日志系统分五个层级。

- **代码实现**：`logger.py`（~1982 行），核心类 `StructuredLogger`（第 58 行）：

**五级日志体系**：

| 层级 | Logger | 职责 | 格式 |
|:---|:---|:---|:---|
| StructuredLogger | `get_logger()` | 通用日志 | JSON Lines，按天轮转 |
| TankLogger | `get_tank_logger()` | Turn 级请求-响应追踪 | 每条 Turn 一个记录 |
| SessionLogger | `get_session_logger()` | 会话级追踪 | SubTurn 细节 |
| LLMRequestLogger | `get_llm_request_logger()` | LLM API 调用日志 | 请求/响应/耗时/token |
| ToolExecutionLogger | `get_tool_execution_logger()` | 工具调用日志 | 工具名/参数/结果/耗时 |

**JSON Lines 格式**：每条日志一个 JSON 对象，一行写入（第 13 行）。按天切分文件 `agent_YYYY-MM-DD.jsonl`。内存缓冲（50 条或 5 秒刷盘），异步后台线程写入。

**一次 LLM 请求的完整追踪链路**：
1. **请求前**：`TankLogger.log_turn_start()` → 记录 system_prompt + user_input
2. **调用中**：`LLMRequestLogger` → 记录完整 messages（截断至 2000 字符）+ 模型参数
3. **响应后**：解析 `usage`（prompt_tokens/completion_tokens）
4. **工具执行**：`ToolExecutionLogger` → 记录每次工具调用
5. **Turn 结束**：`TankLogger.log_turn_end()` → 记录 AI 回复 + 思考轨迹

- **挑战与解决**：日志模块代码量达到 1982 行，略显臃肿。这是因为我将格式化、刷盘、轮转、LLM 日志、工具日志都放在了一个文件中。已规划拆分为 `core/logging/formatter.py`、`sinks/file_sink.py`、`tracers/llm_tracer.py` 等模块。

---

### Q15. 在日志中如何处理敏感信息（如 API Key）？

**参考答案**：

- **代码实现**：`llm_client.py:_mask_api_keys()`（第 39 行）：

```python
def _mask_api_keys(obj: dict) -> dict:
    """递归屏蔽字典中的 API key 类字段。"""
    for k, v in obj.items():
        if k.lower() in ("api_key", "apikey", "authorization", "token", "secret"):
            masked[k] = "***"
        elif isinstance(v, dict):
            masked[k] = _mask_api_keys(v)  # 递归处理嵌套 dict
        elif isinstance(v, list):
            masked[k] = [_mask_api_keys(i) if isinstance(i, dict) else i for i in v]
```

- **安全边界**：API Key 存储使用系统 keyring（而非明文配置文件），获取时从 keyring 读取（`_try_keyring_get()` L100）。环境变量也受支持（`MYAGENT_API_KEY`）。

- **扩展思考**：当前仅在 LLM 日志层面做脱敏。工具执行结果中如果包含敏感信息（如 `execute_command` 返回了环境变量、文件内容包含密码），尚未统一脱敏。需要增加一个"工具输出扫描层"，用正则检测并脱敏身份证号、手机号、API key 模式。

---

### Q16. 你如何确保日志不丢失？程序崩溃时未刷盘的日志怎么办？

**参考答案**：

- **设计思路**：日志可靠性通过三层保障——内存缓冲 + 定时刷盘 + 全局异常钩子。

- **代码实现**：

1. **异步刷盘**（`logger.py:_writer_loop()` L162）：后台线程每 5 秒（`FLUSH_INTERVAL`）或缓冲达 50 条（`MAX_BUFFER_SIZE`）时自动刷盘。

2. **优雅停机**（`app.py:_setup_graceful_shutdown()` L61）：SIGTERM/SIGINT 信号处理，等待 1 秒让缓冲刷盘完成。

3. **全局异常钩子**（`logger.py:_register_global_exception_hook()`）：捕获所有未处理异常，写入 `crash.log`，确保崩溃时至少有一份原始错误信息。

- **已知局限**：如果 Python 进程被 `taskkill /F` 或断电，未刷盘的内存缓冲会丢失。对于 Agent 场景，最关键的 LLM 请求/响应日志是在响应到达时立即写入的（通过 `_llm_flush_interval = 2` 秒的更短间隔），丢失概率较低。

- **扩展思考**：生产环境建议使用 `concurrent-log-handler` 或外挂日志采集器（如 Filebeat → Elasticsearch），将日志持久化从应用层转移到基础设施层。

---

### Q17. 你的日志查询体验如何？如何快速找到某次对话的所有日志？

**参考答案**：

- **当前状态**：日志以 JSON Lines 按天存储，查询依赖 `grep` 或文本编辑器。通过 `trace_id` 字段可以关联同一次请求的所有日志行。

- **典型查询命令**：
```bash
# 查找 trace_id 的所有日志
grep '"trace_id":"abc123"' logs/agent_2026-08-03.jsonl | jq .
# 查找某个工具的所有调用记录
grep '"tool_name":"execute_command"' logs/agent_*.jsonl | jq '.duration_ms'
```

- **不足与改进方向**：缺乏可视化查询面板。已规划开发 `scripts/log_query.py` CLI 工具，支持：
  - `--trace-id`：按追踪 ID 过滤
  - `--time-range`：时间范围过滤
  - `--level`：日志级别过滤
  - `--tool`：按工具名过滤
  - 输出：时间线格式或汇总统计格式

---

## 5. 搜索与外部知识

### Q18. 你如何设计多源搜索的降级策略？为什么选择这个优先级顺序？

**参考答案**：

- **设计思路**：搜索不可靠是常态——API 可能欠费、限流、或被墙。我的降级链按"质量 + 可用性"排序。

- **代码实现**：`tools/search_tools.py:_multi_source_search()`（第 403 行）：

```python
providers = [
    ("bing_api",     lambda: _search_bing_api(...)),     # 质量最高，但需付费
    ("serp_api",     lambda: _search_serp_api(...)),     # Google 搜索，付费
    ("duckduckgo",   lambda: _search_duckduckgo(...)),   # 免费，稳定性好
    ("baidu",        lambda: _search_baidu(...)),        # 免费，中文效果好但反爬严格
]
```

**优先级逻辑**：Bing API → SerpAPI → DuckDuckGo → 百度 HTML 爬取。任一源返回非空结果即停止降级。全部失败时返回错误提示。

**每个源的失败模式**：
- Bing API：API key 未配置 → `RuntimeError`
- SerpAPI：同上
- DuckDuckGo：库未安装 → `ImportError`；或被限流
- 百度：HTML 结构变化 → 返回空列表

- **挑战与解决**：百度搜索是我最纠结的一个源。它的 HTML 爬取依赖页面 DOM 结构（多套 CSS 选择器兼容），百度反爬升级后就会失效。目前我在评估是否完全移除百度、改用 DuckDuckGo 作为中文搜索的唯二免费兜底（DDG 本身中文搜索质量在持续改善）。

---

### Q19. 你的搜索结果相关性校验算法具体怎么工作的？

**参考答案**：

- **设计思路**：搜索 API 返回的结果经常不相关——特别是中文搜索中，查询"Python 深度学习框架"可能返回"Python 基础教程"这类弱相关结果。我的相关性校验器使用混合打分而非单一指标。

- **代码实现**：`tools/search_tools.py:_compute_relevance()`（第 174 行）：

**中文查询路径**（查询包含中文字符时）：
```
总分数 = 标题分数 × 0.7 + 摘要分数 × 0.3

标题分数 = 字符重叠度 × 0.40 + 子串命中率 × 0.35 + bigram Jaccard × 0.25
```

1. **字符重叠度**（`_chinese_char_overlap()` L153）：查询中的汉字有多少出现在文本中。例如查询"Python爬虫"，标题"Python爬虫框架Scrapy" → 命中 {P,y,t,h,o,n,爬,虫} = 8/8 = 1.0。

2. **子串命中率**（`_substring_hit_rate()` L164）：查询中 2-4 字词组在文本中出现的比例。这是中文搜索最关键的特征——"卸载Qoder" 中的 "卸载" 是需要精确匹配的语义单元。

3. **bigram Jaccard**（`_jaccard()` L209）：标准 Jaccard 相似度，对英文/数字查询更有效。

**英文查询路径**：仅使用 bigram Jaccard（英文的 bigram 稀疏性使其更准确）。

**一票否决机制**（`validate_relevance()` L222）：
- 域名黑名单（目前为空，保留扩展能力）
- 垃圾标题模式（`_JUNK_TITLE_PATTERNS`）：过滤"是什么意思""_百度百科""怎么读"等词典类结果

**缓存**（`_SearchCache` L84）：MD5(query+func_name) 为 key，TTL 300 秒，LRU 最大 200 条。线程安全的读写锁。

- **挑战与解决**：中文相关性打分的最大难点是 bigram Jaccard 的"分母膨胀"——长查询如"2026年AI技术发展趋势普通人有什么机会"产生大量 bigram，导致 Jaccard 分数极低（< 0.05），即使结果高度相关。解决方案是引入字符重叠和子串匹配作为主要特征（各占 40% 和 35%），将 bigram 的权重降到 25%，同时将默认阈值从 0.10 降到 0.06。

---

### Q20. 你是如何抓取网页正文内容的？遇到 JS 渲染的页面怎么办？

**参考答案**：

- **当前实现**：`tools/search_tools.py:fetch_webpage()`（第 589 行）使用 `requests` + 正则提取：

```python
# 移除 script/style/nav/footer/header 标签
html = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', '', html, ...)
# 提取 <p>/<article>/<div class="content"> 内容
paragraphs = re.findall(r'<(?:p|article|div class="content")[^>]*>(.*?)</...>', html, ...)
```

- **已知局限**：无法处理 JS 渲染的 SPA 页面（如 React/Vue 应用）、反爬验证码。

- **改进方向**：引入 Playwright headless 浏览器作为 fallback——当 `requests` 返回的正文为空或过短时，自动启动 headless Chrome 获取渲染后的 DOM。已经在技术选型评估中，但考虑到安装 Chromium 的体积（~200MB），暂未加入默认依赖。

---

### Q21. 你的搜索缓存策略是什么？如何处理搜索结果的有效期？

**参考答案**：

- **代码实现**：`tools/search_tools.py:_SearchCache`（第 84 行）：

```python
class _SearchCache:
    def __init__(self, ttl: int = 300, max_size: int = 200):
        self._cache: Dict[str, Tuple[float, str]] = {}  # key → (timestamp, value)
        # TTL = 300 秒（5分钟）
        # max_size = 200 条（LRU 淘汰）
```

- **缓存键**：`MD5(f"{func_name}:{query.lower().strip()}")`，大小写不敏感。

- **有效期策略**：5 分钟 TTL 是基于"同一用户的搜索意图在短时间内通常不变"的假设。对于 `search_news`，新闻的时效性更高，但当前 TTL 与普通搜索一致，这是可优化点——新闻搜索的 TTL 应缩短到 60 秒。

- **缓存淘汰**：LRU 策略（`min(self._cache, key=lambda k: self._cache[k][0])`），超过 200 条时淘汰最旧条目。

---

## 6. 文件生成与数据处理

### Q22. 你的 PPT 生成是如何实现的？从用户需求到最终 .pptx 文件的完整流程是怎样的？

**参考答案**：

- **设计思路**：我选择了 HTML → PPTX 的转换路径而非直接用 python-pptx API。原因是：LLM 擅长生成 HTML/CSS（训练数据中有大量网页布局），但直接生成 python-pptx 代码容易出错。HTML 作为中间表示层，让 LLM 用熟悉的格式描述布局，然后由渲染引擎转译为 PPTX。

- **代码实现**：

1. **LLM 生成 HTML**：工具 `ppt_tools.py` 中的 `ppt_create` 等工具引导 LLM 生成符合模板规范的 HTML（含特定 CSS 类名和数据属性）。

2. **HTML → PPTX 渲染**：`ppt_generator/renderer.py` 解析 HTML DOM，映射到 python-pptx 元素：
   - `<h1>` → 标题文本框
   - `<div class="slide">` → 新幻灯片
   - `<table>` → pptx 表格
   - `<img>` → 图片形状
   - CSS 变量（`--primary-color` 等） → 主题色

3. **主题系统**：`ppt_generator/themes.py` 预定义多套配色方案和字体组合，LLM 可通过 CSS class 引用。

- **挑战与解决**：HTML 到 PPTX 的布局映射是最难的部分——HTML 是流式布局，PPTX 是绝对定位。当前渲染器做了简化处理：每个 `<div class="slide">` 内部元素按序垂直堆叠，不支持复杂的分栏/重叠布局。对于需要精确布局的场景，建议 LLM 使用 `python-pptx` 的原生 API（通过 `code_tools.py` 执行生成的代码）。

- **扩展思考**：更好的方案是双路径——简单布局走 HTML→PPTX（快速、LLM 友好），复杂布局走 python-pptx 代码生成（精确、灵活）。当前已有 python-pptx 作为依赖，可以扩展这个方向。

---

### Q23. 你的 Word 文档生成是如何实现的？如何处理格式、表格、图片等复杂元素？

**参考答案**：

- **代码实现**：`tools/word_tools.py` 使用 `python-docx` 库操作 Word 文档。支持的操作包括：创建文档、添加标题/段落/表格/图片、设置字体样式。LLM 通过函数调用传递结构化的内容描述，工具内部转换为 python-docx API 调用。

- **典型流程**：用户说"帮我生成一份项目周报" → LLM 调用 `create_word_document(title="项目周报")` → 多次调用 `add_paragraph`/`add_table` 填充内容 → 最终保存文件。

- **局限**：当前不支持复杂格式（页眉页脚、分节符、目录生成、邮件合并）。这些功能需要在 Word 中手动完成，或通过 VBA 宏自动化——Agent 可以通过 `execute_command` 调用 Word 的 COM 接口来实现高级操作。

---

### Q24. 你的 Excel 数据处理能力如何？如何实现数据分析和图表生成？

**参考答案**：

- **代码实现**：`tools/excel_tools.py`（通过 `prompts/tools.yaml` 中注册的域 `excel_analyze` 管理）。使用 `openpyxl` 库。

- **支持的操作**：`excel_write`（写入数据）、`excel_pivot`（数据透视表）、`excel_chart_advanced`（图表生成）、`excel_formula`（公式写入）、`excel_conditional_format`（条件格式）、`excel_merge`（合并单元格）、`excel_batch_process`（批量处理）。

- **实际工作流示例**：
  1. LLM 调用 `read_file` 读取 CSV/XLSX 文件
  2. 解析数据结构，调用 `excel_write` 或 `excel_pivot` 进行分析
  3. 调用 `excel_chart_advanced` 生成可视化图表
  4. 通过 `excel_convert` 导出为需要的格式

- **已知局限**：`excel_pivot` 和 `excel_chart_advanced` 的实现在 tools.yaml 中注册但底层 python 实现可能不完整——在代码中标记为"需完善"。数据分析的真实能力取决于 LLM 能否正确理解数据结构并生成准确的操作参数。

---

## 7. 异常处理与鲁棒性

### Q25. 你是如何防止 LLM 陷入无限循环的？有哪些熔断机制？

**参考答案**：

- **设计思路**：LLM 无限循环是 Agent 开发中最常见的 bug——LLM 对同一工具重复调用、对同一参数反复尝试、或者在"调用→失败→重试"中空转。我实现了多层防御体系。

- **代码实现**：

**Layer 1: 精确签名去重**（`ToolLoopDetector` L561-577）：
```python
signature = compute_call_signature(tool_name, arguments)  # MD5(tool_name+sorted_args)
if signature == self._last_signature:
    self._consecutive_exact_repeat += 1
    if self._consecutive_exact_repeat > 2:
        raise ToolLoopError("工具以完全相同参数连续调用3次，判定为无效循环")
```

**Layer 2: 同名工具频率**（L579-589）：
- 同一工具累计调用 > 10 次 → 硬终止
- 搜索工具（`web_search`/`file_search` 等）豁免此限制

**Layer 3: 全局步数上限**（`MAX_TURNS = 15`）：主循环硬限制，超过后强制生成进度摘要并终止。

**Layer 4: 进展评估**（`ProgressEvaluator`）：
- PROGRESS：有用功，继续
- STALLED：无进展 → 注入限制提示（禁止重复上一步，要求更换方案）
- FUTILE：徒劳 → 直接终止并给出建议

**Layer 5: 断路器**（`BreakerState`）：
- 同名同参数连续失败 3 次 → 终止
- 全局连续失败 4 次（跨工具） → 终止
- 连续空搜索 2 次 → 注入降级提示

**Layer 6: WARNING_TURN 提前告警**（`react_engine.py:217`）：在第 12 轮生成进度摘要，让 LLM 意识到"快超时了，该总结了"。

- **挑战与解决**：一段实际的无限循环案例——LLM 反复调用 `search_files(keyword="config")` 寻找不存在的文件，每次返回空结果但不改变策略。`ToolLoopDetector` 的 Layer 1（精确签名去重）在第三次调用时精准捕获。

---

### Q26. 任务执行到一半崩溃了怎么办？你的检查点恢复机制如何工作？

**参考答案**：

- **设计思路**：Agent 可能因为各种原因中断——LLM API 超时、用户关闭浏览器、系统重启。检查点机制确保中断后可以无缝恢复。

- **代码实现**：`core/agent_state.py` 中的 `TaskCheckpoint`（第 50 行）和 `CheckpointManager`（第 134 行）：

```python
@dataclass
class TaskCheckpoint:
    session_id: str         # 会话 ID
    user_task: str          # 原始用户任务
    turn: int               # 当前轮次号
    current_step: int       # 当前子步骤
    messages: List[Dict]    # 完整对话历史（JSON 序列化）
    completed_steps: List   # 已完成步骤列表
    remaining_steps: List   # 待完成步骤
    current_tool_name: str  # 当前正在执行的工具
    progress_summary: str   # 进度摘要
```

**存储**：SQLite（`checkpoints.db`，WAL 模式），24 小时自动清理（`cleanup_age_hours=24`）。

**保存时机**（多处 `save_checkpoint()` 调用）：
- 每次工具执行完成后（成功或失败）
- WARNING_TURN 达到时
- 异常捕获时

**恢复流程**（`prepare_run_state()` L1127-1157）：
```python
if resumed_checkpoint:
    engine.completed_steps = resumed_checkpoint.completed_steps
    remaining_steps = resumed_checkpoint.remaining_steps
    # 注入恢复提示到 system prompt
    resume_prompt = f"""【任务恢复】以下是之前未完成任务的状态：
    已完成步骤：{json.dumps(engine.completed_steps, ...)}
    剩余步骤：{json.dumps(remaining_steps, ...)}
    请继续执行后续步骤，不要重复已完成的工作。"""
    # 从检查点恢复对话消息历史
    conversation_messages = [dict(m) for m in resumed_checkpoint.messages]
```

- **挑战与解决**：一个实际问题是检查点的 `messages` 字段随对话增长会变得非常大（100+ 轮对话 → JSON 可能超过 100KB）。当前方案是完整序列化，已规划优化为增量保存——仅保存差异 messages。

---

### Q27. 如果用户中途取消任务，你的系统如何响应？

**参考答案**：

- **设计思路**：取消操作需要"快速响应"和"优雅收尾"。我使用 `threading.Event` 作为取消信号，在多个检查点进行轮询。

- **代码实现**：

1. **取消信号传递**：`ReActEngine.run()` 接收 `cancel_event` 参数（一个 `threading.Event` 对象），前端通过取消 API 设置该 event。

2. **检查点**（在 3 个关键位置）：
```python
# 检查点1：每轮 LLM 调用前
if cancel_event is not None and cancel_event.is_set():
    yield from safe_final(self, state, "任务已取消", outcome="cancelled")
    return

# 检查点2：工具执行前
if cancel_event is not None and cancel_event.is_set():
    yield from safe_final(self, state, "任务已取消", outcome="cancelled")
    return

# 检查点3：WARNING_TURN 进度摘要生成前
if cancel_event is not None and cancel_event.is_set():
    yield from safe_final(self, state, "任务已取消", outcome="cancelled")
    return
```

3. **优雅收尾**：取消时仍调用 `safe_final()` 保存 AI 的最终回复（msg_type="final_answer"），并触发 `on_emit_final()` 完成 Turn 日志记录。

- **不足**：如果 LLM 调用正在进行中（流式响应传输中），取消信号无法立即中断——需要等待当前 chunk 传输完成。解决方案是使用 `requests` 的流式响应 + 逐 chunk 检查取消信号，在 `_deepseek_call_stream` 和 `_ollama_call_stream` 中实现。

---

### Q28. 如果用户连续发同样的消息会怎样？你的系统如何处理重复请求？

**参考答案**：

- **代码实现**：`memory/session.py:_detect_repeated_query()`（第 299 行）：

```python
def _detect_repeated_query(messages, current_user_msg, similarity_threshold=0.35):
    # 使用 bigram Jaccard 检测历史中是否有相似用户消息
    # 相似度 ≥ 0.35 → 排除该轮 AI 回复（从上下文中移除旧回复）
```

- **处理逻辑**：不忽略用户请求（用户可能是故意重试），但会从历史上下文中排除之前相同问题的 AI 回复，防止 LLM 看到"两轮相同的问答"后产生困惑或重复输出。

- **额外保护**：`ToolLoopDetector` 会在工具调用层面防止重复——如果 LLM 对同一问题总是调用同一工具，layer 1（精确签名去重）会在第 3 次触发。

---

## 8. 性能与扩展性

### Q29. 你是如何优化 LLM 调用成本的？有哪些降低 Token 消耗的策略？

**参考答案**：

- **多层 Token 优化策略**：

1. **Prompt Caching 友好设计**（`build_dynamic_system_prompt()`）：静态 system prompt（core.md + output.md + safety.md）放在前缀，动态部分（长期记忆、摘要、规划）放在尾部。OpenAI/DeepSeek 的 prompt caching 会自动缓存前缀未变化的部分——我的设计使缓存命中率最大化。

2. **情节缓存裁剪**（`memory/session.py:_clean_reasoning_for_archive()` L517）：超过 3 轮的历史轮次剥离 `reasoning_trace`，仅保留工具名+参数+精简结果（从可能的 2000+ token 精简到 ~200 token）。

3. **工具结果截断**（`_truncate_result()` `core/streaming.py:282`）：长工具结果保留头部+尾部，中间截断。搜索结果的 snippet 截断到 200 字符。

4. **Token 预算三级管控**：SOFT 30K（警告）→ HARD 50K（强制压缩到 15K target），确保不超出 LLM 上下文窗口。

5. **压缩模型分离**：摘要压缩使用便宜模型（deepseek-chat），不消耗主力模型的 token。

- **量化效果**：基于设计意图估计——以 20 轮对话为例，情节缓存裁剪可节省 ~60% 的子轮次 token，增量压缩可节省 ~40% 的历史对话 token。

---

### Q30. 如果要把这个系统从单用户扩展到多用户，你需要改哪些地方？

**参考答案**：

- **需要修改的核心模块**：

1. **会话隔离**：当前 `memory/session.py` 使用 `session_id` 做数据隔离（per-session SQLite + per-session summaries.db），这个设计天然支持多用户——只需在 session 创建时关联 user_id。SQLite 切换到 PostgreSQL 更为合适。

2. **工具执行安全**：当前 `safe_exec.py` 在多用户场景下需要额外加固——确保用户 A 的命令不能访问用户 B 的文件。需要引入：
   - per-user 工作目录（`cwd` 绑定到用户专属目录）
   - `_check_protected_paths()` 增加用户边界检查
   - Docker 容器隔离（每个用户的工具执行在独立容器中）

3. **LLM API 调度**：需要 API key 池管理 + 限流——每个用户独立的 API key，或所有用户共享 key 但加入 token 桶限流。

4. **Web 后端**：当前 Flask 单进程部署已无法满足。需要：
   - 用户认证（JWT/Session）
   - WebSocket 替代 SSE（需要双向通信的场景）
   - 任务队列（Celery/RQ）处理长时间运行的 Agent 任务

5. **状态管理**：`GlobalStateManager` 是进程内单例，多用户需要改为 Redis 分布式状态。

6. **日志**：当前按时间切分文件，多用户需要增加 user_id 字段并接入集中式日志系统（ELK/Loki）。

- **不需要改的**：ReAct 主循环、记忆分层架构、工具注册机制、BM25 路由——这些核心逻辑是用户无关的，可以复用。

---

## 附录：问题快速索引

| # | 类别 | 问题 |
|:---|:---|:---|
| Q1 | 架构设计 | 为什么选择 ReAct 模式？主循环如何编排？ |
| Q2 | 架构设计 | JSON 解析失败怎么处理？ |
| Q3 | 架构设计 | Agent 状态管理如何保证线程安全？ |
| Q4 | 架构设计 | 话题切换检测为什么不用 LLM？ |
| Q5 | 架构设计 | 和 LangChain 的核心区别？为什么不用？ |
| Q6 | 记忆与上下文 | 分层记忆系统每一层存储什么？ |
| Q7 | 记忆与上下文 | 上下文拼接策略？System Prompt 如何构建？ |
| Q8 | 记忆与上下文 | Token 估算为什么不用 tiktoken？ |
| Q9 | 记忆与上下文 | 压缩摘要策略？如何保证不丢失关键信息？ |
| Q10 | 工具与安全 | 工具注册机制？新增工具需要改哪里？ |
| Q11 | 工具与安全 | 安全沙箱四层防护？rm -rf / 会被拦截吗？ |
| Q12 | 工具与安全 | 如何防止 LLM 传 confirmed=True 绕过安全确认？ |
| Q13 | 工具与安全 | 工具执行超时和子进程挂了怎么处理？ |
| Q14 | 日志与可观测性 | 日志系统五级设计？如何追踪请求链路？ |
| Q15 | 日志与可观测性 | 如何处理敏感信息（API Key）？ |
| Q16 | 日志与可观测性 | 崩溃时未刷盘的日志怎么办？ |
| Q17 | 日志与可观测性 | 日志查询体验？如何快速找到某次对话日志？ |
| Q18 | 搜索与外部知识 | 多源搜索降级策略？优先级顺序的理由？ |
| Q19 | 搜索与外部知识 | 相关性校验算法怎么工作？ |
| Q20 | 搜索与外部知识 | 网页抓取实现？JS 渲染页面怎么办？ |
| Q21 | 搜索与外部知识 | 搜索缓存策略？ |
| Q22 | 文件生成 | PPT 生成的完整流程（HTML→PPTX）？ |
| Q23 | 文件生成 | Word 文档如何处理复杂元素？ |
| Q24 | 文件生成 | Excel 数据分析和图表生成能力？ |
| Q25 | 异常与鲁棒性 | 如何防止无限循环？六层熔断机制？ |
| Q26 | 异常与鲁棒性 | 检查点恢复机制？崩溃后如何继续？ |
| Q27 | 异常与鲁棒性 | 用户中途取消任务如何响应？ |
| Q28 | 异常与鲁棒性 | 重复请求如何处理？ |
| Q29 | 性能与扩展 | 如何优化 LLM 调用成本？ |
| Q30 | 性能与扩展 | 单用户扩展到多用户需要改什么？ |
