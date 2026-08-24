"""
经验内化模块：从对话历史中提取任务处理经验，生成可复用的 Skill，暂时未完整实现
"""
import os
import json
import datetime
import re
import shutil
from pathlib import Path
import yaml

from . import register_tool

# 技能保存目录（与内置技能分离）
AGENT_SKILLS_DIR = Path(__file__).parent.parent / "agent_skills"

# 技能注册文件（与内置 tools.yaml 分离）
AGENT_TOOLS_YAML = AGENT_SKILLS_DIR / "agent_tools.yaml"

# 触发词列表（优化：分为精确匹配和前缀匹配）
EXACT_TRIGGER_KEYWORDS = [
    "内化经验", "保存为技能", "保存技能",
    "学习这次", "记住这次",
    "经验内化", "生成技能", "创建技能", "提取经验",
]

PREFIX_TRIGGER_KEYWORDS = [
    "把这次", "将这次", "让agent学习",
]

# 快速过滤关键词（用于 LLM 意图检测前的快速筛选）
QUICK_FILTER_KEYWORDS = ["技能", "经验", "学习", "skill", "learn"]


@register_tool
def is_skill_learning_request(user_message: str) -> bool:
    """
    检测用户消息是否包含经验内化意图（优化版：正则 + 词边界）
    
    优化点：
    - 精确触发词：完整匹配
    - 前缀触发词：出现在消息开头或结尾
    - 英文触发词：独立短语匹配
    """
    message_lower = user_message.lower().strip()
    
    # 1. 检查精确触发词（完整匹配）
    for keyword in EXACT_TRIGGER_KEYWORDS:
        if keyword in message_lower:
            return True
    
    # 2. 检查前缀触发词（出现在消息开头或结尾）
    for prefix in PREFIX_TRIGGER_KEYWORDS:
        # 出现在开头
        if message_lower.startswith(prefix):
            return True
        # 出现在结尾（后面只能有标点符号）
        if re.search(re.escape(prefix) + r"[，。！？\s]*$", message_lower):
            return True
    
    # 3. 英文触发词（独立短语）
    english_patterns = [
        r"\blearn\s+from\b",
        r"\bsave\s+as\s+skill\b",
        r"\bcreate\s+skill\b"
    ]
    for pattern in english_patterns:
        if re.search(pattern, message_lower):
            return True
    
    return False


@register_tool
def extract_task_steps_from_history(messages: list[dict]) -> dict:
    """
    从对话历史中提取任务处理步骤（优化版）。
    
    优化点：
    - 动态选择消息范围（基于任务相关性，而非硬截断 20 条）
    - 准确提取工具调用（从 tool_calls 或 content 中解析）
    - 准确判断消息类型（避免误判）
    
    参数：
        messages: 对话历史消息列表（已按时间排序）
    
    返回：
        {
            "task_description": "任务描述",
            "steps": [...],
            "tools_used": ["read_file", "run_python", ...],
            "summary": "处理步骤总结"
        }
    """
    # 1. 动态选择消息范围
    task_start_index = _find_task_start_index(messages)
    relevant_messages = messages[task_start_index:]
    
    # 2. 提取步骤
    tools_used = set()
    steps = []
    
    for msg in relevant_messages:
        role = msg["role"]
        content = msg.get("content", "")
        
        if role == "assistant":
            # 尝试从 tool_calls 中提取
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    tool_name = tc.get("function", {}).get("name", "")
                    tools_used.add(tool_name)
                    
                    arguments = tc.get("function", {}).get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments) if arguments else {}
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}  # 如果解析失败，保留原始字符串
                    
                    steps.append({
                        "role": "assistant",
                        "content": content[:500],
                        "tool_name": tool_name,
                        "tool_args": arguments
                    })
            else:
                # 如果没有 tool_calls，尝试从 content 中提取 JSON
                tool_name, tool_args = _extract_tool_call_from_content(content)
                if tool_name:
                    tools_used.add(tool_name)
                    steps.append({
                        "role": "assistant",
                        "content": content[:500],
                        "tool_name": tool_name,
                        "tool_args": tool_args
                    })
                else:
                    steps.append({
                        "role": "assistant",
                        "content": content[:500]
                    })
        
        elif role == "user":
            # 判断是否为工具返回结果
            if _is_tool_result(content):
                steps.append({
                    "role": "tool_result",
                    "content": content[:500],
                    "tool_name": _extract_tool_name_from_result(content)
                })
            else:
                steps.append({
                    "role": "user",
                    "content": content[:500]
                })
    
    return {
        "task_description": _extract_task_description(messages),
        "steps": steps,
        "tools_used": list(tools_used),
        "summary": f"使用工具：{', '.join(tools_used) if tools_used else '无'}"
    }


