# 经验内化功能使用指南

## 功能介绍

经验内化功能允许 Agent 从对话历史中提取任务处理经验，并自动生成可复用的 Skill 文件。这样，Agent 就可以从实际对话中持续学习和进化。

## 触发方式

在对话中，当用户发送包含以下关键词的消息时，会触发经验内化：

- 中文：`内化经验`、`保存为技能`、`保存技能`、`学习这次`、`记住这次`、`把这次`、`经验内化`、`生成技能`、`创建技能`、`提取经验`
- 英文：`learn from`、`save as skill`、`create skill`

## 处理流程

1. **意图检测**：Agent 检测到用户消息包含经验内化关键词
2. **提取步骤**：从当前会话历史中提取任务处理步骤（使用的工具、处理顺序等）
3. **生成技能名称**：使用 LLM 根据任务描述建议一个简洁的技能名称
4. **生成技能内容**：使用 LLM 将任务处理步骤生成 Skill Markdown 内容
5. **保存技能**：将生成的 Skill 内容保存到 `prompts/skills/` 目录
6. **通知用户**：通过 SSE 返回保存结果（技能名称、保存路径）

## 示例

### 示例 1：处理 Excel 后内化经验

**用户**：帮我处理这个 Excel 文件，统计每个部门的人数

**Agent**：（执行任务，使用 `read_file`、`run_python` 等工具处理 Excel 文件）

**用户**：把这次处理的经验内化为新技能

**Agent**：✅ 经验内化完成！

技能名称：**excel_department_stats**
保存路径：`D:\PyCharm Project\MyAgent\prompts\skills\excel_department_stats.md`

你可以查看并修改这个技能文件，下次处理类似任务时我会自动使用这个技能。

### 示例 2：代码生成后保存技能

**用户**：帮我写一个 Python 脚本，用于批量重命名文件

**Agent**：（生成代码，提供脚本）

**用户**：保存为技能

**Agent**：✅ 经验内化完成！

技能名称：**batch_rename_files**
保存路径：`D:\PyCharm Project\MyAgent\prompts\skills\batch_rename_files.md`

## 技能文件格式

生成的技能文件是 Markdown 格式，包含以下部分：

```markdown
# 技能名称

## 工具使用
本次任务使用了以下工具：...

## 处理步骤
1. 分析用户需求
2. 选择合适的工具
3. 执行任务
4. 返回结果

## 注意事项
- 确保工具参数正确
- 处理大文件时注意性能
```

## 后续改进

1. **自动注册**：在经验内化完成后，自动将新技能注册到 `tools.yaml` 的 `domains` 中，以便后续自动匹配
2. **用户确认**：在保存技能之前，先询问用户是否要调整技能内容
3. **指定名称**：支持用户在触发经验内化时指定技能名称（例如："把这次的经验保存为技能，名称是 'Excel 部门统计'"）
4. **技能管理**：提供技能列表、查看、编辑、删除等功能

## 技术实现

- **意图检测**：`tools/skill_learning.py` 中的 `is_skill_learning_request()` 函数
- **步骤提取**：`tools/skill_learning.py` 中的 `extract_task_steps_from_history()` 函数
- **技能生成**：`tools/skill_learning.py` 中的 `generate_skill_with_llm()` 函数
- **技能保存**：`tools/skill_learning.py` 中的 `save_skill()` 函数
- **集成点**：`backend/agent.py` 中的 `run_agent()` 函数，在 Phase 1（域匹配）之后、Phase 2（构建 System Prompt）之前

## 注意事项

1. 经验内化功能会在当前会话的**所有历史消息**中提取步骤，包括当前轮次之前的消息
2. 生成的技能文件会立即生效（因为 `tools/__init__.py` 中的 `_load_prompt()` 函数使用了文件 mtime 缓存自动失效机制）
3. 如果技能名称已存在，会自动添加时间戳避免覆盖
4. 技能生成质量取决于 LLM 的能力，可能需要人工审核和调整

## 故障排除

如果遇到问题，可以检查以下内容：

1. 确认用户消息包含触发关键词
2. 检查 `prompts/skills/` 目录是否存在，是否有写入权限
3. 查看日志文件（`logs/agent_*.log` 或 `logs/llm_calls/*/`）
4. 检查 LLM 调用是否正常（API Key 是否有效，Ollama 是否运行等）
