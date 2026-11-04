# -*- coding: utf-8 -*-
"""工具声明一致性回归：幻影工具不得注入、域工具必须已注册、域技能提示词不缺失。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools as tools_pkg  # noqa: E402

tools_pkg._ensure_tool_registration()

ALL_DOMAINS = sorted(tools_pkg.tool_data_provider.get_all_domains())
REGISTERED = set(tools_pkg.TOOL_REGISTRY)


def _injected(domain):
    params = tools_pkg.build_tools_param([domain]) or []
    return [t["function"]["name"] for t in params]


def test_domain_tools_are_registered():
    for domain in ALL_DOMAINS:
        for name in tools_pkg.tool_data_provider.get_domain_tools(domain):
            assert name in REGISTERED, f"域 {domain} 引用了未注册工具 {name}"


def test_injected_tools_are_registered():
    for domain in ALL_DOMAINS:
        for name in _injected(domain):
            assert name in REGISTERED, f"域 {domain} 注入了未注册工具 {name}"


def test_always_inject_tools_are_registered():
    for name in tools_pkg.tool_data_provider.get_always_inject_tools():
        assert name in REGISTERED, f"always_inject 引用了未注册工具 {name}"


def test_known_domains_have_skill_prompt():
    # tools.yaml 中带域名与技能提示词文件存在映射关系的域
    for domain in ["code_dev", "excel_analyze", "system_basic",
                   "system_high_risk", "office_word", "office_ppt", "web_browse"]:
        assert tools_pkg.get_skill_prompt(domain), f"域 {domain} 缺失技能提示词"


def test_skill_map_targets_exist():
    skills_dir = ROOT / "prompts" / "skills"
    for domain, skill_name in tools_pkg.DOMAIN_SKILL_MAP.items():
        assert (skills_dir / f"{skill_name}.md").exists(), \
            f"域 {domain} 映射的技能文件 {skill_name}.md 不存在"