def _find_task_start_index(messages: list[dict]) -> int:
    """
    查找任务开始的索引（从最后一条消息向前查找）
    
    启发式：
    - 查找第一条包含任务开始标记的消息（"帮我"、"请"、"我想"等）
    - 最多向前查找 50 条消息
    """
    task_markers = ["帮我", "请", "我想", "能否", "可以", "如何", "怎么", "帮我"]
    
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg["role"] == "user":
            content = msg.get("content", "")
            if any(marker in content for marker in task_markers):
                return i
    
    # 如果找不到，返回最后 50 条消息（或所有消息）
    return max(0, len(messages) - 50)


def _is_tool_result(content: str) -> bool:
    """
    判断消息是否为工具返回结果（优化版）
    
    方法：
    1. 检查是否包含 JSON 结构（工具通常返回 JSON）
    2. 检查是否包含工具返回的常见前缀
    3. 检查是否包含错误信息（工具执行失败时会返回错误）
    """
    # 方法 1：检查是否包含 JSON 结构
    content_stripped = content.strip()
    if content_stripped.startswith("{") or content_stripped.startswith("["):
        try:
            json.loads(content_stripped[:100])  # 只尝试解析前 100 字符
            return True
        except Exception:
            pass
    
    # 方法 2：检查是否包含工具返回的常见前缀
    tool_result_prefixes = ["工具", "返回：", "执行结果", "调用结果", "工具返回"]
    if any(prefix in content for prefix in tool_result_prefixes):
        return True
    
    # 方法 3：检查是否包含错误信息
    error_keywords = ["错误", "失败", "error", "failed", "timeout"]
    if any(kw in content.lower() for kw in error_keywords):
        # 但需要确保不是用户在描述问题
        if "工具" in content or "执行" in content:
            return True
    
    return False


def _extract_tool_name_from_result(content: str) -> str:
    """从工具返回结果中提取工具名称"""
    # 尝试从 "工具 XXX 返回：" 中提取
    match = re.search(r"工具\s+(\S+)\s+返回", content)
    if match:
        return match.group(1)
    
    # 尝试从 JSON 中提取
    try:
        data = json.loads(content.strip()[:500])
        if isinstance(data, dict) and "tool_name" in data:
            return data["tool_name"]
    except Exception:
        pass
    
    return "unknown"


def _extract_tool_call_from_content(content: str) -> tuple[str, dict]:
    """
    从 assistant 消息的 content 中提取工具调用
    
    返回：
        (tool_name, arguments) 或 ("", {})
    """
    # 尝试解析完整 JSON
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "tool_name" in data:
            return data["tool_name"], data.get("arguments", {})
    except Exception:
        pass
    
    # 尝试使用正则提取
    match = re.search(r'"tool_name"\s*:\s*"([^"]+)"', content)
    if match:
        tool_name = match.group(1)
        args_match = re.search(r'"arguments"\s*:\s*({.*?})', content, re.DOTALL)
        if args_match:
            try:
                args = json.loads(args_match.group(1))
                return tool_name, args
            except Exception:
                pass
        return tool_name, {}
    
    return "", {}


