"""Teage Skill 本地插件系统。

提供 SkillLoader 用于扫描 skills/ 目录、解析 SKILL.md frontmatter、
动态 importlib 加载 tools.py。

典型用法：
    loader = SkillLoader()
    metas = loader.discover()  # 仅扫描元数据，不 import 代码
    skill = loader.load("calculator")  # 动态加载代码
    load_skill_to_registry(registry, skill)  # 注册到 ToolRegistry Deferred 层
"""