def _extract_task_description(messages: list[dict]) -> str:
    """提取任务描述"""
    for msg in messages:
        if msg["role"] == "user":
            return msg["content"][:200]
    return "未知任务"


@register_tool
def generate_skill_with_llm(task_info: dict, llm_call_func) -> str:
    """
    使用 LLM 将任务处理步骤生成 Skill Markdown 内容（优化版）。
    
    优化点：
    - 智能截断 JSON（按消息边界截断，不破坏结构）
    - 添加已有技能格式示例到 Prompt
    - 引导 LLM 遵循现有技能的风格
    
    参数：
        task_info: extract_task_steps_from_history 的返回值
        llm_call_func: LLM 调用函数（非流式，返回完整文本）
    
    返回：
        Skill Markdown 内容（字符串）
    """
    # 智能截断 steps（按消息边界）
    steps_json = _truncate_steps_intelligently(task_info['steps'], max_chars=3000)
    task_info['steps_json'] = steps_json
    
    # 读取已有技能文件作为格式示例
    example_skills = _load_example_skills()
    
    # 构建 Prompt
    prompt = f"""你是一个 AI Agent 的技能编写专家。请根据以下对话历史，为 Agent 编写一个可复用的技能（Skill）。

## 技能格式要求
技能文件是 Markdown 格式，使用 YAML frontmatter 存储元数据。应该包含以下部分（参考现有技能的格式）：

{example_skills}

## 任务描述
{task_info['task_description']}

## 使用的工具
{', '.join(task_info['tools_used']) if task_info['tools_used'] else '无'}

## 对话历史（最近部分）
{task_info['steps_json']}

## 编写要求
1. 技能内容应该简洁、实用，直接指导 Agent 的行为
2. 使用中文编写
3. 遵循现有技能的格式风格（参考上面的示例）
4. 包含具体的流程和规则（Agent 在处理此类任务时应该遵循的步骤）
5. 包含工具使用的最佳实践（基于本次对话中成功的方法）
6. 包含注意事项和常见错误（如果有的话）
7. 不要在正文重复 YAML frontmatter 中的信息

## 输出格式要求
直接输出 Markdown 内容（包含 YAML frontmatter），不要包含 ```markdown``` 代码块标记。

**必须包含以下部分：**

1. **YAML frontmatter**（必须包含 keywords）：
```yaml
---
skill_name: excel_department_stats
version: 1.0
created_at: 2026-06-29 22:00:00
keywords:
  - 统计部门人数
  - 部门统计
  - Excel统计
  - 人数统计
---
```

2. **KEYWORDS 行**（在正文开头，方便解析）：
在 YAML frontmatter 之后、# 标题之前，添加一行：
`KEYWORDS: 关键词1, 关键词2, 关键词3`

**完整示例：**
```
---
skill_name: excel_department_stats
version: 1.0
created_at: 2026-06-29 22:00:00
keywords:
  - 统计部门人数
  - 部门统计
  - Excel统计
  - 人数统计
---

KEYWORDS: 统计部门人数, 部门统计, Excel统计, 人数统计

# 统计部门人数

## 流程
1. 使用 read_file 读取 Excel 文件
2. 使用 run_python 执行 pandas 统计
3. 返回统计结果

## 注意事项
- 确保文件路径正确
- 处理大文件时注意性能
```

**关键词生成要求：**
- 生成 5-10 个关键词
- 包含：任务类型（如"Excel处理"）、具体动作（如"统计"）、对象（如"部门人数"）
- 关键词应该简短（2-6 个字），便于匹配
- 避免使用"帮我"、"请"等无关词汇

请开始编写技能内容：
"""

    # 调用 LLM
    messages = [
        {"role": "system", "content": "你是一个专业的 AI Agent 技能编写专家。"},
        {"role": "user", "content": prompt}
    ]
    
    try:
        response = llm_call_func(messages)
        skill_content = response.get("message", {}).get("content", "")
        return skill_content.strip()
    except Exception as e:
        print(f"[ERROR] LLM 调用失败: {e}")
        # 返回一个简单的降级 Skill 内容
        return _generate_fallback_skill(task_info)


def _truncate_steps_intelligently(steps: list[dict], max_chars: int = 3000) -> str:
    """
    智能截断 steps，确保在消息边界截断，不破坏 JSON 结构。
    """
    result = []
    total_chars = 0
    
    for step in steps:
        step_str = json.dumps(step, ensure_ascii=False, indent=2)
        if total_chars + len(step_str) > max_chars:
            # 如果添加这个 step 会超出限制，停止
            break
        result.append(step)
        total_chars += len(step_str)
    
    return json.dumps(result, ensure_ascii=False, indent=2)


def _load_example_skills() -> str:
    """读取内置技能文件作为格式示例"""
    skills_dir = Path(__file__).parent.parent / "prompts" / "skills"
    example_files = ["data_excel.md", "code_generate.md", "system_config.md"]
    
    examples = []
    for filename in example_files:
        file_path = skills_dir / filename
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            examples.append(f"### 示例：{filename}\n{content[:500]}")  # 只取前 500 字符
    
    return "\n\n".join(examples) if examples else "（无示例）"


def _generate_fallback_skill(task_info: dict) -> str:
    """生成降级的 Skill 内容（当 LLM 调用失败时使用）"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 从任务描述中提取基础关键词
    task_desc = task_info['task_description'][:50]
    tools = task_info.get('tools_used', [])
    
    # 生成关键词列表
    keywords = []
    
    # 从任务描述分词提取关键词（优先从描述提取）
    words = re.split(r'[\s，,、。.!？?""]+', task_info['task_description'])
    
    # 无意义词过滤列表
    stop_words = {'帮我', '请', '我想', '能否', '这个', '一下', '处理', '一下', '好吗', '呢', '啊', '吧', '呀'}
    
    for w in words:
        w = w.strip()
        # 跳过太短、太长或无意义词的
        if 1 <= len(w) <= 6 and w not in stop_words:
            keywords.append(w)
    
    # 如果工具有，添加工具名作为关键词（不带 _tool 后缀）
    for t in tools[:2]:
        if t not in keywords:
            keywords.append(t)
    
    # 从工具名生成派生关键词
    for t in tools[:1]:
        if t == 'read_file' and '文件' not in keywords:
            keywords.append('文件')
        elif t == 'run_python' and '脚本' not in keywords:
            keywords.append('脚本')
        elif t == 'web_search' and '搜索' not in keywords:
            keywords.append('搜索')
    
    # 去重并限制数量（保持顺序）
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)
    keywords = unique_keywords[:8]  # 最多 8 个关键词
    
    # 构建 YAML frontmatter（包含 keywords）
    keywords_yaml = '\n'.join(f'  - {kw}' for kw in keywords) if keywords else '  - unknown'
    keywords_line = ', '.join(keywords) if keywords else 'unknown'
    
    lines = [
        f"---",
        f"skill_name: {task_desc}",
        f"version: 1.0",
        f"created_at: {now}",
        f"keywords:",
        keywords_yaml,
        f"---",
        f"",
        f"KEYWORDS: {keywords_line}",
        f"",
        f"# {task_desc}",
        f"",
        f"## 工具使用",
        f"本次任务使用了以下工具：{', '.join(tools) if tools else '无'}",
        f"",
        f"## 处理步骤",
        f"1. 分析用户需求",
        f"2. 选择合适的工具",
        f"3. 执行任务",
        f"4. 返回结果",
        f"",
        f"## 注意事项",
        f"- 确保工具参数正确",
        f"- 处理大文件时注意性能",
    ]
    return "\n".join(lines)


def _extract_keywords_from_skill(content: str) -> list[str]:
    """
    从 LLM 生成的 Skill 内容中提取关键词。
    
    尝试顺序：
    1. 从 YAML frontmatter 的 keywords 字段提取（只匹配文件开头的 frontmatter）
    2. 从 KEYWORDS 行提取（格式：KEYWORDS: 关键词1, 关键词2, 关键词3）
    3. 从 # 标题提取（作为备选）
    
    返回：关键词列表
    """
    keywords = []
    
    # 方法 1：从 YAML frontmatter 提取（使用 yaml.safe_load 代替脆弱的正则）
    yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if yaml_match:
        yaml_text = yaml_match.group(1)
        try:
            frontmatter = yaml.safe_load(yaml_text)
            if isinstance(frontmatter, dict) and 'keywords' in frontmatter:
                kw_value = frontmatter['keywords']
                if isinstance(kw_value, list):
                    keywords = [str(k).strip() for k in kw_value if k]
                elif isinstance(kw_value, str):
                    keywords = [k.strip() for k in kw_value.split(',') if k.strip()]
        except yaml.YAMLError:
            pass  # YAML 解析失败时回退到后续方法
    
    # 方法 2：从 KEYWORDS 行提取（独立一行，不在 yaml 内）
    if not keywords:
        # 匹配 frontmatter 结束后的 KEYWORDS 行（捕获冒号后面的内容）
        keywords_match = re.search(r'^---\s*\n.*?\n---\s*\nKEYWORDS:\s*(.+)', content, re.DOTALL)
        if not keywords_match:
            # 备选：直接搜索 KEYWORDS 行（捕获冒号后面的内容）
            keywords_match = re.search(r'KEYWORDS:\s*(.+)', content)
        
        if keywords_match:
            keywords_text = keywords_match.group(1).strip()
            keywords = [kw.strip() for kw in keywords_text.split(',') if kw.strip()]
    
    # 方法 3：从 # 标题提取（备选）
    if not keywords:
        title_match = re.search(r'\n#\s+([^\n]+)', content)
        if title_match:
            title = title_match.group(1).strip()
            # 简单的分词：按空格或标点分割
            keywords = [kw.strip() for kw in re.split(r'[\s，,、]+', title) if len(kw.strip()) >= 2]
    
    # 清理关键词
    cleaned_keywords = []
    for kw in keywords:
        # 去除引号
        kw = kw.strip('"\'')
        # 去除空字符串
        if kw and len(kw) >= 2:
            cleaned_keywords.append(kw)
    
    return cleaned_keywords[:10]  # 最多返回 10 个关键词


@register_tool
def save_skill(skill_name: str, content: str, version: str = "1.0", merge: bool = True) -> dict:
    """
    保存 Skill 到文件（优化版）。
    
    优化点：
    - 使用 YAML frontmatter 存储版本号（不污染正文）
    - 版本冲突时自动备份旧版本
    - 支持版本自增
    
    参数：
        skill_name: 技能名称（将用作文件名，会自动添加 .md 后缀）
        content: Skill Markdown 内容（包含 YAML frontmatter）
        version: 版本号（默认 "1.0"）
        merge: 是否合并已有技能（默认 True，会备份旧版本）
    
    返回：
        {"success": bool, "message": str, "file_path": str, "version": str}
    """
    # 规范化文件名
    safe_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', skill_name)
    safe_name = safe_name.strip('_')
    
    if not safe_name:
        safe_name = f"learned_skill_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # 确保目录存在
    AGENT_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    
    file_path = AGENT_SKILLS_DIR / f"{safe_name}.md"
    
    # 检查文件是否已存在
    if file_path.exists() and merge:
        # 合并保存前备份旧版本到 _backups 目录，便于失败回滚
        backup_dir = AGENT_SKILLS_DIR / "_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        backup_path = backup_dir / f"{safe_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        shutil.copy(file_path, backup_path)
        
        print(f"[INFO] 已备份旧版本到 {backup_path}")
    
    try:
        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        return {
            "success": True,
            "message": f"技能已保存到 {file_path}",
            "file_path": str(file_path),
            "version": version
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"保存失败: {str(e)}",
            "file_path": ""
        }


@register_tool
def register_skill_to_yaml(skill_name: str, description: str, keywords: list[str], version: str = "1.0") -> dict:
    """
    将新技能注册到 agent_tools.yaml 的 domains 中。
    
    参数：
        skill_name: 技能名称（对应 agent_skills/*.md 文件名，不含 .md）
        description: 技能描述
        keywords: 触发该技能的关键词列表
        version: 版本号
    
    返回：
        {"success": bool, "message": str}
    """
    try:
        # 读取现有配置
        config = load_agent_tools_config()
        
        # 添加新域
        if "domains" not in config:
            config["domains"] = {}
        
        config["domains"][skill_name] = {
            "keywords": keywords,
            "tools": [],  # 留空，因为技能文件会注入所有工具
            "description": description,
            "version": version,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "file_path": f"agent_skills/{skill_name}.md"
        }
        
        # 写回文件
        with open(AGENT_TOOLS_YAML, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, sort_keys=False)
        
        return {"success": True, "message": f"技能已注册到 agent_tools.yaml"}
    except Exception as e:
        return {"success": False, "message": f"注册失败: {str(e)}"}


@register_tool
def load_agent_tools_config() -> dict:
    """加载 agent_tools.yaml 配置"""
    if not AGENT_TOOLS_YAML.exists():
        # 创建默认配置
        default_config = {
            "domains": {},
            "management": {
                "auto_register": True,
                "max_skills": 50,
                "version_policy": "increment"
            }
        }
        with open(AGENT_TOOLS_YAML, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, allow_unicode=True, sort_keys=False)
        return default_config
    
    try:
        with open(AGENT_TOOLS_YAML, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config if config else {"domains": {}, "management": {}}
    except Exception as e:
        print(f"[ERROR] agent_tools.yaml 加载失败: {e}")
        return {"domains": {}, "management": {}}


@register_tool
def suggest_skill_name(task_info: dict, llm_call_func) -> str:
    """
    使用 LLM 建议一个技能名称。
    
    参数：
        task_info: extract_task_steps_from_history 的返回值
        llm_call_func: LLM 调用函数
    
    返回：
        建议的技能名称（字符串）
    """
    prompt = f"""请根据以下任务描述，为这个技能起一个简洁的中文名称（不超过 20 个字）。

任务描述：{task_info['task_description']}
使用工具：{', '.join(task_info['tools_used']) if task_info['tools_used'] else '无'}

要求：
1. 名称应该体现技能的核心功能或领域
2. 使用中文
3. 不超过 20 个字
4. 不要包含特殊字符
5. 不要包含"技能"二字（会自动添加）

直接输出名称，不要有其他内容。
"""

    messages = [
        {"role": "user", "content": prompt}
    ]
    
    try:
        response = llm_call_func(messages)
        name = response.get("message", {}).get("content", "").strip()
        # 清理名称
        name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', name)
        return name[:50]  # 限制长度
    except Exception:
        # 降级：使用任务描述前 20 个字
        return task_info['task_description'][:20].strip()


# 预留接口：删除技能
@register_tool
def delete_skill(skill_name: str) -> dict:
    """
    删除技能（预留接口）
    
    参数：
        skill_name: 技能名称（不含 .md）
    
    返回：
        {"success": bool, "message": str}
    """
    file_path = AGENT_SKILLS_DIR / f"{skill_name}.md"
    
    if not file_path.exists():
        return {"success": False, "message": f"技能文件不存在: {file_path}"}
    
    try:
        # 删除技能文件
        file_path.unlink()
        
        # 从 agent_tools.yaml 中移除注册
        config = load_agent_tools_config()
        if "domains" in config and skill_name in config["domains"]:
            del config["domains"][skill_name]
            with open(AGENT_TOOLS_YAML, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
        
        return {"success": True, "message": f"技能已删除: {skill_name}"}
    except Exception as e:
        return {"success": False, "message": f"删除失败: {str(e)}"}


# 预留接口：更新技能
@register_tool
def update_skill(skill_name: str, content: str, new_version: str = None) -> dict:
    """
    更新技能（预留接口）
    
    参数：
        skill_name: 技能名称（不含 .md）
        content: 新的技能内容
        new_version: 新版本号（如果为 None，则自动自增）
    
    返回：
        {"success": bool, "message": str, "version": str}
    """
    file_path = AGENT_SKILLS_DIR / f"{skill_name}.md"
    
    if not file_path.exists():
        return {"success": False, "message": f"技能文件不存在: {file_path}", "version": ""}
    
    try:
        # 读取现有内容，提取版本号
        existing_content = file_path.read_text(encoding="utf-8")
        existing_version = _extract_version_from_frontmatter(existing_content)
        
        # 确定新版本号
        if new_version is None:
            new_version = _increment_version(existing_version)
        
        # 更新 YAML frontmatter 中的版本号
        content = _update_version_in_frontmatter(content, new_version)
        
        # 版本更新前备份旧版本到 _backups 目录（文件名含原版本号），便于失败回滚
        backup_dir = AGENT_SKILLS_DIR / "_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{skill_name}_v{existing_version}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        shutil.copy(file_path, backup_path)
        
        # 写入新内容
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        # 更新 agent_tools.yaml 中的版本号
        config = load_agent_tools_config()
        if "domains" in config and skill_name in config["domains"]:
            config["domains"][skill_name]["version"] = new_version
            with open(AGENT_TOOLS_YAML, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, sort_keys=False)
        
        return {"success": True, "message": f"技能已更新: {skill_name} (v{new_version})", "version": new_version}
    except Exception as e:
        return {"success": False, "message": f"更新失败: {str(e)}", "version": ""}


def _extract_version_from_frontmatter(content: str) -> str:
    """从 YAML frontmatter 中提取版本号"""
    match = re.search(r'^version:\s*([\d\.]+)', content, re.MULTILINE)
    if match:
        return match.group(1)
    return "1.0"


def _update_version_in_frontmatter(content: str, new_version: str) -> str:
    """更新 YAML frontmatter 中的版本号"""
    return re.sub(r'^(version:\s*)[\d\.]+', f'\\g<1>{new_version}', content, flags=re.MULTILINE)


def _increment_version(version: str) -> str:
    """版本号自增（1.0 → 1.1）"""
    try:
        parts = version.split('.')
        if len(parts) == 2:
            major = int(parts[0])
            minor = int(parts[1])
            minor += 1
            return f"{major}.{minor}"
        else:
            return "1.0"
    except Exception:
        return "1.0"


if __name__ == "__main__":
    # 测试
    test_messages = [
        {"role": "user", "content": "帮我处理这个 Excel 文件，统计每个部门的人数"},
        {"role": "assistant", "content": '{"tool_name": "read_file", "arguments": {"file_path": "data.xlsx"}}'},
        {"role": "user", "content": '工具 read_file 返回：文件内容...'},
        {"role": "assistant", "content": '{"tool_name": "run_python", "arguments": {"code": "..."}}'},
    ]
    
    # 测试意图检测
    print("测试意图检测:")
    test_cases = [
        ("把这次处理的经验内化为新技能", True),
        ("帮我处理这个 Excel 文件", False),
        ("保存为技能", True),
    ]
    for msg, expected in test_cases:
        result = is_skill_learning_request(msg)
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] '{msg}' -> {result}")
    
    # 测试步骤提取
    print("\n测试步骤提取:")
    task_info = extract_task_steps_from_history(test_messages)
    print(f"  任务描述: {task_info['task_description']}")
    print(f"  使用工具: {task_info['tools_used']}")
    print(f"  步骤数量: {len(task_info['steps'])}")
